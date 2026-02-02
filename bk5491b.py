"""bk5491b.py

Minimal, robust SCPI helper for the B&K Precision 5491B (and similar) bench DMMs
over USB-serial.

This module also defines the PiPAT CAN-side function enums and SCPI mappings.

Why two SCPI "dialects"?
  - Some firmware / manuals use a classic SCPI tree with :FUNCtion, :VOLTage:DC:RANGe, etc.
    (see 2831E/5491B bench multimeter manual).
  - Other documentation (dual-display command set) uses :CONFigure (CONF:...) for selecting
    both primary and secondary display functions, and global CONF:RANGe:AUTO, etc.
    (see 5491/5492 remote operation manual section with dual display).

PiPAT can be configured to use either dialect (or auto-detect) via config.MMETER_SCPI_STYLE.

Goals
  - tolerate command echo (common on USB-serial instruments)
  - parse single or dual-display readings (CSV)
  - keep dependencies minimal (only stdlib)

This module intentionally does not manage threading; callers should guard
serial access with an external lock.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional


# --- Function enums used on CAN (and in HardwareManager state) ---


class MmeterFunc:
    VDC = 0
    VAC = 1
    IDC = 2
    IAC = 3
    RES = 4
    FREQ = 5
    PERIOD = 6
    DIODE = 7
    CONT = 8


FUNC_NAME = {
    MmeterFunc.VDC: "VDC",
    MmeterFunc.VAC: "VAC",
    MmeterFunc.IDC: "IDC",
    MmeterFunc.IAC: "IAC",
    MmeterFunc.RES: "RES",
    MmeterFunc.FREQ: "FREQ",
    MmeterFunc.PERIOD: "PER",
    MmeterFunc.DIODE: "DIODE",
    MmeterFunc.CONT: "CONT",
}


# --- SCPI "dialects" ---

SCPI_STYLE_CONF = "conf"  # :CONFigure (CONF:...) style
SCPI_STYLE_FUNC = "func"  # :FUNCtion, :VOLTage:DC:RANGe style
SCPI_STYLE_AUTO = "auto"


# Primary function selection commands
# NOTE: Always include a leading ':' for root-level addressing.

# FUNC-style (classic) function selection.
#
# NOTE: For many SCPI DMMs, the parameter values for :FUNCtion are *mnemonics*
# (e.g. "VOLT:DC", "CURR:DC"). Some firmware variants are picky about these
# tokens (they may not accept the long-form "VOLTage" / "CURRent" strings as
# parameter values even though long-form works in command headers).
#
# To maximize compatibility (and avoid front-panel "BUS: BAD COMMAND"), we use
# the common abbreviated mnemonics here.
FUNC_TO_SCPI_FUNC = {
    MmeterFunc.VDC: ":FUNCtion VOLTage:DC",
    MmeterFunc.VAC: ":FUNCtion VOLTage:AC",
    MmeterFunc.IDC: ":FUNCtion CURRent:DC",
    MmeterFunc.IAC: ":FUNCtion CURRent:AC",
    MmeterFunc.RES: ":FUNCtion RESistance",
    MmeterFunc.FREQ: ":FUNCtion FREQuency",
    MmeterFunc.PERIOD: ":FUNCtion PERiod",
    MmeterFunc.DIODE: ":FUNCtion DIODe",
    MmeterFunc.CONT: ":FUNCtion CONTinuity",
}



# CONF-style (legacy/alternate) function selection.
#
# IMPORTANT:
#   For the 2831E/5491B family, the official manual documents a :FUNCtion tree,
#   and B&K's "Added Commands" doc extends that with :FUNCtion2 for the secondary
#   display. That dialect is what PiPAT uses by default.
#
#   We keep this mapping as an escape hatch for odd firmware variants, but it is
#   NOT used unless you explicitly set MMETER_SCPI_STYLE=conf.
FUNC_TO_SCPI_CONF = {
    # Legacy / alternate command set. Many manuals show these without a leading ':'.
    MmeterFunc.VDC: "CONF:VOLT:DC",
    MmeterFunc.VAC: "CONF:VOLT:AC",
    MmeterFunc.IDC: "CONF:CURR:DC",
    MmeterFunc.IAC: "CONF:CURR:AC",
    MmeterFunc.RES: "CONF:RES",
    MmeterFunc.FREQ: "CONF:FREQ",
}



# Secondary display function selection for FUNC-style firmware.
# Per B&K "Added Commands" doc, the secondary display supports:
#   VOLTage:AC/DC, CURRent:AC/DC, FREQuency, dB, dBm.
# We only expose the subset we have enums for.
FUNC_TO_SCPI_FUNC2 = {
    MmeterFunc.VDC: ":FUNCtion2 VOLTage:DC",
    MmeterFunc.VAC: ":FUNCtion2 VOLTage:AC",
    MmeterFunc.IDC: ":FUNCtion2 CURRent:DC",
    MmeterFunc.IAC: ":FUNCtion2 CURRent:AC",
    MmeterFunc.FREQ: ":FUNCtion2 FREQuency",
}



# Which subsystem prefix to use for RANGE / AUTO-RANGE / NPLC / REF in FUNC-style.
# Not all functions support these; unsupported functions will be ignored.
FUNC_TO_RANGE_PREFIX_FUNC = {
    MmeterFunc.VDC: ":VOLTage:DC",
    MmeterFunc.VAC: ":VOLTage:AC",
    MmeterFunc.IDC: ":CURRent:DC",
    MmeterFunc.IAC: ":CURRent:AC",
    MmeterFunc.RES: ":RESistance",
}



FUNC_TO_NPLC_PREFIX_FUNC = {
    # Integration rate (NPLC) is supported for most basic measurement functions
    # except frequency/period/continuity/diode (see the 2831E/5491B manual).
    MmeterFunc.VDC: ":VOLTage:DC",
    MmeterFunc.VAC: ":VOLTage:AC",
    MmeterFunc.IDC: ":CURRent:DC",
    MmeterFunc.IAC: ":CURRent:AC",
    MmeterFunc.RES: ":RESistance",
}



FUNC_TO_REF_PREFIX_FUNC = {
    MmeterFunc.VDC: ":VOLTage:DC",
    MmeterFunc.VAC: ":VOLTage:AC",
    MmeterFunc.IDC: ":CURRent:DC",
    MmeterFunc.IAC: ":CURRent:AC",
    MmeterFunc.RES: ":RESistance",
}



def func_name(func: int) -> str:
    return FUNC_NAME.get(int(func), f"FUNC{int(func)}")


def func_unit(func: int) -> str:
    f = int(func)
    if f in (MmeterFunc.VDC, MmeterFunc.VAC):
        return "V"
    if f in (MmeterFunc.IDC, MmeterFunc.IAC):
        return "A"
    if f == MmeterFunc.RES:
        return "Ohm"
    if f == MmeterFunc.FREQ:
        return "Hz"
    if f == MmeterFunc.PERIOD:
        return "s"
    if f == MmeterFunc.DIODE:
        return "V"
    if f == MmeterFunc.CONT:
        return "Ohm"
    return ""


_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


def _extract_floats(s: str) -> list[float]:
    out: list[float] = []
    for m in _NUM_RE.finditer(s):
        try:
            out.append(float(m.group(0)))
        except Exception:
            continue
    return out


@dataclass
class FetchResult:
    primary: Optional[float] = None
    secondary: Optional[float] = None
    raw: str = ""


class BK5491B:
    """Thin SCPI helper around a pyserial Serial object."""

    def __init__(self, ser, *, log_fn: Callable[[str], None] = print) -> None:
        self.ser = ser
        self.log = log_fn

    def _write_line(self, cmd: str) -> None:
        b = (cmd.strip() + "\n").encode("ascii", errors="ignore")
        self.ser.write(b)
        try:
            self.ser.flush()
        except Exception:
            pass

    def write(self, cmd: str, *, delay_s: float = 0.0, clear_input: bool = False) -> None:
        """Write a command (no response expected)."""
        if clear_input:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
        self._write_line(cmd)
        if delay_s and delay_s > 0:
            time.sleep(float(delay_s))

    def query_line(
        self,
        cmd: str,
        *,
        delay_s: float = 0.0,
        read_lines: int = 6,
        clear_input: bool = True,
    ) -> str:
        """Write a query and return the first non-echo, non-empty line."""

        cmd_s = cmd.strip()
        if clear_input:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass

        self._write_line(cmd_s)
        if delay_s and delay_s > 0:
            time.sleep(float(delay_s))

        # Common echoes are exactly the command, or the command without spaces.
        echo_a = cmd_s.upper()
        echo_b = echo_a.replace(" ", "")

        for _ in range(max(1, int(read_lines))):
            raw = self.ser.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue
            u = line.upper().replace(" ", "")
            if u == echo_b or u.startswith(echo_b):
                continue
            return line
        return ""

    def system_error(self) -> str:
        """Query one entry from the instrument error queue.

        The 2831E/5491B manual documents :SYSTem:ERRor? for reading and clearing
        errors from the queue.
        """

        return self.query_line(":SYSTem:ERRor?", delay_s=0.0, read_lines=6, clear_input=True)

    def drain_errors(self, *, max_n: int = 16, log: bool = True) -> list[str]:
        """Drain the error queue (best-effort).

        Returns the collected error strings. Stops when a "no error" response
        is observed or after max_n reads.
        """

        out: list[str] = []
        for _ in range(max(1, int(max_n))):
            line = (self.system_error() or "").strip()
            if not line:
                break
            out.append(line)
            u = line.upper()
            # Typical SCPI: "0,No error"
            if u.startswith("0") or ("NO ERROR" in u):
                break

        if log and out:
            preview = " | ".join(out[:4])
            if len(out) > 4:
                preview += " | ..."
            self.log(f"[mmeter] SYST:ERR? -> {preview}")
        return out

    def fetch_values(
        self,
        cmd: str = ":FETCh?",
        *,
        delay_s: float = 0.0,
        read_lines: int = 6,
    ) -> FetchResult:
        """Fetch primary/secondary readings.

        Dual-display fetch often returns CSV ("primary,secondary"). We parse
        the first two floats we see.
        """

        line = self.query_line(cmd, delay_s=delay_s, read_lines=read_lines, clear_input=True)
        if not line:
            return FetchResult(None, None, "")
        nums = _extract_floats(line)
        if not nums:
            return FetchResult(None, None, line)

        primary = nums[0]
        secondary = nums[1] if len(nums) > 1 else None

        # Some firmware variants report "9.9E37" for overload.
        for v in (primary, secondary):
            if v is None:
                continue
            if abs(float(v)) > 1e36:
                # represent overload as NaN
                if v == primary:
                    primary = math.nan
                else:
                    secondary = math.nan

        return FetchResult(primary, secondary, line)

    # Back-compat: earlier PiPAT code called this helper.
    # Return a tuple so callers can do: p, s, raw = query_values(...)
    def query_values(
        self,
        cmd: str = ":FETCh?",
        *,
        delay_s: float = 0.0,
        read_lines: int = 6,
    ) -> tuple[Optional[float], Optional[float], str]:
        r = self.fetch_values(cmd, delay_s=delay_s, read_lines=read_lines)
        return (r.primary, r.secondary, r.raw)
