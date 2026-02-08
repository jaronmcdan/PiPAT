# mrsignal.py
from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from typing import Optional, Tuple

import serial  # pyserial
import minimalmodbus

# Holding register base: 40001 => address 0
REG_ID = 0                    # 40001 ushort R
REG_OUTPUT_ON = 20            # 40021 ushort R/W  (output enable)
REG_OUTPUT_SELECT = 21        # 40022 ushort R/W  (mode select)
REG_OUTPUT_VALUE_FLOAT = 30   # 40031 float  R/W  (set output value)
REG_INPUT_VALUE_FLOAT = 14    # 40015 float  R    (read input value)

OUTPUT_MODE_LABELS = {
    0: "mA",
    1: "V",
    2: "XMT",
    3: "PULSE",
    4: "mV",
    5: "R",
    6: "24V",
}


def call_compat(func, *args, **kwargs):
    """Call minimalmodbus methods with only the kwargs supported by the installed version."""
    sig = inspect.signature(func)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return func(*args, **filtered)


def available_byteorders():
    """Return list of (name, value) for byteorder constants available in this minimalmodbus."""
    names = (
        "BYTEORDER_BIG",
        "BYTEORDER_LITTLE",
        "BYTEORDER_BIG_SWAP",
        "BYTEORDER_LITTLE_SWAP",
        "BYTEORDER_ABCD",
    )
    out = []
    for n in names:
        if hasattr(minimalmodbus, n):
            out.append((n, getattr(minimalmodbus, n)))
    # De-dupe by value while keeping order
    seen = set()
    uniq = []
    for n, v in out:
        if v not in seen:
            uniq.append((n, v))
            seen.add(v)
    return uniq


def get_byteorder_by_name(name: str | None):
    if not name:
        return None
    if hasattr(minimalmodbus, name):
        return getattr(minimalmodbus, name)
    return None


