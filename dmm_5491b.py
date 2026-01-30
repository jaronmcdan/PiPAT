# dmm_5491b.py
"""
5491B Multimeter support (SCPI over serial).

Design goals:
- Tolerant of echoed commands on serial
- Avoid "BUS:BAD COMMAND" by rooting subsystem commands with ':' (SCPI path-pointer safe)
- Keep PiPAT compatibility helpers (legacy_range_code_to_value)
- Keep API compatibility with older PiPAT code (errq_probe kwarg)
- Be conservative about CURRENT autorange (5491B only supports autorange on the low-current terminal)
  unless explicitly enabled.

Why CURRENT autorange is guarded:
The 5491B only supports autoranging for current readings under 500 mA (low-current terminal).
For higher current ranges (5 A / 20 A), only manual range is available. If the fixture is wired
to the high-current terminal and we force ':CURR:DC:RANG:AUTO ON', the meter can throw an
error even though the command is valid SCPI.

Set env var MULTI_METER_ALLOW_CURRENT_AUTORANGE=1 if your setup uses the low-current terminal
and you want current autoranging over SCPI.
"""

from __future__ import annotations

import os
import re
import threading
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
    m = _FLOAT_RE.search(s or "")
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "")
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name, "")
    if v == "":
        return default
    try:
        return float(v.strip())
    except Exception:
        return default


