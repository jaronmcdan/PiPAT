# device_control.py

"""Device control workers (separate from CAN RX).

Goal
----
Keep CAN receive responsive by ensuring *no* instrument/GPIO I/O happens on the
CAN RX thread. The RX thread only parses CAN frames and updates a desired-state
object.

This module owns worker threads that apply the latest desired state to actual
hardware.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

import config
from dmm_5491b import MeterMode, legacy_range_code_to_value
from hardware import HardwareManager


def _log(msg: str) -> None:
    # Avoid importing dashboard/console here (keep device threads lightweight)
    print(msg, flush=True)


# -----------------------------
# Desired-state containers
# -----------------------------


@dataclass
class RelayDesired:
    drive_on: bool = False
    seq: int = 0


@dataclass
class AfgDesired:
    enable: bool = False
    shape_idx: int = 0
    freq_hz: int = 1000
    ampl_mVpp: int = 1000
    offset_mV: int = 0
    duty_pct: int = 50
    seq: int = 0


@dataclass
class EloadDesired:
    enable: int = 0
    mode_res: int = 0
    short: int = 0
    csetting_mA: int = 0
    rsetting_mOhm: int = 0
    seq: int = 0


@dataclass
class MmeterDesired:
    mode: int = 0
    range_code: int = 0
    range_value: Optional[float] = None  # None => AUTO
    seq: int = 0


@dataclass
class MrSignalDesired:
    enable: bool = False
    output_select: int = 1
    value: float = 0.0
    seq: int = 0


class DesiredControlState:
    """Thread-safe desired state, updated by CAN RX thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.relay = RelayDesired(drive_on=bool(getattr(config, "K1_IDLE_DRIVE", False)))
        self.afg = AfgDesired(enable=bool(getattr(config, "AFG_IDLE_OUTPUT_ON", False)))
        self.eload = EloadDesired(
            enable=1 if bool(getattr(config, "ELOAD_IDLE_INPUT_ON", False)) else 0,
            short=1 if bool(getattr(config, "ELOAD_IDLE_SHORT_ON", False)) else 0,
        )
        self.mmeter = MmeterDesired(mode=0, range_code=0, range_value=None)
        self.mrs = MrSignalDesired(enable=bool(getattr(config, "MRSIGNAL_IDLE_OUTPUT_ON", False)))

    # --- update helpers (called by CAN RX thread) ---
    def set_relay(self, drive_on: bool) -> int:
        with self._lock:
            if bool(self.relay.drive_on) != bool(drive_on):
                self.relay.drive_on = bool(drive_on)
                self.relay.seq += 1
            return int(self.relay.seq)

    def set_afg_primary(self, *, enable: bool, shape_idx: int, freq_hz: int, ampl_mVpp: int) -> int:
        with self._lock:
            changed = False
            if bool(self.afg.enable) != bool(enable):
                self.afg.enable = bool(enable)
                changed = True
            if int(self.afg.shape_idx) != int(shape_idx):
                self.afg.shape_idx = int(shape_idx) & 0xFF
                changed = True
            if int(self.afg.freq_hz) != int(freq_hz):
                self.afg.freq_hz = int(freq_hz)
                changed = True
            if int(self.afg.ampl_mVpp) != int(ampl_mVpp):
                self.afg.ampl_mVpp = int(ampl_mVpp)
                changed = True
            if changed:
                self.afg.seq += 1
            return int(self.afg.seq)

    def set_afg_ext(self, *, offset_mV: int, duty_pct: int) -> int:
        duty_pct = max(1, min(99, int(duty_pct)))
        with self._lock:
            changed = False
            if int(self.afg.offset_mV) != int(offset_mV):
                self.afg.offset_mV = int(offset_mV)
                changed = True
            if int(self.afg.duty_pct) != int(duty_pct):
                self.afg.duty_pct = int(duty_pct)
                changed = True
            if changed:
                self.afg.seq += 1
            return int(self.afg.seq)

    def set_eload(self, *, enable: int, mode_res: int, short: int, csetting_mA: int, rsetting_mOhm: int) -> int:
        with self._lock:
            changed = False
            if int(self.eload.enable) != int(enable):
                self.eload.enable = int(enable)
                changed = True
            if int(self.eload.mode_res) != int(mode_res):
                self.eload.mode_res = int(mode_res)
                changed = True
            if int(self.eload.short) != int(short):
                self.eload.short = int(short)
                changed = True
            if int(self.eload.csetting_mA) != int(csetting_mA):
                self.eload.csetting_mA = int(csetting_mA)
                changed = True
            if int(self.eload.rsetting_mOhm) != int(rsetting_mOhm):
                self.eload.rsetting_mOhm = int(rsetting_mOhm)
                changed = True
            if changed:
                self.eload.seq += 1
            return int(self.eload.seq)

    def set_mmeter(self, *, mode: int, range_code: int, range_value: Optional[float]) -> int:
        with self._lock:
            changed = False
            if int(self.mmeter.mode) != int(mode):
                self.mmeter.mode = int(mode) & 0xFF
                changed = True
            if int(self.mmeter.range_code) != int(range_code):
                self.mmeter.range_code = int(range_code) & 0xFF
                changed = True
            # Normalize float changes (None vs value)
            if range_value is None:
                if self.mmeter.range_value is not None:
                    self.mmeter.range_value = None
                    changed = True
            else:
                rv = float(range_value)
                if (self.mmeter.range_value is None) or (abs(float(self.mmeter.range_value) - rv) > 1e-12):
                    self.mmeter.range_value = rv
                    changed = True
            if changed:
                self.mmeter.seq += 1
            return int(self.mmeter.seq)

    def set_mrsignal(self, *, enable: bool, output_select: int, value: float) -> int:
        with self._lock:
            changed = False
            if bool(self.mrs.enable) != bool(enable):
                self.mrs.enable = bool(enable)
                changed = True
            if int(self.mrs.output_select) != int(output_select):
                self.mrs.output_select = int(output_select) & 0xFF
                changed = True
            v = float(value)
            if not math.isfinite(v):
                v = 0.0
            if abs(float(self.mrs.value) - v) > 1e-7:
                self.mrs.value = v
                changed = True
            if changed:
                self.mrs.seq += 1
            return int(self.mrs.seq)

    # --- snapshot helpers (called by device threads) ---
    def snapshot_relay(self) -> RelayDesired:
        with self._lock:
            return RelayDesired(drive_on=bool(self.relay.drive_on), seq=int(self.relay.seq))

    def snapshot_afg(self) -> AfgDesired:
        with self._lock:
            return AfgDesired(
                enable=bool(self.afg.enable),
                shape_idx=int(self.afg.shape_idx),
                freq_hz=int(self.afg.freq_hz),
                ampl_mVpp=int(self.afg.ampl_mVpp),
                offset_mV=int(self.afg.offset_mV),
                duty_pct=int(self.afg.duty_pct),
                seq=int(self.afg.seq),
            )

    def snapshot_eload(self) -> EloadDesired:
        with self._lock:
            return EloadDesired(
                enable=int(self.eload.enable),
                mode_res=int(self.eload.mode_res),
                short=int(self.eload.short),
                csetting_mA=int(self.eload.csetting_mA),
                rsetting_mOhm=int(self.eload.rsetting_mOhm),
                seq=int(self.eload.seq),
            )

    def snapshot_mmeter(self) -> MmeterDesired:
        with self._lock:
            return MmeterDesired(
                mode=int(self.mmeter.mode),
                range_code=int(self.mmeter.range_code),
                range_value=(None if self.mmeter.range_value is None else float(self.mmeter.range_value)),
                seq=int(self.mmeter.seq),
            )

    def snapshot_mrs(self) -> MrSignalDesired:
        with self._lock:
            return MrSignalDesired(
                enable=bool(self.mrs.enable),
                output_select=int(self.mrs.output_select),
                value=float(self.mrs.value),
                seq=int(self.mrs.seq),
            )