def is_sane_float(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and abs(x) < 1e6


@dataclass
class MrSignalStatus:
    device_id: Optional[int] = None
    output_on: Optional[bool] = None
    output_select: Optional[int] = None
    output_value: Optional[float] = None
    input_value: Optional[float] = None
    float_byteorder: str = "DEFAULT"

    @property
    def mode_label(self) -> str:
        if self.output_select is None:
            return "—"
        return OUTPUT_MODE_LABELS.get(int(self.output_select), f"UNKNOWN({int(self.output_select)})")


class MrSignalClient:
    """Mr.Signal / LANYI MR2.0 Modbus RTU client via USB virtual COM port."""

    def __init__(
        self,
        port: str,
        slave_id: int = 1,
        baud: int = 9600,
        parity: str = "N",
        stopbits: int = 1,
        timeout_s: float = 0.5,
        *,
        float_byteorder: str | None = None,
        float_byteorder_auto: bool = False,
    ) -> None:
        self.port = port
        self.slave_id = int(slave_id)
        self.baud = int(baud)
        self.parity = str(parity).upper()
        self.stopbits = int(stopbits)
        self.timeout_s = float(timeout_s)
        self.float_byteorder = (float_byteorder or "").strip() or None
        self.float_byteorder_auto = bool(float_byteorder_auto)

        self.inst: Optional[minimalmodbus.Instrument] = None
        self._last_used_bo: str = "DEFAULT"

    def connect(self) -> None:
        inst = minimalmodbus.Instrument(self.port, self.slave_id, mode=minimalmodbus.MODE_RTU)
        inst.clear_buffers_before_each_transaction = True

        inst.serial.baudrate = self.baud
        inst.serial.parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}.get(
            self.parity, serial.PARITY_NONE
        )
        inst.serial.stopbits = serial.STOPBITS_ONE if self.stopbits == 1 else serial.STOPBITS_TWO
        inst.serial.timeout = self.timeout_s
        self.inst = inst

    def close(self) -> None:
        try:
            if self.inst and getattr(self.inst, "serial", None):
                self.inst.serial.close()
        except Exception:
            pass
        self.inst = None

    def _read_u16(self, reg_addr: int, *, signed: bool = False) -> int:
        if not self.inst:
            raise RuntimeError("MrSignal not connected")
        return int(call_compat(self.inst.read_register, reg_addr, 0, functioncode=3, signed=signed))

    def _write_u16(self, reg_addr: int, value: int, *, signed: bool = False) -> None:
        if not self.inst:
            raise RuntimeError("MrSignal not connected")
        call_compat(self.inst.write_register, reg_addr, int(value), functioncode=6, signed=signed)

    def _read_float(self, reg_addr: int) -> Tuple[float, str]:
        if not self.inst:
            raise RuntimeError("MrSignal not connected")

        # If we previously discovered a working byteorder during auto-detect,
        # try it first to avoid re-scanning the full set on every poll.
        if self.float_byteorder_auto and (not self.float_byteorder):
            prev = (self._last_used_bo or "DEFAULT").strip() or "DEFAULT"
            if prev != "DEFAULT":
                bo_prev = get_byteorder_by_name(prev)
                if bo_prev is not None:
                    try:
                        if hasattr(self.inst, "byteorder"):
                            self.inst.byteorder = bo_prev
                            v = float(call_compat(self.inst.read_float, reg_addr, functioncode=3, number_of_registers=2))
                        else:
                            v = float(call_compat(self.inst.read_float, reg_addr, functioncode=3, number_of_registers=2, byteorder=bo_prev))
                        if is_sane_float(v):
                            return v, prev
                    except Exception:
                        pass

        # Try configured byteorder first (if any)
        if self.float_byteorder:
            bo = get_byteorder_by_name(self.float_byteorder)
            if bo is not None:
                try:
                    if hasattr(self.inst, "byteorder"):
                        self.inst.byteorder = bo
                        v = float(call_compat(self.inst.read_float, reg_addr, functioncode=3, number_of_registers=2))
                        self._last_used_bo = self.float_byteorder
                        return v, self.float_byteorder
                    v = float(call_compat(self.inst.read_float, reg_addr, functioncode=3, number_of_registers=2, byteorder=bo))
                    self._last_used_bo = self.float_byteorder
                    return v, self.float_byteorder
                except Exception:
                    pass

        # Auto-detect if enabled
        if self.float_byteorder_auto:
            for name, bo in available_byteorders():
                try:
                    if hasattr(self.inst, "byteorder"):
                        self.inst.byteorder = bo
                        v = float(call_compat(self.inst.read_float, reg_addr, functioncode=3, number_of_registers=2))
                    else:
                        v = float(call_compat(self.inst.read_float, reg_addr, functioncode=3, number_of_registers=2, byteorder=bo))
                    if is_sane_float(v):
                        self._last_used_bo = name
                        return v, name
                except Exception:
                    continue

        # Fallback: whatever the installed minimalmodbus default is
        v = float(call_compat(self.inst.read_float, reg_addr, functioncode=3, number_of_registers=2))
        self._last_used_bo = "DEFAULT"
        return v, "DEFAULT"

    def _write_float(self, reg_addr: int, value: float) -> str:
        if not self.inst:
            raise RuntimeError("MrSignal not connected")

        if self.float_byteorder:
            bo = get_byteorder_by_name(self.float_byteorder)
            if bo is not None:
                if hasattr(self.inst, "byteorder"):
                    self.inst.byteorder = bo
                    call_compat(self.inst.write_float, reg_addr, float(value), functioncode=16, number_of_registers=2)
                    self._last_used_bo = self.float_byteorder
                    return self.float_byteorder
                call_compat(self.inst.write_float, reg_addr, float(value), functioncode=16, number_of_registers=2, byteorder=bo)
                self._last_used_bo = self.float_byteorder
                return self.float_byteorder

        call_compat(self.inst.write_float, reg_addr, float(value), functioncode=16, number_of_registers=2)
        self._last_used_bo = "DEFAULT"
        return "DEFAULT"

    def read_status(self) -> MrSignalStatus:
        dev_id = None
        out_on = None
        out_sel = None
        out_val = None
        in_val = None
        bo = self._last_used_bo or "DEFAULT"

        dev_id = self._read_u16(REG_ID, signed=False)
        out_on = bool(self._read_u16(REG_OUTPUT_ON, signed=False))
        out_sel = int(self._read_u16(REG_OUTPUT_SELECT, signed=False))

        try:
            out_val, bo = self._read_float(REG_OUTPUT_VALUE_FLOAT)
        except Exception:
            out_val = None
        try:
            in_val, bo2 = self._read_float(REG_INPUT_VALUE_FLOAT)
            bo = bo2 or bo
        except Exception:
            in_val = None

        return MrSignalStatus(
            device_id=dev_id,
            output_on=out_on,
            output_select=out_sel,
            output_value=out_val,
            input_value=in_val,
            float_byteorder=bo,
        )

    def set_output(self, *, enable: bool, output_select: int, value: float) -> None:
        """Set output select + float value + enable."""
        # Order chosen to minimize unexpected output while changing mode/value:
        #   - If disabling: disable output FIRST (fast safety action)
        #   - Then apply mode/value (optional but useful for preloading)
        #   - If enabling: enable output LAST
        if not bool(enable):
            self._write_u16(REG_OUTPUT_ON, 0, signed=False)

        self._write_u16(REG_OUTPUT_SELECT, int(output_select), signed=False)
        self._write_float(REG_OUTPUT_VALUE_FLOAT, float(value))

        if bool(enable):
            self._write_u16(REG_OUTPUT_ON, 1, signed=False)

    def set_enable(self, enable: bool) -> None:
        self._write_u16(REG_OUTPUT_ON, 1 if enable else 0, signed=False)
