# device_comm.py

from __future__ import annotations

import queue
import struct
import time
from typing import Callable, Optional

import config
from hardware import HardwareManager


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

            # Mode changes can take time; keep them in the device thread.
            if self.hardware.multi_meter and (self.hardware.multi_meter_mode != meter_mode):
                try:
                    with self.hardware.mmeter_lock:
                        if meter_mode == 0:
                            self.hardware.multi_meter.write(b"FUNC VOLT:DC\n")
                        elif meter_mode == 1:
                            self.hardware.multi_meter.write(b"FUNC CURR:DC\n")
                            time.sleep(0.2)
                            # NOTE: this range is instrument-specific; keep as existing default
                            self.hardware.multi_meter.write(b"CURR:DC:RANG 5\n")
                        self.hardware.multi_meter_mode = meter_mode
                except Exception:
                    pass

            self.hardware.multi_meter_range = meter_range
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

    while not stop_event.is_set():
        try:
            arb, data = cmd_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        except Exception:
            continue

        try:
            # Update freshness *when we actually start processing* the command.
            if watchdog_mark_fn:
                try:
                    if int(arb) == int(config.RLY_CTRL_ID):
                        watchdog_mark_fn("k1")
                    elif int(arb) in (int(config.AFG_CTRL_ID), int(config.AFG_CTRL_EXT_ID)):
                        watchdog_mark_fn("afg")
                    elif int(arb) == int(config.MMETER_CTRL_ID):
                        watchdog_mark_fn("mmeter")
                    elif int(arb) == int(config.LOAD_CTRL_ID):
                        watchdog_mark_fn("eload")
                    elif int(arb) == int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)):
                        watchdog_mark_fn("mrsignal")
                except Exception:
                    pass

            proc.handle(int(arb), bytes(data))
        except Exception as e:
            log_fn(f"Device command error: {e}")

    if idle_on_stop:
        try:
            hardware.apply_idle_all()
        except Exception:
            pass
