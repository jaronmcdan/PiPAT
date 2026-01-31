# device_comm.py

from __future__ import annotations

import queue
import struct
import time
import math
from typing import Callable, Optional

import config
from hardware import HardwareManager
from bk5491b import (
    FUNC_TO_SCPI_CONF,
    FUNC_TO_SCPI_FUNC,
    FUNC_TO_RANGE_PREFIX_FUNC,
    FUNC_TO_NPLC_PREFIX_FUNC,
    FUNC_TO_REF_PREFIX_FUNC,
    MmeterFunc,
    SCPI_STYLE_CONF,
    SCPI_STYLE_FUNC,
    func_name,
)


class DeviceCommandProcessor:
    """Apply decoded *control* commands to physical devices.

    This module intentionally contains **no CAN I/O**. It receives (arb_id, data)
    tuples from a queue and performs the associated device writes.
    """

    SHAPE_MAP = {0: "SIN", 1: "SQU", 2: "RAMP"}

    def __init__(self, hardware: HardwareManager, *, log_fn: Callable[[str], None] = print):
        self.hardware = hardware
        self.log = log_fn

        # Cached MrSignal arbitration id with a fallback.
        self._mrsignal_ctrl_id = int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800))

    def _mmeter_write(self, cmd: str, *, delay_s: float = 0.0, clear_input: bool = False) -> None:
        """Write a SCPI command to the multimeter.

        Caller should hold hardware.mmeter_lock.
        """
        try:
            if getattr(self.hardware, "mmeter", None) is not None:
                # Use the robust helper if available.
                self.hardware.mmeter.write(cmd, delay_s=delay_s, clear_input=clear_input)
                return

            mm = getattr(self.hardware, "multi_meter", None)
            if not mm:
                return

            if clear_input:
                try:
                    mm.reset_input_buffer()
                except Exception:
                    pass

            mm.write((cmd.strip() + "\n").encode("ascii", errors="ignore"))
            try:
                mm.flush()
            except Exception:
                pass
            if delay_s and delay_s > 0:
                time.sleep(float(delay_s))
        except Exception as e:
            self.log(f"MMETER write error: {e}")

    def _mmeter_set_func(self, func: int) -> None:
        func_i = int(func) & 0xFF
        style = str(getattr(self.hardware, "mmeter_scpi_style", SCPI_STYLE_CONF) or SCPI_STYLE_CONF).strip().lower()
        if style == SCPI_STYLE_FUNC:
            cmd = FUNC_TO_SCPI_FUNC.get(func_i)
        else:
            # Default to CONF-style for 5491B dual-display command set.
            cmd = FUNC_TO_SCPI_CONF.get(func_i)

        if not cmd:
            self.log(f"MMETER: unsupported function enum {func_i} for style='{style}'")
            return

        self._mmeter_write(cmd, delay_s=0.10, clear_input=True)
        self.hardware.mmeter_func = func_i

    def handle(self, arb: int, data: bytes) -> None:
        """Handle one control frame."""

        # Relay control (K1 direct drive)
        if arb == int(config.RLY_CTRL_ID):
            if len(data) < 1:
                return

            # CAN bit0 (K1)
            k1_is_1 = (data[0] & 0x01) == 0x01

            # Direct drive only (no DUT inference). Optional invert via K1_CAN_INVERT.
            drive = (not k1_is_1) if bool(getattr(config, "K1_CAN_INVERT", False)) else k1_is_1
            self.hardware.set_k1_drive(bool(drive))
            return

        # AFG Control (Primary)
        if arb == int(config.AFG_CTRL_ID):
            if not self.hardware.afg or len(data) < 8:
                return

            enable = data[0] != 0
            shape_idx = data[1]
            freq = struct.unpack("<I", data[2:6])[0]
            ampl_mV = struct.unpack("<H", data[6:8])[0]
            ampl_V = ampl_mV / 1000.0

            try:
                with self.hardware.afg_lock:
                    if self.hardware.afg_output != enable:
                        self.hardware.afg.write(f"SOUR1:OUTP {'ON' if enable else 'OFF'}")
                        self.hardware.afg_output = enable
                    if self.hardware.afg_shape != shape_idx:
                        shape_str = self.SHAPE_MAP.get(shape_idx, "SIN")
                        self.hardware.afg.write(f"SOUR1:FUNC {shape_str}")
                        self.hardware.afg_shape = shape_idx
                    if self.hardware.afg_freq != freq:
                        self.hardware.afg.write(f"SOUR1:FREQ {freq}")
                        self.hardware.afg_freq = freq
                    if self.hardware.afg_ampl != ampl_mV:
                        self.hardware.afg.write(f"SOUR1:AMPL {ampl_V}")
                        self.hardware.afg_ampl = ampl_mV
            except Exception as e:
                self.log(f"AFG Control Error: {e}")
            return

        # AFG Control (Extended)
        if arb == int(config.AFG_CTRL_EXT_ID):
            if not self.hardware.afg or len(data) < 3:
                return

            offset_mV = struct.unpack("<h", data[0:2])[0]
            offset_V = offset_mV / 1000.0
            duty_cycle = int(data[2])
            duty_cycle = max(1, min(99, duty_cycle))

            try:
                with self.hardware.afg_lock:
                    if self.hardware.afg_offset != offset_mV:
                        self.hardware.afg.write(f"SOUR1:VOLT:OFFS {offset_V}")
                        self.hardware.afg_offset = offset_mV
                    if self.hardware.afg_duty != duty_cycle:
                        self.hardware.afg.write(f"SOUR1:SQU:DCYC {duty_cycle}")
                        self.hardware.afg_duty = duty_cycle
            except Exception as e:
                self.log(f"AFG Ext Error: {e}")
            return

        # Multimeter control
        if arb == int(config.MMETER_CTRL_ID):
            if len(data) < 2:
                return

            meter_mode = int(data[0])
            meter_range = int(data[1])

            # Keep legacy semantics but actually drive the instrument.
            if self.hardware.multi_meter and (self.hardware.multi_meter_mode != meter_mode):
                try:
                    with self.hardware.mmeter_lock:
                        if meter_mode == 0:
                            self._mmeter_set_func(int(MmeterFunc.VDC))
                        elif meter_mode == 1:
                            self._mmeter_set_func(int(MmeterFunc.IDC))
                        self.hardware.multi_meter_mode = meter_mode
                except Exception:
                    pass

            # Range byte historically wasn't applied; we now interpret 0 as autorange ON.
            # Non-zero values are stored but not mapped (instrument-specific).
            try:
                with self.hardware.mmeter_lock:
                    if int(meter_range) == 0:
                        style = str(getattr(self.hardware, "mmeter_scpi_style", SCPI_STYLE_CONF) or SCPI_STYLE_CONF).strip().lower()
                        if style == SCPI_STYLE_FUNC:
                            prefix = FUNC_TO_RANGE_PREFIX_FUNC.get(int(self.hardware.mmeter_func))
                            if prefix:
                                self._mmeter_write(f"{prefix}:RANGe:AUTO ON")
                                self.hardware.mmeter_autorange = True
                        else:
                            # CONF-style global autorange (primary display)
                            self._mmeter_write(":CONFigure:RANGe:AUTO 1")
                            self.hardware.mmeter_autorange = True
                    else:
                        self.hardware.mmeter_autorange = False
            except Exception:
                pass

            self.hardware.multi_meter_range = int(meter_range)
            return

        # Multimeter control (Extended)
        if arb == int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601)):
            if not self.hardware.multi_meter or len(data) < 1:
                return

            # Payload:
            #   byte0 = opcode
            #   byte1 = arg0
            #   byte2 = arg1
            #   byte3 = arg2
            #   bytes4..7 = float32 value (little endian)
            op = int(data[0]) & 0xFF
            arg0 = int(data[1]) & 0xFF if len(data) > 1 else 0
            arg1 = int(data[2]) & 0xFF if len(data) > 2 else 0
            arg2 = int(data[3]) & 0xFF if len(data) > 3 else 0
            fval = 0.0
            if len(data) >= 8:
                try:
                    fval = float(struct.unpack("<f", data[4:8])[0])
                except Exception:
                    fval = 0.0

            # Convention: arg0 == 0xFF means "apply to current function".
            tgt_func = int(self.hardware.mmeter_func) if arg0 == 0xFF else int(arg0)

            try:
                with self.hardware.mmeter_lock:
                    style = str(getattr(self.hardware, "mmeter_scpi_style", SCPI_STYLE_CONF) or SCPI_STYLE_CONF).strip().lower()
                    is_secondary = (int(arg2) & 0xFF) == 1

                    # Helpers for CONF-style @2 channel list formatting.
                    # If a numeric parameter is omitted, @2 must be sent as the *first* parameter,
                    # which means we need a leading space before ',@2'.
                    conf_no_value = " ,@2" if is_secondary else ""
                    conf_with_value = ",@2" if is_secondary else ""

                    if op == 0x01:  # SET_FUNCTION
                        # Always sets the *primary* function.
                        self._mmeter_set_func(tgt_func)
                        self.log(f"MMETER func -> {func_name(int(self.hardware.mmeter_func))}")

                    elif op == 0x02:  # SET_AUTORANGE (arg1=0/1)
                        on = bool(arg1)
                        if style == SCPI_STYLE_FUNC:
                            prefix = FUNC_TO_RANGE_PREFIX_FUNC.get(tgt_func)
                            if prefix:
                                self._mmeter_write(f"{prefix}:RANGe:AUTO {'ON' if on else 'OFF'}")
                                self.hardware.mmeter_autorange = on
                        else:
                            # CONF-style global autorange for primary/secondary display.
                            self._mmeter_write(f":CONFigure:RANGe:AUTO {1 if on else 0}{conf_no_value}")
                            self.hardware.mmeter_autorange = on

                    elif op == 0x03:  # SET_RANGE (float)
                        if not math.isfinite(float(fval)):
                            return
                        if style == SCPI_STYLE_FUNC:
                            prefix = FUNC_TO_RANGE_PREFIX_FUNC.get(tgt_func)
                            if prefix:
                                self._mmeter_write(f"{prefix}:RANGe {float(fval):g}")
                                self.hardware.mmeter_autorange = False
                                self.hardware.mmeter_range_value = float(fval)
                        else:
                            # CONF-style has no documented direct CONF:RANG <n> setter; range is part
                            # of the CONF:<FUNC> command (numeric value is the range).
                            base = FUNC_TO_SCPI_CONF.get(tgt_func)
                            if base:
                                # Disable autorange first (best-effort) so the range sticks.
                                self._mmeter_write(f":CONFigure:RANGe:AUTO 0{conf_no_value}")
                                self._mmeter_write(f"{base} {float(fval):g}{conf_with_value}")
                                self.hardware.mmeter_autorange = False
                                self.hardware.mmeter_range_value = float(fval)

                    elif op == 0x04:  # SET_NPLC (float)
                        # Clamp to sane values; 0.01 .. 100 is typical.
                        nplc = max(0.01, min(100.0, float(fval)))
                        if style == SCPI_STYLE_FUNC:
                            prefix = FUNC_TO_NPLC_PREFIX_FUNC.get(tgt_func)
                            if prefix:
                                # Manual shows :NPLCycles; NPLC is its abbreviation.
                                self._mmeter_write(f"{prefix}:NPLCycles {nplc:g}")
                                self.hardware.mmeter_nplc = float(nplc)
                        else:
                            # CONF-style exposes measurement rate as SLOW|MED|FAST.
                            rate = "FAST" if nplc <= 0.1 else ("MED" if nplc <= 1.0 else "SLOW")
                            self._mmeter_write(f":CONFigure:DISPlay:RATE {rate}")
                            self.hardware.mmeter_nplc = float(nplc)

                    elif op == 0x05:  # SECONDARY_ENABLE (arg0=0/1)
                        on = bool(arg0)
                        if style == SCPI_STYLE_CONF:
                            if not on:
                                self._mmeter_write(":CONFigure:OFFDual")
                                self.hardware.mmeter_func2_enabled = False
                            else:
                                # Enabling secondary is implicit when you configure @2.
                                # If we already know the desired secondary function, apply it now.
                                base2 = FUNC_TO_SCPI_CONF.get(int(getattr(self.hardware, "mmeter_func2", int(MmeterFunc.VDC))))
                                if base2:
                                    self._mmeter_write(base2 + " ,@2")
                                    self.hardware.mmeter_func2_enabled = True
                                else:
                                    self.hardware.mmeter_func2_enabled = True
                        else:
                            # FUNC-style secondary display isn't standardized across firmware; ignore.
                            self.hardware.mmeter_func2_enabled = bool(on)

                    elif op == 0x06:  # SECONDARY_FUNCTION
                        func_i = tgt_func
                        if style == SCPI_STYLE_CONF:
                            base = FUNC_TO_SCPI_CONF.get(func_i)
                            if base:
                                self._mmeter_write(base + " ,@2")
                                self.hardware.mmeter_func2 = func_i
                                self.hardware.mmeter_func2_enabled = True
                        else:
                            # FUNC-style secondary display isn't standardized across firmware; ignore.
                            self.hardware.mmeter_func2 = func_i

                    elif op == 0x07:  # TRIG_SOURCE (arg0=0 IMM,1 BUS,2 MAN)
                        if style == SCPI_STYLE_FUNC:
                            src_map = {0: "IMM", 1: "BUS", 2: "MAN"}
                            src = src_map.get(int(arg0), "IMM")
                            self._mmeter_write(f":TRIGger:SOURce {src}")
                            self.hardware.mmeter_trig_source = int(arg0) & 0xFF
                        else:
                            # Not documented in CONF-style manual excerpt.
                            self.hardware.mmeter_trig_source = int(arg0) & 0xFF

                    elif op == 0x08:  # BUS_TRIGGER
                        self._mmeter_write("*TRG")

                    elif op == 0x09:  # RELATIVE_ENABLE (arg0=0/1)
                        on = bool(arg0)
                        if style == SCPI_STYLE_FUNC:
                            prefix = FUNC_TO_REF_PREFIX_FUNC.get(tgt_func)
                            if prefix:
                                self._mmeter_write(f"{prefix}:REFerence:STATe {'ON' if on else 'OFF'}")
                                self.hardware.mmeter_rel_enabled = on
                        else:
                            # Not documented in CONF-style manual excerpt.
                            self.hardware.mmeter_rel_enabled = on

                    elif op == 0x0A:  # RELATIVE_ACQUIRE
                        if style == SCPI_STYLE_FUNC:
                            prefix = FUNC_TO_REF_PREFIX_FUNC.get(tgt_func)
                            if prefix:
                                self._mmeter_write(f"{prefix}:REFerence:ACQuire")
                        else:
                            # Not documented in CONF-style manual excerpt.
                            pass

                    else:
                        # Unknown op; ignore.
                        if op != 0:
                            self.log(f"MMETER ext: unknown op=0x{op:02X} arg0={arg0} arg1={arg1} arg2={arg2}")

            except Exception as e:
                self.log(f"MMETER ext control error: {e}")

            return

        # E-load control
        if arb == int(config.LOAD_CTRL_ID):
            if not self.hardware.e_load or len(data) < 6:
                return

            first_byte = data[0]
            new_enable = 1 if (first_byte & 0x0C) == 0x04 else 0
            new_mode = 1 if (first_byte & 0x30) == 0x10 else 0
            new_short = 1 if (first_byte & 0xC0) == 0x40 else 0

            try:
                if self.hardware.e_load_enabled != new_enable:
                    self.hardware.e_load_enabled = new_enable
                    with self.hardware.eload_lock:
                        self.hardware.e_load.write("INP ON" if new_enable else "INP OFF")

                if self.hardware.e_load_mode != new_mode:
                    self.hardware.e_load_mode = new_mode
                    with self.hardware.eload_lock:
                        self.hardware.e_load.write("FUNC RES" if new_mode else "FUNC CURR")

                if self.hardware.e_load_short != new_short:
                    self.hardware.e_load_short = new_short
                    with self.hardware.eload_lock:
                        self.hardware.e_load.write("INP:SHOR ON" if new_short else "INP:SHOR OFF")

                val_c = (data[3] << 8) | data[2]
                if self.hardware.e_load_csetting != val_c:
                    self.hardware.e_load_csetting = val_c
                    with self.hardware.eload_lock:
                        self.hardware.e_load.write(f"CURR {val_c/1000}")

                val_r = (data[5] << 8) | data[4]
                if self.hardware.e_load_rsetting != val_r:
                    self.hardware.e_load_rsetting = val_r
                    with self.hardware.eload_lock:
                        self.hardware.e_load.write(f"RES {val_r/1000}")
            except Exception:
                pass
            return

        # MrSignal control (MR2.0)
        if arb == self._mrsignal_ctrl_id:
            if len(data) < 6:
                return
            if not getattr(self.hardware, "mrsignal", None):
                return

            enable = (data[0] & 0x01) == 0x01
            output_select = int(data[1])  # direct register value (0=mA, 1=V, 4=mV, 6=24V)
            try:
                value = struct.unpack("<f", data[2:6])[0]
            except Exception:
                return

            # Safety: ignore unknown modes (extend later if desired)
            if output_select not in (0, 1, 4, 6):
                return

            try:
                self.hardware.set_mrsignal(
                    enable=bool(enable),
                    output_select=int(output_select),
                    value=float(value),
                    max_v=float(getattr(config, "MRSIGNAL_MAX_V", 24.0)),
                    max_ma=float(getattr(config, "MRSIGNAL_MAX_MA", 24.0)),
                )
            except Exception as e:
                self.log(f"MrSignal Control Error: {e}")
            return


