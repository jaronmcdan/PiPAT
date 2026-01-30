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
        """
        Try a list of SCPI commands for compatibility.
        We do not have reliable error reporting, so we just write them in order.
        """
        for c in cmds:
            try:
                self.write(c)
                time.sleep(0.01)
            except Exception:
                # ignore and continue
                pass

    def set_mode(self, mode: int) -> None:
        if mode == MeterMode.VDC:
            self._try_write_sequence([
                "FUNC VOLT:DC",
                "CONF:VOLT:DC",
                "SENS:FUNC 'VOLT:DC'",
            ])
        elif mode == MeterMode.IDC:
            self._try_write_sequence([
                "FUNC CURR:DC",
                "CONF:CURR:DC",
                "SENS:FUNC 'CURR:DC'",
            ])
        elif mode == MeterMode.FREQ:
            self._try_write_sequence([
                "FUNC FREQ",
                "CONF:FREQ",
                "SENS:FUNC 'FREQ'",
            ])
        elif mode == MeterMode.OHM:
            # 2-wire resistance
            self._try_write_sequence([
                "FUNC RES",
                "CONF:RES",
                "SENS:FUNC 'RES'",
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
                f"SENS:VOLT:DC:RANG:AUTO {e}",
            ])
        elif mode == MeterMode.IDC:
            self._try_write_sequence([
                f"CURR:DC:RANG:AUTO {e}",
                f"SENS:CURR:DC:RANG:AUTO {e}",
            ])
        elif mode == MeterMode.OHM:
            self._try_write_sequence([
                f"RES:RANG:AUTO {e}",
                f"SENS:RES:RANG:AUTO {e}",
            ])
        elif mode == MeterMode.FREQ:
            self._try_write_sequence([
                f"FREQ:RANG:AUTO {e}",
                f"SENS:FREQ:RANG:AUTO {e}",
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