# -----------------------------
# Worker coordinator
# -----------------------------


class DeviceControlCoordinator:
    """Runs per-device control workers that apply desired state to hardware."""

    SHAPE_MAP = {0: "SIN", 1: "SQU", 2: "RAMP"}

    def __init__(self, hardware: HardwareManager, stop_event: threading.Event) -> None:
        self.hardware = hardware
        self.stop_event = stop_event
        self.state = DesiredControlState()

        # One event per device so a slow device (e.g., DMM mode change) doesn't
        # delay other device updates.
        self._ev_relay = threading.Event()
        self._ev_afg = threading.Event()
        self._ev_eload = threading.Event()
        self._ev_mmeter = threading.Event()
        self._ev_mrs = threading.Event()

        self._threads: list[threading.Thread] = []

    # ---- Public API used by CAN RX thread ----
    def request_k1_drive(self, drive_on: bool) -> None:
        self.state.set_relay(bool(drive_on))
        self._ev_relay.set()

    def request_afg_primary(self, *, enable: bool, shape_idx: int, freq_hz: int, ampl_mVpp: int) -> None:
        self.state.set_afg_primary(enable=enable, shape_idx=shape_idx, freq_hz=freq_hz, ampl_mVpp=ampl_mVpp)
        self._ev_afg.set()

    def request_afg_ext(self, *, offset_mV: int, duty_pct: int) -> None:
        self.state.set_afg_ext(offset_mV=offset_mV, duty_pct=duty_pct)
        self._ev_afg.set()

    def request_eload(self, *, enable: int, mode_res: int, short: int, csetting_mA: int, rsetting_mOhm: int) -> None:
        self.state.set_eload(enable=enable, mode_res=mode_res, short=short, csetting_mA=csetting_mA, rsetting_mOhm=rsetting_mOhm)
        self._ev_eload.set()

    def request_mmeter(self, *, mode: int, range_code: int, range_value: Optional[float]) -> None:
        self.state.set_mmeter(mode=mode, range_code=range_code, range_value=range_value)
        self._ev_mmeter.set()

    def request_mrsignal(self, *, enable: bool, output_select: int, value: float) -> None:
        self.state.set_mrsignal(enable=enable, output_select=output_select, value=value)
        self._ev_mrs.set()

    # ---- Idle helpers (used by watchdog.enforce) ----
    def set_k1_idle(self) -> None:
        self.request_k1_drive(bool(getattr(config, "K1_IDLE_DRIVE", False)))

    def apply_idle_eload(self) -> None:
        # Only safety-critical bits.
        snap = self.state.snapshot_eload()
        self.request_eload(
            enable=1 if bool(getattr(config, "ELOAD_IDLE_INPUT_ON", False)) else 0,
            mode_res=int(snap.mode_res),
            short=1 if bool(getattr(config, "ELOAD_IDLE_SHORT_ON", False)) else 0,
            csetting_mA=int(snap.csetting_mA),
            rsetting_mOhm=int(snap.rsetting_mOhm),
        )

    def apply_idle_afg(self) -> None:
        snap = self.state.snapshot_afg()
        self.request_afg_primary(
            enable=bool(getattr(config, "AFG_IDLE_OUTPUT_ON", False)),
            shape_idx=int(snap.shape_idx),
            freq_hz=int(snap.freq_hz),
            ampl_mVpp=int(snap.ampl_mVpp),
        )

    def apply_idle_mrsignal(self) -> None:
        snap = self.state.snapshot_mrs()
        self.request_mrsignal(
            enable=bool(getattr(config, "MRSIGNAL_IDLE_OUTPUT_ON", False)),
            output_select=int(snap.output_select),
            value=float(snap.value),
        )

    def apply_idle_all(self) -> None:
        self.set_k1_idle()
        self.apply_idle_eload()
        self.apply_idle_afg()
        self.apply_idle_mrsignal()

    # ---- Thread lifecycle ----
    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._relay_worker, name="dev-relay", daemon=True),
            threading.Thread(target=self._afg_worker, name="dev-afg", daemon=True),
            threading.Thread(target=self._eload_worker, name="dev-eload", daemon=True),
            threading.Thread(target=self._mmeter_worker, name="dev-mmeter", daemon=True),
            threading.Thread(target=self._mrs_worker, name="dev-mrs", daemon=True),
        ]
        for t in self._threads:
            t.start()

    # -----------------------------
    # Worker implementations
    # -----------------------------

    def _relay_worker(self) -> None:
        last_seq = -1
        while not self.stop_event.is_set():
            self._ev_relay.wait(timeout=0.2)
            self._ev_relay.clear()
            snap = self.state.snapshot_relay()
            if snap.seq == last_seq:
                continue
            last_seq = snap.seq
            try:
                self.hardware.set_k1_drive(bool(snap.drive_on))
            except Exception:
                pass

    def _afg_worker(self) -> None:
        last_seq = -1
        while not self.stop_event.is_set():
            self._ev_afg.wait(timeout=0.2)
            self._ev_afg.clear()
            snap = self.state.snapshot_afg()
            if snap.seq == last_seq:
                continue
            last_seq = snap.seq
            if not self.hardware.afg:
                continue
            try:
                shape_str = self.SHAPE_MAP.get(int(snap.shape_idx) & 0xFF, "SIN")
                with self.hardware.afg_lock:
                    # Enable
                    if bool(self.hardware.afg_output) != bool(snap.enable):
                        self.hardware.afg.write(f"SOUR1:OUTP {'ON' if snap.enable else 'OFF'}")
                        self.hardware.afg_output = bool(snap.enable)
                    # Shape
                    if int(self.hardware.afg_shape) != int(snap.shape_idx):
                        self.hardware.afg.write(f"SOUR1:FUNC {shape_str}")
                        self.hardware.afg_shape = int(snap.shape_idx)
                    # Frequency
                    if int(self.hardware.afg_freq) != int(snap.freq_hz):
                        self.hardware.afg.write(f"SOUR1:FREQ {int(snap.freq_hz)}")
                        self.hardware.afg_freq = int(snap.freq_hz)
                    # Amplitude
                    if int(self.hardware.afg_ampl) != int(snap.ampl_mVpp):
                        self.hardware.afg.write(f"SOUR1:AMPL {float(snap.ampl_mVpp) / 1000.0}")
                        self.hardware.afg_ampl = int(snap.ampl_mVpp)
                    # Offset
                    if int(self.hardware.afg_offset) != int(snap.offset_mV):
                        self.hardware.afg.write(f"SOUR1:VOLT:OFFS {float(snap.offset_mV) / 1000.0}")
                        self.hardware.afg_offset = int(snap.offset_mV)
                    # Duty
                    duty = max(1, min(99, int(snap.duty_pct)))
                    if int(self.hardware.afg_duty) != int(duty):
                        self.hardware.afg.write(f"SOUR1:SQU:DCYC {int(duty)}")
                        self.hardware.afg_duty = int(duty)
            except Exception:
                pass

    def _eload_worker(self) -> None:
        last_seq = -1
        while not self.stop_event.is_set():
            self._ev_eload.wait(timeout=0.2)
            self._ev_eload.clear()
            snap = self.state.snapshot_eload()
            if snap.seq == last_seq:
                continue
            last_seq = snap.seq
            if not self.hardware.e_load:
                continue
            try:
                with self.hardware.eload_lock:
                    if int(self.hardware.e_load_enabled) != int(snap.enable):
                        self.hardware.e_load.write("INP ON" if int(snap.enable) else "INP OFF")
                        self.hardware.e_load_enabled = int(snap.enable)

                    if int(self.hardware.e_load_mode) != int(snap.mode_res):
                        self.hardware.e_load.write("FUNC RES" if int(snap.mode_res) else "FUNC CURR")
                        self.hardware.e_load_mode = int(snap.mode_res)

                    if int(self.hardware.e_load_short) != int(snap.short):
                        self.hardware.e_load.write("INP:SHOR ON" if int(snap.short) else "INP:SHOR OFF")
                        self.hardware.e_load_short = int(snap.short)

                    if int(self.hardware.e_load_csetting) != int(snap.csetting_mA):
                        self.hardware.e_load.write(f"CURR {float(snap.csetting_mA) / 1000.0}")
                        self.hardware.e_load_csetting = int(snap.csetting_mA)

                    if int(self.hardware.e_load_rsetting) != int(snap.rsetting_mOhm):
                        self.hardware.e_load.write(f"RES {float(snap.rsetting_mOhm) / 1000.0}")
                        self.hardware.e_load_rsetting = int(snap.rsetting_mOhm)
            except Exception:
                pass

    def _mmeter_worker(self) -> None:
        last_seq = -1
        while not self.stop_event.is_set():
            self._ev_mmeter.wait(timeout=0.2)
            self._ev_mmeter.clear()
            snap = self.state.snapshot_mmeter()
            if snap.seq == last_seq:
                continue
            last_seq = snap.seq
            if getattr(self.hardware, "dmm", None) is None:
                # No driver; nothing to do (legacy fallback is intentionally removed from CAN thread).
                continue
            try:
                # Resolve range value: float override if present, else use legacy table.
                range_value = snap.range_value
                if range_value is None:
                    range_value = legacy_range_code_to_value(int(snap.mode), int(snap.range_code))

                with self.hardware.mmeter_lock:
                    if int(self.hardware.multi_meter_mode) != int(snap.mode):
                        self.hardware.dmm.set_mode(int(snap.mode))
                        self.hardware.multi_meter_mode = int(snap.mode)

                    prev_code = int(getattr(self.hardware, "multi_meter_range", 0)) & 0xFF
                    prev_val = float(getattr(self.hardware, "multi_meter_range_value", 0.0))
                    desired_val = float(range_value or 0.0)
                    if (prev_code != (int(snap.range_code) & 0xFF)) or (abs(prev_val - desired_val) > 1e-12):
                        self.hardware.dmm.set_range(int(snap.mode), range_value)
                        self.hardware.multi_meter_range = int(snap.range_code) & 0xFF
                        self.hardware.multi_meter_range_value = float(desired_val)
            except Exception:
                pass

    def _mrs_worker(self) -> None:
        last_seq = -1
        while not self.stop_event.is_set():
            self._ev_mrs.wait(timeout=0.2)
            self._ev_mrs.clear()
            snap = self.state.snapshot_mrs()
            if snap.seq == last_seq:
                continue
            last_seq = snap.seq
            if not getattr(self.hardware, "mrsignal", None):
                continue
            try:
                # Safety: ignore unknown modes (extend as needed)
                if int(snap.output_select) not in (0, 1, 4, 6):
                    continue
                self.hardware.set_mrsignal(
                    enable=bool(snap.enable),
                    output_select=int(snap.output_select),
                    value=float(snap.value),
                    max_v=float(getattr(config, "MRSIGNAL_MAX_V", 24.0)),
                    max_ma=float(getattr(config, "MRSIGNAL_MAX_MA", 24.0)),
                )
            except Exception:
                pass
