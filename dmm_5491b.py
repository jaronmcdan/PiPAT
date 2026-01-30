# dmm_5491b.py
"""B&K Precision 5491B Multimeter support (SCPI over serial).

Key reliability notes for this instrument family:

- The manual describes a stateful SCPI "path pointer" where omitting the leading ':'
  makes a command relative to the current path, and the path pointer only moves down.
  To avoid BUS:BAD COMMAND from accidentally sending a higher-level command in a
  lower-level context, this driver *always* sends subsystem commands as root commands
  by prefixing ':' (unless the command already begins with ':' or '*'). (See the
  5491B/2831E user manual SCPI section.)

- Error queue query is :SYSTem:ERRor? (short form :SYST:ERR? is also accepted on most
  firmwares). When enabled, errq probing allows us to try alternate spellings without
  leaving persistent front-panel "BUS:BAD COMMAND" warnings.

PiPAT CAN modes:
- 0: VDC
- 1: IDC
- 2: FREQ
- 3: OHM (2-wire resistance)

Range control:
- range_value <= 0 or None => AUTO range
- otherwise sets manual range (units depend on mode)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

import serial


class MeterMode:
    VDC = 0
    IDC = 1
    FREQ = 2
    OHM = 3


class MeterUnit:
    VOLT = 0
    AMP = 1
    HZ = 2
    OHM = 3
    UNKNOWN = 255


@dataclass(frozen=True)
class MeterReading:
    mode: int
    unit: int
    value: float
    valid: bool = True
    overrange: bool = False

    @property
    def flags_byte(self) -> int:
        flags = 0
        if self.valid:
            flags |= 0x01
        if self.overrange:
            flags |= 0x02
        return flags & 0xFF


_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def _parse_float(s: str) -> Optional[float]:
    """Extract the first float from a string."""
    m = _FLOAT_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _normalize_scpi(cmd: str) -> str:
    """Return a SCPI command rooted at ':' unless it's a common command."""
    c = cmd.strip()
    if not c:
        return c
    if c.startswith("*") or c.startswith(":"):
        return c
    # Root subsystem command to avoid path-pointer surprises.
    return ":" + c


