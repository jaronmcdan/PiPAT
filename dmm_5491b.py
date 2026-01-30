# dmm_5491b.py
"""
5491B Multimeter support (SCPI over serial).

This driver is designed to be tolerant of:
- echoed commands (common on some serial SCPI devices)
- single-line responses that may include whitespace or comma-separated values
- minor SCPI dialect differences (FUNC vs CONF, etc.)

Supported modes (as used by PiPAT CAN control):
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


def _is_all_zero(bs: bytes) -> bool:
    return all(b == 0 for b in bs)


class Dmm5491B:
    def __init__(self, ser: serial.Serial):
        self.ser = ser
        # Some units implement an SCPI error queue (SYST:ERR? / SYSTEM:ERROR?).
        # When available, we can safely probe alternate command spellings without
        # leaving the front panel in a persistent "bad bus command" state.
        self._errq_cmd: Optional[str] = None  # resolved lazily

    def write(self, cmd: str) -> None:
        if not cmd.endswith("\n"):
            cmd += "\n"
        self.ser.write(cmd.encode("ascii", errors="replace"))
        self.ser.flush()

    def query(self, cmd: str, *, max_lines: int = 6, settle_s: float = 0.02) -> str:
        """
        Send a query and return the first non-echo response line.
        Many 5491B units echo the command line back before returning the response.
        """
        cmd_stripped = cmd.strip()
        self.write(cmd_stripped)
        # Give the device a moment to respond.
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

            # Ignore exact echoes (e.g. "*IDN?" or "FETC?")
            if line.strip() == cmd_stripped:
                continue

            # Some firmwares echo with trailing spaces or with leading prompt chars.
            if line.replace(" ", "") == cmd_stripped.replace(" ", ""):
                continue

            best = line
            break

        return best

    def identify(self) -> str:
        return self.query("*IDN?")

    def _try_write_sequence(self, cmds: list[str]) -> None:
        """Try alternate SCPI spellings *safely*.

        Previous versions would blindly write every candidate spelling.
        On some 5491B firmwares, an unsupported spelling triggers a front-panel
        "bad bus command" warning. If the meter supports an SCPI error queue,
        we can probe candidates and stop after the first that produces no error.

        If no error queue is available, we conservatively send only the first
        candidate (the most likely to work) to avoid spurious errors.
        """

        # If we can't query an error queue, do *not* blast multiple candidates.
        if not self._ensure_errq_cmd():
            if cmds:
                try:
                    self.write(cmds[0])
                    time.sleep(0.01)
                except Exception:
                    pass
            return

        # With an error queue, probe until one succeeds.
        for c in cmds:
            try:
                self._clear_status_best_effort()
                self.write(c)
                time.sleep(0.02)
                err = self._read_err()
                if err is None:
                    # Unexpected: we previously detected error queue support.
                    # Fail closed: stop probing to avoid spamming.
                    return
                code, _msg = err
                if code == 0:
                    return
            except Exception:
                # ignore and continue
                continue

    def _clear_status_best_effort(self) -> None:
        """Clear error/status queues without assuming any single dialect."""
        for cmd in ("*CLS", "SYST:CLE", "SYSTEM:CLEAR"):
            try:
                self.write(cmd)
                time.sleep(0.005)
            except Exception:
                pass

    def _ensure_errq_cmd(self) -> bool:
        """Detect which error-queue query works (if any)."""
        if self._errq_cmd is not None:
            return bool(self._errq_cmd)

        # Try common spellings.
        for cand in ("SYST:ERR?", "SYSTEM:ERROR?"):
            try:
                resp = self.query(cand, settle_s=0.02)
                if self._parse_err(resp) is not None:
                    self._errq_cmd = cand
                    return True
            except Exception:
                continue

        self._errq_cmd = ""  # sentinel = unsupported
        return False

    def _read_err(self) -> Optional[tuple[int, str]]:
        """Return (code, message) from the SCPI error queue, or None if unsupported."""
        if not self._ensure_errq_cmd():
            return None
        try:
            resp = self.query(self._errq_cmd, settle_s=0.02)
        except Exception:
            return None
        return self._parse_err(resp)

    @staticmethod
    def _parse_err(resp: str) -> Optional[tuple[int, str]]:
        # Typical forms:
        #   0,"No error"
        #   +0,"No error"
        #  -113,"Undefined header"
        # Some firmwares omit quotes.
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
        # Strip optional surrounding quotes
        if len(msg) >= 2 and ((msg[0] == '"' and msg[-1] == '"') or (msg[0] == "'" and msg[-1] == "'")):
            msg = msg[1:-1]
        return code, msg

    def set_mode(self, mode: int) -> None:
        if mode == MeterMode.VDC:
            self._try_write_sequence([
                # Most SCPI DMMs accept either FUNC or CONF; some require quotes.
                "FUNC VOLT:DC",
                "FUNC \"VOLT:DC\"",
                "CONF:VOLT:DC",
                "SENS:FUNC \"VOLT:DC\"",
            ])
        elif mode == MeterMode.IDC:
            self._try_write_sequence([
                "FUNC CURR:DC",
                "FUNC \"CURR:DC\"",
                "CONF:CURR:DC",
                "SENS:FUNC \"CURR:DC\"",
            ])
        elif mode == MeterMode.FREQ:
            self._try_write_sequence([
                "FUNC FREQ",
                "FUNC \"FREQ\"",
                "CONF:FREQ",
                "SENS:FUNC \"FREQ\"",
            ])
        elif mode == MeterMode.OHM:
            # 2-wire resistance
            self._try_write_sequence([
                "FUNC RES",
                "FUNC \"RES\"",
                "CONF:RES",
                "SENS:FUNC \"RES\"",
            ])

    def set_range(self, mode: int, range_value: Optional[float]) -> None:
        """
        Set manual range when range_value > 0, else enable autorange.
        Units:
        - VDC: volts
        - IDC: amps
        - OHM: ohms
        - FREQ: (best-effort) typically ignored; instrument-dependent
        """
        if range_value is None or range_value <= 0:
            self._set_autorange(mode, True)
            return

        self._set_autorange(mode, False)
        v = float(range_value)

        if mode == MeterMode.VDC:
            self._try_write_sequence([
                f"VOLT:DC:RANG {v}",
                f"SENS:VOLT:DC:RANG {v}",
            ])
        elif mode == MeterMode.IDC:
            self._try_write_sequence([
                f"CURR:DC:RANG {v}",
                f"SENS:CURR:DC:RANG {v}",
            ])
        elif mode == MeterMode.OHM:
            self._try_write_sequence([
                f"RES:RANG {v}",
                f"SENS:RES:RANG {v}",
            ])
        elif mode == MeterMode.FREQ:
            # Some meters allow setting expected input range or gate; keep best-effort.
            self._try_write_sequence([
                f"FREQ:RANG {v}",
                f"SENS:FREQ:RANG {v}",
            ])

    def _set_autorange(self, mode: int, enable: bool) -> None:
        e = 1 if enable else 0
        if mode == MeterMode.VDC:
            self._try_write_sequence([
                f"VOLT:DC:RANG:AUTO {e}",
                f"VOLT:DC:RANG:AUTO {'ON' if e else 'OFF'}",
                f"SENS:VOLT:DC:RANG:AUTO {e}",
                f"SENS:VOLT:DC:RANG:AUTO {'ON' if e else 'OFF'}",
            ])
        elif mode == MeterMode.IDC:
            self._try_write_sequence([
                f"CURR:DC:RANG:AUTO {e}",
                f"CURR:DC:RANG:AUTO {'ON' if e else 'OFF'}",
                f"SENS:CURR:DC:RANG:AUTO {e}",
                f"SENS:CURR:DC:RANG:AUTO {'ON' if e else 'OFF'}",
            ])
        elif mode == MeterMode.OHM:
            self._try_write_sequence([
                f"RES:RANG:AUTO {e}",
                f"RES:RANG:AUTO {'ON' if e else 'OFF'}",
                f"SENS:RES:RANG:AUTO {e}",
                f"SENS:RES:RANG:AUTO {'ON' if e else 'OFF'}",
            ])
        elif mode == MeterMode.FREQ:
            self._try_write_sequence([
                f"FREQ:RANG:AUTO {e}",
                f"FREQ:RANG:AUTO {'ON' if e else 'OFF'}",
                f"SENS:FREQ:RANG:AUTO {e}",
                f"SENS:FREQ:RANG:AUTO {'ON' if e else 'OFF'}",
            ])

    def read(self) -> Optional[float]:
        """
        Read the current function's measurement as a float.
        Returns None if a parseable numeric value isn't received.
        """
        # Preferred fast path:
        resp = self.query("FETC?")
        if not resp:
            resp = self.query("READ?")
        if not resp:
            return None

        # Some meters return "9.9E37" or similar for overrange.
        val = _parse_float(resp)
        return val

    def read_reading(self, mode: int) -> MeterReading:
        raw = self.read()
        if raw is None:
            return MeterReading(mode=mode, unit=MeterUnit.UNKNOWN, value=0.0, valid=False, overrange=False)

        # Overrange heuristics: many meters use very large sentinel values (e.g. 9.9e37)
        overrange = abs(raw) > 1e20

        unit = MeterUnit.UNKNOWN
        if mode == MeterMode.VDC:
            unit = MeterUnit.VOLT
        elif mode == MeterMode.IDC:
            unit = MeterUnit.AMP
        elif mode == MeterMode.FREQ:
            unit = MeterUnit.HZ
        elif mode == MeterMode.OHM:
            unit = MeterUnit.OHM

        return MeterReading(mode=mode, unit=unit, value=float(raw), valid=True, overrange=overrange)


def legacy_range_code_to_value(mode: int, code: int) -> Optional[float]:
    """
    Back-compat range code mapping (byte1 in MMETER_CTRL_ID).
    code==0 => AUTO (returns None)
    Otherwise returns a best-effort manual range float.

    NOTE: These are generic DMM ranges; if you need exact ranges, use range_value float.
    """
    code = int(code) & 0xFF
    if code == 0:
        return None

    # Tables are 1-indexed by code.
    if mode == MeterMode.VDC:
        table = [0.5, 5.0, 50.0, 500.0, 1000.0]
    elif mode == MeterMode.IDC:
        table = [0.05, 0.5, 5.0, 10.0]
    elif mode == MeterMode.OHM:
        table = [500.0, 5e3, 50e3, 500e3, 5e6, 50e6]
    elif mode == MeterMode.FREQ:
        # Typically ignore; leave autorange.
        return None
    else:
        return None

    idx = code - 1
    if 0 <= idx < len(table):
        return float(table[idx])
    return None