def device_command_loop(
    cmd_queue: "queue.Queue[tuple[int, bytes]]",
    hardware: HardwareManager,
    stop_event,
    *,
    log_fn: Callable[[str], None] = print,
    watchdog_mark_fn: Optional[Callable[[str], None]] = None,
    idle_on_stop: bool = True,
) -> None:
    """Process queued control frames and apply them to devices.

    The loop is resilient: any per-frame exception is contained.

    Parameters
    - cmd_queue: receives (arb_id, data) tuples from the CAN RX thread.
    - hardware: HardwareManager instance.
    - stop_event: threading.Event used to signal shutdown.
    """

    log_fn("Device command thread started.")
    proc = DeviceCommandProcessor(hardware, log_fn=log_fn)

    # For "knob-like" controls where only the *latest* value matters, we coalesce
    # bursts of frames to keep the device response snappy.
    #
    # This is especially important when the controller transmits at a higher rate
    # than the physical instruments can accept (SCPI/Modbus/serial writes are
    # comparatively slow).
    coalesce_ids = {
        int(config.RLY_CTRL_ID),
        int(config.AFG_CTRL_ID),
        int(config.AFG_CTRL_EXT_ID),
        int(config.MMETER_CTRL_ID),
        int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601)),
        int(config.LOAD_CTRL_ID),
        int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)),
    }

    # Apply in a stable order so dependent frames behave predictably.
    apply_order = [
        int(config.RLY_CTRL_ID),
        int(config.LOAD_CTRL_ID),
        int(config.AFG_CTRL_ID),
        int(config.AFG_CTRL_EXT_ID),
        int(config.MMETER_CTRL_ID),
        int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601)),
        int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)),
    ]

    while not stop_event.is_set():
        # Block for at least one command, then drain a small burst and coalesce.
        try:
            first_arb, first_data = cmd_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        except Exception:
            continue

        latest: dict[int, bytes] = {}

        def _record(a: int, d: bytes) -> None:
            a_i = int(a)
            if a_i in coalesce_ids:
                latest[a_i] = bytes(d)
            else:
                # Non-coalesced frames are processed immediately.
                latest[a_i] = bytes(d)

        _record(int(first_arb), bytes(first_data))

        # Drain anything currently queued without blocking.
        # This keeps latency low while still allowing bursts to be collapsed.
        for _ in range(1024):
            try:
                a, d = cmd_queue.get_nowait()
                _record(int(a), bytes(d))
            except queue.Empty:
                break
            except Exception:
                break

        # Apply in deterministic order; then apply any other IDs (unlikely).
        applied = set()
        for a in apply_order:
            if a in latest:
                try:
                    if watchdog_mark_fn:
                        if a == int(config.RLY_CTRL_ID):
                            watchdog_mark_fn("k1")
                        elif a in (int(config.AFG_CTRL_ID), int(config.AFG_CTRL_EXT_ID)):
                            watchdog_mark_fn("afg")
                        elif a in (int(config.MMETER_CTRL_ID), int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601))):
                            watchdog_mark_fn("mmeter")
                        elif a == int(config.LOAD_CTRL_ID):
                            watchdog_mark_fn("eload")
                        elif a == int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)):
                            watchdog_mark_fn("mrsignal")
                    proc.handle(int(a), bytes(latest[a]))
                except Exception as e:
                    log_fn(f"Device command error: {e}")
                applied.add(a)

        # Any other IDs (future extensions) are applied last.
        for a, d in latest.items():
            if a in applied:
                continue
            try:
                proc.handle(int(a), bytes(d))
            except Exception as e:
                log_fn(f"Device command error: {e}")

    if idle_on_stop:
        try:
            hardware.apply_idle_all()
        except Exception:
            pass