class Dmm5491B:
    def __init__(self, ser: serial.Serial, *, errq_probe: bool = False):
        self.ser = ser
        self._enable_errq_probe = bool(errq_probe)
        # Resolved lazily. Empty string sentinel means "unsupported".
        self._errq_cmd: Optional[str] = None

        # Small gaps help when the instrument is busy switching functions/ranges.
        self._post_write_sleep_s = 0.01
        self._post_func_sleep_s = 0.05

    def write(self, cmd: str) -> None:
        c = _normalize_scpi(cmd)
        if not c.endswith("\n"):
            c += "\n"
        self.ser.write(c.encode("ascii", errors="replace"))
        self.ser.flush()
        if self._post_write_sleep_s > 0:
            time.sleep(self._post_write_sleep_s)

    def query(self, cmd: str, *, max_lines: int = 8, settle_s: float = 0.02) -> str:
        """Send a query and return the first non-echo response line.

        Many 5491B units echo the command line back before returning the response.
        """
        cmd_norm = _normalize_scpi(cmd)
        cmd_stripped = cmd_norm.strip()
        self.write(cmd_stripped)

        if settle_s > 0:
            time.sleep(settle_s)

        best = ""
        for _ in range(max_lines):
            raw = self.ser.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue

            # Ignore exact echoes (e.g. ":*IDN?" or ":FETCh?")
            if line == cmd_stripped:
                continue

            # Some firmwares echo with whitespace differences.
            if line.replace(" ", "") == cmd_stripped.replace(" ", ""):
                continue

            best = line
            break

        return best

    def identify(self) -> str:
        return self.query("*IDN?")

    # -----------------------
    # Error queue (optional)
    # -----------------------
    def _ensure_errq_cmd(self) -> bool:
        """Detect which error-queue query works (if any)."""
        if not self._enable_errq_probe:
            if self._errq_cmd is None:
                self._errq_cmd = ""  # sentinel
            return False

        if self._errq_cmd is not None:
            return bool(self._errq_cmd)

        # The 5491B manual documents :SYSTem:ERRor? as the error queue query.
        # We still try a couple of common alternates for safety.
        for cand in (":SYSTem:ERRor?", ":SYST:ERR?", "SYST:ERR?", "SYSTEM:ERROR?"):
            try:
                resp = self.query(cand, settle_s=0.02)
                if self._parse_err(resp) is not None:
                    self._errq_cmd = cand
                    return True
            except Exception:
                continue

        self._errq_cmd = ""
        return False

    def _read_err(self) -> Optional[tuple[int, str]]:
        if not self._ensure_errq_cmd():
            return None
        try:
            resp = self.query(self._errq_cmd, settle_s=0.02)  # type: ignore[arg-type]
        except Exception:
            return None
        return self._parse_err(resp)

    @staticmethod
    def _parse_err(resp: str) -> Optional[tuple[int, str]]:
        # Typical forms:
        #   0,"NO ERROR!"
        #  -113,"Undefined header"
        if not resp:
            return None
        m = re.match(r"^\s*([+-]?\d+)\s*(?:,\s*(.*))?$", resp)
        if not m:
            return None
        try:
            code = int(m.group(1))
        except Exception:
            return None
        msg = (m.group(2) or "").strip()
        if len(msg) >= 2 and ((msg[0] == '"' and msg[-1] == '"') or (msg[0] == "'" and msg[-1] == "'")):
            msg = msg[1:-1]
        return code, msg

    def _clear_status_best_effort(self) -> None:
        # Keep this minimal to avoid creating errors on firmwares that implement
        # only a subset of common/status commands.
        try:
            self.write("*CLS")
        except Exception:
            pass

    def _try_write_sequence(self, cmds: list[str]) -> None:
        """Try alternate SCPI spellings safely.

        - If error queue probing is disabled, send only the first candidate.
        - If enabled and supported, send candidates until the error queue returns 0.
        """
        if not self._ensure_errq_cmd():
            if cmds:
                try:
                    self.write(cmds[0])
                except Exception:
                    pass
            return

        for c in cmds:
            try:
                self._clear_status_best_effort()
                self.write(c)
                err = self._read_err()
                if err is None:
                    return
                code, _msg = err
                if code == 0:
                    return
            except Exception:
                continue

    # -----------------------
    # Mode / range control
    # -----------------------
    def set_mode(self, mode: int) -> None:
        """Select measurement function."""
        if mode == MeterMode.VDC:
            self._try_write_sequence(["FUNCtion VOLTage:DC"])
        elif mode == MeterMode.IDC:
            self._try_write_sequence(["FUNCtion CURRent:DC"])
        elif mode == MeterMode.FREQ:
            self._try_write_sequence(["FUNCtion FREQuency"])
        elif mode == MeterMode.OHM:
            self._try_write_sequence(["FUNCtion RESistance"])
        # Give the meter a moment to settle after a function switch.
        if self._post_func_sleep_s > 0:
            time.sleep(self._post_func_sleep_s)

    def set_range(self, mode: int, range_value: Optional[float]) -> None:
        """Set manual range when range_value > 0, else enable autorange."""
        if range_value is None or range_value <= 0:
            self._set_autorange(mode, True)
            return

        v = float(range_value)

        if mode == MeterMode.VDC:
            # Manual specifies :VOLTage:DC:RANGe <n>
            self._try_write_sequence([f"VOLTage:DC:RANGe {v}"])
        elif mode == MeterMode.IDC:
            self._try_write_sequence([f"CURRent:DC:RANGe {v}"])
        elif mode == MeterMode.OHM:
            self._try_write_sequence([f"RESistance:RANGe {v}"])
        elif mode == MeterMode.FREQ:
            # No explicit range command is documented for FREQ in many DMMs; ignore.
            return

    def _set_autorange(self, mode: int, enable: bool) -> None:
        onoff = "ON" if enable else "OFF"
        if mode == MeterMode.VDC:
            self._try_write_sequence([f"VOLTage:DC:RANGe:AUTO {onoff}"])
        elif mode == MeterMode.IDC:
            self._try_write_sequence([f"CURRent:DC:RANGe:AUTO {onoff}"])
        elif mode == MeterMode.OHM:
            self._try_write_sequence([f"RESistance:RANGe:AUTO {onoff}"])
        elif mode == MeterMode.FREQ:
            return

    # -----------------------
    # Reading
    # -----------------------
    def read(self) -> Optional[float]:
        """Read the current function's measurement as a float."""
        # Fast path per manual: :FETCh? returns last available reading without triggering.
        resp = self.query("FETCh?")
        if not resp:
            resp = self.query("READ?")
        if not resp:
            return None
        return _parse_float(resp)

    def read_reading(self, mode: int) -> MeterReading:
        raw = self.read()
        if raw is None:
            return MeterReading(mode=mode, unit=MeterUnit.UNKNOWN, value=0.0, valid=False)

        unit = MeterUnit.UNKNOWN
        if mode == MeterMode.VDC:
            unit = MeterUnit.VOLT
        elif mode == MeterMode.IDC:
            unit = MeterUnit.AMP
        elif mode == MeterMode.FREQ:
            unit = MeterUnit.HZ
        elif mode == MeterMode.OHM:
            unit = MeterUnit.OHM

        # Heuristic: treat very large values as overrange (typical DMM behavior).
        overrange = abs(raw) >= 9.0e36
        return MeterReading(mode=mode, unit=unit, value=float(raw), valid=not overrange, overrange=overrange)