class Dmm5491B:
    """
    Minimal SCPI driver.

    Notes:
    - This meter implements a SCPI path-pointer; sending commands without a leading ':'
      can make subsequent commands be interpreted relative to the last subsystem.
      Always root non-* commands with ':'.
    - Serial access must be serialized. Even if PiPAT uses mmeter_lock, making the
      driver itself thread-safe avoids future regressions.
    """

    def __init__(self, ser: serial.Serial, errq_probe: Optional[bool] = None):
        self.ser = ser
        self._io_lock = threading.RLock()

        # Optional (usually OFF): probe error queue after writes.
        if errq_probe is None:
            self._probe_errq = _env_bool("MULTI_METER_ERRQ_PROBE", False)
        else:
            self._probe_errq = bool(errq_probe)

        # Guard current autorange unless explicitly enabled.
        self._allow_current_autorange = _env_bool("MULTI_METER_ALLOW_CURRENT_AUTORANGE", False)

        # Small settle delay after function change so we don't immediately hit it with range/read.
        # (This keeps mode switch + first range/read from tripping front-panel errors.)
        self._mode_settle_s = _env_float("MULTI_METER_MODE_SETTLE_S", 0.25)

        # Which error query works, resolved lazily if probing is enabled
        self._errq_cmd: Optional[str] = None

    @staticmethod
    def _root_cmd(cmd: str) -> str:
        cmd = (cmd or "").strip()
        if not cmd:
            return cmd
        if cmd.startswith("*"):
            return cmd
        if cmd.startswith(":"):
            return cmd
        return ":" + cmd

    def write(self, cmd: str) -> None:
        cmd_to_send = self._root_cmd(cmd)
        if not cmd_to_send.endswith("\n"):
            cmd_to_send += "\n"
        with self._io_lock:
            self.ser.write(cmd_to_send.encode("ascii", errors="replace"))
            self.ser.flush()

    def query(self, cmd: str, *, max_lines: int = 6, settle_s: float = 0.02) -> str:
        """
        Send a query and return the first non-echo response line.
        Many 5491B units echo the command line back before returning the response.
        """
        original = (cmd or "").strip()
        sent = self._root_cmd(original)

        with self._io_lock:
            self.write(sent)  # write() is already locked, but RLock makes this safe
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

                # Skip echoes (either the rooted or original form)
                if line == sent or line == original:
                    continue
                if line.replace(" ", "") == sent.replace(" ", ""):
                    continue
                if original and line.replace(" ", "") == original.replace(" ", ""):
                    continue

                best = line
                break

            return best

    def identify(self) -> str:
        return self.query("*IDN?")

    # ---- Optional error probing (disabled by default) ----

    def _clear_status_best_effort(self) -> None:
        for cmd in ("*CLS", "SYST:CLE", "SYSTEM:CLEAR"):
            try:
                self.write(cmd)
                time.sleep(0.005)
            except Exception:
                pass

    def _ensure_errq_cmd(self) -> bool:
        if self._errq_cmd is not None:
            return bool(self._errq_cmd)

        for cand in (":SYST:ERR?", ":SYSTEM:ERROR?"):
            try:
                resp = self.query(cand, settle_s=0.02)
                if self._parse_err(resp) is not None:
                    self._errq_cmd = cand
                    return True
            except Exception:
                continue

        self._errq_cmd = ""  # unsupported
        return False

    def _read_err(self) -> Optional[tuple[int, str]]:
        if not self._ensure_errq_cmd():
            return None
        try:
            resp = self.query(self._errq_cmd, settle_s=0.02)
        except Exception:
            return None
        return self._parse_err(resp)

    @staticmethod
    def _parse_err(resp: str) -> Optional[tuple[int, str]]:
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

    def _safe_write(self, cmd: str) -> None:
        """
        Write a command, optionally probe error queue if enabled.
        """
        if not self._probe_errq:
            self.write(cmd)
            return

        try:
            self._clear_status_best_effort()
        except Exception:
            pass

        self.write(cmd)

        err = self._read_err()
        if err is None:
            return
        code, msg = err
        if code != 0:
            print(f"[5491B] SCPI error after '{cmd}': {code}, {msg}")

    # ---- Mode / range ----

    def set_mode(self, mode: int) -> None:
        """
        Change function and allow the meter time to settle before any follow-on commands.
        """
        if mode == MeterMode.VDC:
            self._safe_write("FUNC VOLT:DC")
        elif mode == MeterMode.IDC:
            self._safe_write("FUNC CURR:DC")
        elif mode == MeterMode.FREQ:
            self._safe_write("FUNC FREQ")
        elif mode == MeterMode.OHM:
            self._safe_write("FUNC RES")

        # Give the instrument time to complete the internal mode transition.
        # This prevents the very next command (range/read) from being rejected.
        if self._mode_settle_s > 0:
            time.sleep(self._mode_settle_s)

    def _set_autorange(self, mode: int, enable: bool) -> None:
        e = "ON" if enable else "OFF"
        if mode == MeterMode.VDC:
            self._safe_write(f"VOLT:DC:RANG:AUTO {e}")
        elif mode == MeterMode.IDC:
            # Guarded: see module docstring.
            if not self._allow_current_autorange:
                return
            self._safe_write(f"CURR:DC:RANG:AUTO {e}")
        elif mode == MeterMode.OHM:
            self._safe_write(f"RES:RANG:AUTO {e}")
        elif mode == MeterMode.FREQ:
            pass

    def set_range(self, mode: int, range_value: Optional[float]) -> None:
        """
        range_value:
          - None or <= 0 => AUTO (if supported)
          - otherwise => manual range by specifying expected reading absolute value
        """
        if range_value is None or range_value <= 0:
            self._set_autorange(mode, True)
            return

        self._set_autorange(mode, False)
        v = float(range_value)

        if mode == MeterMode.VDC:
            self._safe_write(f"VOLT:DC:RANG {v}")
        elif mode == MeterMode.IDC:
            self._safe_write(f"CURR:DC:RANG {v}")
        elif mode == MeterMode.OHM:
            self._safe_write(f"RES:RANG {v}")
        elif mode == MeterMode.FREQ:
            pass

    # ---- Reading ----

    def read(self) -> Optional[float]:
        resp = self.query("FETC?")
        if not resp:
            resp = self.query("READ?")
        if not resp:
            return None
        return _parse_float(resp)

    def read_reading(self, mode: int) -> MeterReading:
        raw = self.read()
        if raw is None:
            return MeterReading(mode=mode, unit=MeterUnit.UNKNOWN, value=0.0, valid=False, overrange=False)

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

    NOTE: range_value float override (bytes2..5) is preferred if you need exact behavior.

    5491B-specific notes:
    - Current ranges are 5 mA, 50 mA, 500 mA, 5 A, 20 A.
    - Auto range for current is only available on the low-current terminal (<500 mA).
    """
    code = int(code) & 0xFF
    if code == 0:
        return None

    if mode == MeterMode.VDC:
        # Expected reading values that select typical ranges
        table = [0.1, 1.0, 10.0, 100.0, 1000.0]
    elif mode == MeterMode.IDC:
        # Expected reading values that map to 5 mA / 50 mA / 500 mA / 5 A / 20 A
        table = [0.003, 0.03, 0.3, 3.0, 15.0]
    elif mode == MeterMode.OHM:
        table = [300.0, 3e3, 30e3, 300e3, 3e6, 30e6]
    elif mode == MeterMode.FREQ:
        return None
    else:
        return None

    idx = code - 1
    if 0 <= idx < len(table):
        return float(table[idx])
    return None
