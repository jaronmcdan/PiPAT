#!/usr/bin/env python3
# main.py

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

import can

import config
from can_metrics import BusLoadMeter
from dashboard import HAVE_RICH, build_dashboard, console
from hardware import HardwareManager
from dmm_5491b import MeterMode, MeterUnit
from device_control import DeviceControlCoordinator

try:
    from rich.live import Live
except Exception:
    Live = None


def _u16_clamp(x: int) -> int:
    if x < 0:
        return 0
    if x > 0xFFFF:
        return 0xFFFF
    return x


def _i16_clamp(x: int) -> int:
    if x < -32768:
        return -32768
    if x > 32767:
        return 32767
    return x


def _log(msg: str) -> None:
    (console.log if HAVE_RICH else print)(msg)


def setup_can_interface(channel: str, bitrate: int, *, do_setup: bool = True) -> Optional[can.BusABC]:
    """Bring up SocketCAN and open a python-can bus.

    If the interface is already configured/up, bus open will still succeed.
    """

    if do_setup:
        # Try without sudo first (works when running as root / in systemd).
        cmds = [
            ["ip", "link", "set", channel, "up", "type", "can", "bitrate", str(bitrate)],
            ["sudo", "ip", "link", "set", channel, "up", "type", "can", "bitrate", str(bitrate)],
        ]
        for cmd in cmds:
            try:
                res = subprocess.run(cmd, check=False, capture_output=True, text=True)
                if res.returncode == 0:
                    break
            except FileNotFoundError:
                # ip or sudo missing; continue to bus open attempt
                break

    try:
        return can.interface.Bus(interface="socketcan", channel=channel)
    except Exception as e:
        _log(f"CAN Init Failed: {e}")
        return None


def shutdown_can_interface(channel: str, *, do_setup: bool = True) -> None:
    if not do_setup:
        return
    for cmd in (["ip", "link", "set", channel, "down"], ["sudo", "ip", "link", "set", channel, "down"]):
        try:
            subprocess.run(cmd, check=False, capture_output=True, text=True)
            break
        except FileNotFoundError:
            break


class ControlWatchdog:
    """Tracks freshness of control messages and enforces idle behavior."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_seen: Dict[str, float] = {}
        self._timed_out: Dict[str, bool] = {
            "can": True,
            "k1": True,
            "eload": True,
            "afg": True,
            "mmeter": True,
            "mrsignal": True,
        }

        # Soft timeout threshold is the per-key timeout; hard timeout is
        # timeout + grace. We only enforce idle on hard timeout transitions.
        self._grace_s: float = float(getattr(config, "WATCHDOG_GRACE_SEC", 0.25))

        self._timeouts: Dict[str, float] = {
            "can": float(getattr(config, "CAN_TIMEOUT_SEC", float(config.CONTROL_TIMEOUT_SEC))),
            "k1": float(config.K1_TIMEOUT_SEC),
            "eload": float(config.ELOAD_TIMEOUT_SEC),
            "afg": float(config.AFG_TIMEOUT_SEC),
            "mmeter": float(config.MMETER_TIMEOUT_SEC),
            "mrsignal": float(getattr(config, "MRSIGNAL_TIMEOUT_SEC", float(config.CONTROL_TIMEOUT_SEC))),
        }

    def mark(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._last_seen[key] = now
            self._timed_out[key] = False

    def snapshot(self) -> Dict:
        now = time.monotonic()
        with self._lock:
            ages: Dict[str, Optional[float]] = {}
            states: Dict[str, str] = {}
            for k in self._timeouts.keys():
                if k in self._last_seen:
                    ages[k] = now - self._last_seen[k]
                else:
                    ages[k] = None

                timeout_s = float(self._timeouts.get(k, 0.0))
                grace_s = float(self._grace_s)
                age = ages[k]
                if age is None:
                    states[k] = "to"
                elif age > (timeout_s + grace_s):
                    states[k] = "to"
                elif age > timeout_s:
                    states[k] = "warn"
                else:
                    states[k] = "ok"
            return {
                "ages": ages,
                "states": states,
                "timed_out": dict(self._timed_out),
                "timeouts": dict(self._timeouts),
                "grace_s": float(self._grace_s),
            }

    def enforce(self, idle_target) -> None:
        now = time.monotonic()
        with self._lock:
            for key, timeout_s in self._timeouts.items():
                last = self._last_seen.get(key)
                if last is None:
                    # Never seen => consider timed out, but idle likely already applied.
                    self._timed_out[key] = True
                    continue

                age = now - last
                hard_timeout_s = float(timeout_s) + float(self._grace_s)
                if age > hard_timeout_s:
                    if not self._timed_out.get(key, False):
                        # Transition into timeout => apply idle once
                        self._timed_out[key] = True
                        if key == "can":
                            # Indicator only; we don't apply device idles on generic CAN silence.
                            pass
                        elif key == "k1":
                            try:
                                idle_target.set_k1_idle()
                            except Exception:
                                pass
                        elif key == "eload":
                            try:
                                idle_target.apply_idle_eload()
                            except Exception:
                                pass
                        elif key == "afg":
                            try:
                                idle_target.apply_idle_afg()
                            except Exception:
                                pass
                        elif key == "mmeter":
                            # Nothing safety-critical to command on timeout.
                            pass
                        elif key == "mrsignal":
                            try:
                                idle_target.apply_idle_mrsignal()
                            except Exception:
                                pass

                else:
                    self._timed_out[key] = False




class OutgoingTxState:
    """Thread-safe container for outgoing CAN readback values.

    The main thread updates this state whenever it successfully polls an instrument.
    A dedicated TX thread publishes the latest values on CAN at a fixed rate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Multimeter (legacy + extended)
        self._meter_current_mA: Optional[int] = None  # legacy current-only frame
        self._meter_ext: Optional[tuple[int, int, float, int]] = None  # (mode, unit, value, flags)

        # E-Load
        self._load_volts_mV: Optional[int] = None
        self._load_current_mA: Optional[int] = None

        # AFG
        self._afg_offset_mV: Optional[int] = None
        self._afg_duty_pct: Optional[int] = None

        # MrSignal
        self._mrs_status: Optional[tuple[int, int, float]] = None  # (on, mode, out_val)
        self._mrs_input: Optional[float] = None

    def update_meter_current(self, meter_current_mA: int) -> None:
        """Update the legacy current-only value (mA)."""
        with self._lock:
            self._meter_current_mA = int(meter_current_mA)

    def update_meter_ext(self, *, mode: int, unit: int, value: float, flags: int) -> None:
        """Update the extended meter frame (mode/unit/float/flags)."""
        with self._lock:
            self._meter_ext = (int(mode) & 0xFF, int(unit) & 0xFF, float(value), int(flags) & 0xFF)

    def update_eload(self, load_volts_mV: int, load_current_mA: int) -> None:
        with self._lock:
            self._load_volts_mV = int(load_volts_mV)
            self._load_current_mA = int(load_current_mA)

    def update_afg_ext(self, offset_mV: int, duty_pct: int) -> None:
        with self._lock:
            self._afg_offset_mV = int(offset_mV)
            self._afg_duty_pct = int(duty_pct)

    def update_mrsignal_status(self, *, output_on: bool, output_select: int, output_value: float) -> None:
        with self._lock:
            self._mrs_status = (1 if output_on else 0, int(output_select) & 0xFF, float(output_value))

    def update_mrsignal_input(self, input_value: float) -> None:
        with self._lock:
            self._mrs_input = float(input_value)

    def snapshot(self) -> tuple[
        Optional[int],
        Optional[tuple[int, int, float, int]],
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[tuple[int, int, float]],
        Optional[float],
    ]:
        with self._lock:
            return (
                self._meter_current_mA,
                self._meter_ext,
                self._load_volts_mV,
                self._load_current_mA,
                self._afg_offset_mV,
                self._afg_duty_pct,
                self._mrs_status,
                self._mrs_input,
            )





def can_tx_loop(
    cbus: can.BusABC,
    tx_state: OutgoingTxState,
    stop_event: threading.Event,
    period_s: float,
    busload: BusLoadMeter | None = None,
) -> None:
    """Publish outgoing readback frames at a fixed period (e.g., 50 ms)."""

    try:
        period_s = float(period_s)
    except Exception:
        period_s = 0.05

    if period_s <= 0:
        _log("TX thread disabled (period <= 0).")
        return

    _log(f"TX thread started (period={period_s*1000:.0f} ms).")

    next_t = time.monotonic()
    err_count = 0

    while not stop_event.is_set():
        now = time.monotonic()
        delay = next_t - now
        if delay > 0:
            stop_event.wait(timeout=delay)
            continue

        # Advance the schedule first; this avoids drift if send() takes time.
        next_t += period_s
        if next_t < now - (10.0 * period_s):
            next_t = now + period_s

        meter_current_mA, meter_ext, load_volts_mV, load_current_mA, afg_offset_mV, afg_duty_pct, mrs_status, mrs_input = tx_state.snapshot()

        # Send each frame independently; one failure should not block others.
        if meter_current_mA is not None:
            try:
                u16 = _u16_clamp(meter_current_mA)
                msg = can.Message(
                    arbitration_id=int(config.MMETER_READ_ID),
                    data=list(int(u16).to_bytes(2, "little")) + [0] * 6,
                    is_extended_id=True,
                )
                cbus.send(msg)
                if busload:
                    try:
                        busload.record_tx(len(msg.data) if msg.data is not None else 0)
                    except Exception:
                        pass
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX MMETER send error (count={err_count}): {e}")

        if meter_ext is not None:
            try:
                mode_b, unit_b, value_f, flags_b = meter_ext
                payload = [int(mode_b) & 0xFF, int(unit_b) & 0xFF] + list(struct.pack("<f", float(value_f))) + [int(flags_b) & 0xFF, 0]
                msg = can.Message(
                    arbitration_id=int(getattr(config, "MMETER_READ_EXT_ID", 0x0CFF0009)),
                    data=payload,
                    is_extended_id=True,
                )
                cbus.send(msg)
                if busload:
                    try:
                        busload.record_tx(len(msg.data) if msg.data is not None else 0)
                    except Exception:
                        pass
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX MMETER_EXT send error (count={err_count}): {e}")


        if (load_volts_mV is not None) and (load_current_mA is not None):
            try:
                v_u16 = _u16_clamp(load_volts_mV)
                i_u16 = _u16_clamp(load_current_mA)
                data = (
                    list(int(v_u16).to_bytes(2, "little"))
                    + list(int(i_u16).to_bytes(2, "little"))
                    + [0] * 4
                )
                msg = can.Message(arbitration_id=int(config.ELOAD_READ_ID), data=data, is_extended_id=True)
                cbus.send(msg)
                if busload:
                    try:
                        busload.record_tx(len(msg.data) if msg.data is not None else 0)
                    except Exception:
                        pass
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX ELOAD send error (count={err_count}): {e}")

        if (afg_offset_mV is not None) and (afg_duty_pct is not None):
            try:
                off_mv = _i16_clamp(afg_offset_mV)
                duty_pct = max(0, min(100, int(afg_duty_pct)))
                payload = bytearray(struct.pack("<h", off_mv))
                payload.append(duty_pct & 0xFF)
                payload.extend([0] * 5)
                msg = can.Message(arbitration_id=int(config.AFG_READ_EXT_ID), data=payload, is_extended_id=True)
                cbus.send(msg)
                if busload:
                    try:
                        busload.record_tx(len(msg.data) if msg.data is not None else 0)
                    except Exception:
                        pass
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX AFG_EXT send error (count={err_count}): {e}")
        # MrSignal readback (status + input)
        if mrs_status is not None:
            try:
                on_i, mode_i, out_val = mrs_status
                payload = bytearray(8)
                payload[0] = 1 if int(on_i) else 0
                payload[1] = int(mode_i) & 0xFF
                payload[2:6] = struct.pack("<f", float(out_val))
                msg = can.Message(arbitration_id=int(getattr(config, "MRSIGNAL_READ_STATUS_ID", 0x0CFF0007)),
                                  data=payload, is_extended_id=True)
                cbus.send(msg)
                if busload:
                    try:
                        busload.record_tx(len(msg.data) if msg.data is not None else 0)
                    except Exception:
                        pass
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX MRSIGNAL_STATUS send error (count={err_count}): {e}")

        if mrs_input is not None:
            try:
                payload = bytearray(8)
                payload[0:4] = struct.pack("<f", float(mrs_input))
                msg = can.Message(arbitration_id=int(getattr(config, "MRSIGNAL_READ_INPUT_ID", 0x0CFF0008)),
                                  data=payload, is_extended_id=True)
                cbus.send(msg)
                if busload:
                    try:
                        busload.record_tx(len(msg.data) if msg.data is not None else 0)
                    except Exception:
                        pass
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX MRSIGNAL_INPUT send error (count={err_count}): {e}")




class TelemetryState:
    """Thread-safe snapshot of instrument telemetry for the dashboard and logs.

    The key goal is to keep the Rich TUI responsive by ensuring *no* slow
    instrument I/O happens on the render loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Fast measurements (updated at MEAS_POLL_PERIOD)
        # meter_current_mA is legacy (current-only). Extended meter fields are below.
        self.meter_current_mA: int = 0
        self.meter_mode_str: str = ""
        self.meter_value_str: str = ""
        self.meter_range_str: str = ""
        self.load_volts_mV: int = 0
        self.load_current_mA: int = 0

        # Slow status (updated at STATUS_POLL_PERIOD)
        self.load_stat_func: str = ""
        self.load_stat_curr: str = ""
        self.load_stat_res: str = ""
        self.load_stat_imp: str = ""
        self.load_stat_short: str = ""

        self.afg_freq_str: str = ""
        self.afg_ampl_str: str = ""
        self.afg_offset_str: str = "0"
        self.afg_duty_str: str = "50"
        self.afg_out_str: str = ""
        self.afg_shape_str: str = ""

        # MrSignal status
        self.mrs_id_str: str = ""
        self.mrs_out_str: str = ""
        self.mrs_mode_str: str = ""
        self.mrs_set_str: str = ""
        self.mrs_in_str: str = ""
        self.mrs_bo_str: str = ""

        # Timestamps (monotonic seconds)
        self.last_meas_ts: float = 0.0
        self.last_status_ts: float = 0.0

    def update_meas(self, *, meter_current_mA: int | None = None,
                    meter_mode_str: str | None = None,
                    meter_value_str: str | None = None,
                    meter_range_str: str | None = None,
                    load_volts_mV: int | None = None,
                    load_current_mA: int | None = None,
                    ts: float | None = None) -> None:
        with self._lock:
            if meter_current_mA is not None:
                self.meter_current_mA = int(meter_current_mA)
            if meter_mode_str is not None:
                self.meter_mode_str = str(meter_mode_str)
            if meter_value_str is not None:
                self.meter_value_str = str(meter_value_str)
            if meter_range_str is not None:
                self.meter_range_str = str(meter_range_str)
            if load_volts_mV is not None:
                self.load_volts_mV = int(load_volts_mV)
            if load_current_mA is not None:
                self.load_current_mA = int(load_current_mA)
            self.last_meas_ts = float(ts if ts is not None else time.monotonic())

    def update_status(self, *, load_stat_func: str | None = None,
                      load_stat_curr: str | None = None,
                      load_stat_res: str | None = None,
                      load_stat_imp: str | None = None,
                      load_stat_short: str | None = None,
                      afg_freq_str: str | None = None,
                      afg_ampl_str: str | None = None,
                      afg_offset_str: str | None = None,
                      afg_duty_str: str | None = None,
                      afg_out_str: str | None = None,
                      afg_shape_str: str | None = None,
                      mrs_id_str: str | None = None,
                      mrs_out_str: str | None = None,
                      mrs_mode_str: str | None = None,
                      mrs_set_str: str | None = None,
                      mrs_in_str: str | None = None,
                      mrs_bo_str: str | None = None,
                      ts: float | None = None) -> None:
        with self._lock:
            if load_stat_func is not None:
                self.load_stat_func = load_stat_func
            if load_stat_curr is not None:
                self.load_stat_curr = load_stat_curr
            if load_stat_res is not None:
                self.load_stat_res = load_stat_res
            if load_stat_imp is not None:
                self.load_stat_imp = load_stat_imp
            if load_stat_short is not None:
                self.load_stat_short = load_stat_short

            if afg_freq_str is not None:
                self.afg_freq_str = afg_freq_str
            if afg_ampl_str is not None:
                self.afg_ampl_str = afg_ampl_str
            if afg_offset_str is not None:
                self.afg_offset_str = afg_offset_str
            if afg_duty_str is not None:
                self.afg_duty_str = afg_duty_str
            if afg_out_str is not None:
                self.afg_out_str = afg_out_str
            if afg_shape_str is not None:
                self.afg_shape_str = afg_shape_str

            if mrs_id_str is not None:
                self.mrs_id_str = mrs_id_str
            if mrs_out_str is not None:
                self.mrs_out_str = mrs_out_str
            if mrs_mode_str is not None:
                self.mrs_mode_str = mrs_mode_str
            if mrs_set_str is not None:
                self.mrs_set_str = mrs_set_str
            if mrs_in_str is not None:
                self.mrs_in_str = mrs_in_str
            if mrs_bo_str is not None:
                self.mrs_bo_str = mrs_bo_str

            self.last_status_ts = float(ts if ts is not None else time.monotonic())

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "meter_current_mA": int(self.meter_current_mA),
                "meter_mode_str": str(self.meter_mode_str),
                "meter_value_str": str(self.meter_value_str),
                "meter_range_str": str(self.meter_range_str),
                "load_volts_mV": int(self.load_volts_mV),
                "load_current_mA": int(self.load_current_mA),
                "load_stat_func": str(self.load_stat_func),
                "load_stat_curr": str(self.load_stat_curr),
                "load_stat_res": str(self.load_stat_res),
                "load_stat_imp": str(self.load_stat_imp),
                "load_stat_short": str(self.load_stat_short),
                "afg_freq_str": str(self.afg_freq_str),
                "afg_ampl_str": str(self.afg_ampl_str),
                "afg_offset_str": str(self.afg_offset_str),
                "afg_duty_str": str(self.afg_duty_str),
                "afg_out_str": str(self.afg_out_str),
                "afg_shape_str": str(self.afg_shape_str),
                "mrs_id_str": str(self.mrs_id_str),
                "mrs_out_str": str(self.mrs_out_str),
                "mrs_mode_str": str(self.mrs_mode_str),
                "mrs_set_str": str(self.mrs_set_str),
                "mrs_in_str": str(self.mrs_in_str),
                "mrs_bo_str": str(self.mrs_bo_str),
                "last_meas_ts": float(self.last_meas_ts),
                "last_status_ts": float(self.last_status_ts),
            }


def instrument_poll_loop(
    hardware: HardwareManager,
    tx_state: OutgoingTxState,
    telemetry: TelemetryState,
    stop_event: threading.Event,
    status_period_s: float,
    meas_period_s: float,
) -> None:
    """Poll instruments in the background so the UI render loop stays snappy."""

    try:
        status_period_s = float(status_period_s)
    except Exception:
        status_period_s = 1.0
    if status_period_s <= 0:
        status_period_s = 1.0

    try:
        meas_period_s = float(meas_period_s)
    except Exception:
        meas_period_s = 0.2
    if meas_period_s <= 0:
        meas_period_s = 0.2

    _log(f"Instrument poll thread started (meas={meas_period_s:.3f}s, status={status_period_s:.3f}s).")

    last_status = 0.0
    last_mrs = 0.0
    next_meas = time.monotonic()

    while not stop_event.is_set():
        now_m = time.monotonic()

        # --- Fast measurements (tight loop) ---
        if now_m >= next_meas:
            next_meas += meas_period_s
            # avoid runaway if we get stalled for a while
            if next_meas < now_m - (10.0 * meas_period_s):
                next_meas = now_m + meas_period_s

            meter_current_mA = None
            load_volts_mV = None
            load_current_mA = None

            # Multimeter read (5491B: VDC / IDC / FREQ / OHM)
            if hardware.multi_meter:
                try:
                    mode = int(getattr(hardware, "multi_meter_mode", 0)) & 0xFF

                    with hardware.mmeter_lock:
                        if getattr(hardware, "dmm", None) is not None:
                            reading = hardware.dmm.read_reading(mode)
                        else:
                            # Best-effort fallback (no echo filtering, no range support)
                            hardware.multi_meter.write(b"FETC?\n")
                            raw = hardware.multi_meter.readline()
                            resp = raw.decode("ascii", errors="replace").strip()
                            if resp:
                                v = float(resp)
                                # Assume the unit from mode
                                unit = MeterUnit.VOLT if mode == MeterMode.VDC else (
                                    MeterUnit.AMP if mode == MeterMode.IDC else (
                                        MeterUnit.HZ if mode == MeterMode.FREQ else (
                                            MeterUnit.OHM if mode == MeterMode.OHM else MeterUnit.UNKNOWN
                                        )
                                    )
                                )
                                reading = type("R", (), {"mode": mode, "unit": unit, "value": v, "flags_byte": 0x01})()
                            else:
                                reading = None

                    if reading is not None:
                        # Extended CAN readback (mode/unit/float/flags)
                        tx_state.update_meter_ext(
                            mode=int(getattr(reading, "mode", mode)),
                            unit=int(getattr(reading, "unit", MeterUnit.UNKNOWN)),
                            value=float(getattr(reading, "value", 0.0)),
                            flags=int(getattr(reading, "flags_byte", 0)) & 0xFF,
                        )

                        # Legacy current-only readback (mA) stays meaningful for IDC only.
                        meter_current_mA = 0
                        if int(getattr(reading, "mode", mode)) == MeterMode.IDC and bool(getattr(reading, "valid", True)):
                            meter_current_mA = int(round(float(getattr(reading, "value", 0.0)) * 1000.0))
                        tx_state.update_meter_current(meter_current_mA)

                        # Dashboard strings
                        mode_name = {0: "VDC", 1: "IDC", 2: "FREQ", 3: "OHM"}.get(int(getattr(reading, "mode", mode)), f"MODE{mode}")
                        unit_name = {
                            MeterUnit.VOLT: "V",
                            MeterUnit.AMP: "A",
                            MeterUnit.HZ: "Hz",
                            MeterUnit.OHM: "Ω",
                        }.get(int(getattr(reading, "unit", MeterUnit.UNKNOWN)), "")

                        val = float(getattr(reading, "value", 0.0))
                        # Print tighter for ohms and freq, standard for V/A
                        if int(getattr(reading, "mode", mode)) == MeterMode.FREQ:
                            meter_value_str = f"{val:.3f} {unit_name}"
                        elif int(getattr(reading, "mode", mode)) == MeterMode.OHM:
                            meter_value_str = f"{val:.3f} {unit_name}"
                        else:
                            meter_value_str = f"{val:.6f} {unit_name}"

                        rv = float(getattr(hardware, "multi_meter_range_value", 0.0))
                        rc = int(getattr(hardware, "multi_meter_range", 0)) & 0xFF
                        if (rc == 0) or (rv <= 0.0):
                            meter_range_str = "AUTO"
                        else:
                            # Range units track mode
                            r_unit = "V" if mode == MeterMode.VDC else ("A" if mode == MeterMode.IDC else ("Ω" if mode == MeterMode.OHM else ""))
                            meter_range_str = f"R={rv:g}{r_unit}"

                        telemetry.update_meas(
                            meter_current_mA=meter_current_mA,
                            meter_mode_str=mode_name,
                            meter_value_str=meter_value_str,
                            meter_range_str=meter_range_str,
                            ts=now_m,
                        )

                except Exception:
                    pass

# E-Load measurement
            if hardware.e_load:
                try:
                    with hardware.eload_lock:
                        v_str = hardware.e_load.query("MEAS:VOLT?").strip()
                        i_str = hardware.e_load.query("MEAS:CURR?").strip()
                    if v_str and i_str:
                        load_volts_mV = int(float(v_str) * 1000)
                        load_current_mA = int(float(i_str) * 1000)
                        tx_state.update_eload(load_volts_mV, load_current_mA)
                except Exception:
                    pass

            if (meter_current_mA is not None) or (load_volts_mV is not None) or (load_current_mA is not None):
                telemetry.update_meas(
                    meter_current_mA=meter_current_mA,
                    load_volts_mV=load_volts_mV,
                    load_current_mA=load_current_mA,
                    ts=now_m,
                )

        # --- Slow status poll (setpoints/mode) ---
        if (now_m - last_status) >= status_period_s:
            last_status = now_m

            load_stat_func = None
            load_stat_curr = None
            load_stat_imp = None
            load_stat_res = None
            load_stat_short = None

            afg_freq_str = None
            afg_ampl_str = None
            afg_out_str = None
            afg_shape_str = None
            afg_offset_str = None
            afg_duty_str = None

            if hardware.e_load:
                try:
                    with hardware.eload_lock:
                        load_stat_func = hardware.e_load.query("FUNC?").strip()
                        load_stat_curr = hardware.e_load.query("CURR?").strip()
                        load_stat_imp = hardware.e_load.query("INP?").strip()
                        load_stat_res = hardware.e_load.query("RES?").strip()
                        # Some loads have SHOR?; keep optional
                        try:
                            load_stat_short = hardware.e_load.query("INP:SHOR?").strip()
                        except Exception:
                            load_stat_short = ""
                except Exception:
                    pass

            if hardware.afg:
                try:
                    with hardware.afg_lock:
                        afg_freq_str = hardware.afg.query("SOUR1:FREQ?").strip()
                        afg_ampl_str = hardware.afg.query("SOUR1:AMPL?").strip()
                        afg_out_str = hardware.afg.query("SOUR1:OUTP?").strip()

                        is_actually_on = str(afg_out_str).strip().upper() in ["ON", "1"]
                        if hardware.afg_output != is_actually_on:
                            hardware.afg_output = is_actually_on

                        afg_shape_str = hardware.afg.query("SOUR1:FUNC?").strip()
                        afg_offset_str = hardware.afg.query("SOUR1:VOLT:OFFS?").strip()
                        afg_duty_str = hardware.afg.query("SOUR1:SQU:DCYC?").strip()

                        if afg_offset_str and afg_duty_str:
                            off_mv = _i16_clamp(int(float(afg_offset_str) * 1000))
                            duty_pct = max(0, min(100, int(float(afg_duty_str))))
                            tx_state.update_afg_ext(off_mv, duty_pct)
                except Exception:
                    pass


                        # MrSignal (MR2.0) status/input
            mrs_id_str = None
            mrs_out_str = None
            mrs_mode_str = None
            mrs_set_str = None
            mrs_in_str = None
            mrs_bo_str = None

            if getattr(hardware, "mrsignal", None):
                try:
                    poll_p = float(getattr(config, "MRSIGNAL_POLL_PERIOD", status_period_s))
                    if poll_p <= 0:
                        poll_p = status_period_s

                    if (now_m - last_mrs) >= poll_p:
                        last_mrs = now_m
                        with hardware.mrsignal_lock:
                            st = hardware.mrsignal.read_status()

                        # Update hardware cached fields for dashboard
                        hardware.mrsignal_id = st.device_id
                        hardware.mrsignal_output_on = bool(st.output_on) if st.output_on is not None else False
                        hardware.mrsignal_output_select = int(st.output_select or 0)
                        if st.output_value is not None:
                            hardware.mrsignal_output_value = float(st.output_value)
                        if st.input_value is not None:
                            hardware.mrsignal_input_value = float(st.input_value)
                        hardware.mrsignal_float_byteorder = str(st.float_byteorder or "DEFAULT")

                        mrs_id_str = str(st.device_id) if st.device_id is not None else "—"
                        mrs_out_str = "ON" if bool(st.output_on) else "OFF"
                        mrs_mode_str = st.mode_label

                        # Render set/input with units based on mode
                        if st.output_value is not None:
                            if int(st.output_select or 0) == 0:
                                mrs_set_str = f"{float(st.output_value):.4g} mA"
                            elif int(st.output_select or 0) == 4:
                                mrs_set_str = f"{float(st.output_value):.4g} mV"
                            else:
                                mrs_set_str = f"{float(st.output_value):.4g} V"
                        if st.input_value is not None:
                            if int(st.output_select or 0) == 0:
                                mrs_in_str = f"{float(st.input_value):.4g} mA"
                            elif int(st.output_select or 0) == 4:
                                mrs_in_str = f"{float(st.input_value):.4g} mV"
                            else:
                                mrs_in_str = f"{float(st.input_value):.4g} V"
                        mrs_bo_str = str(st.float_byteorder or "DEFAULT")

                        # CAN readback publisher state
                        try:
                            tx_state.update_mrsignal_status(
                                output_on=bool(st.output_on),
                                output_select=int(st.output_select or 0),
                                output_value=float(st.output_value or 0.0),
                            )
                            if st.input_value is not None:
                                tx_state.update_mrsignal_input(float(st.input_value))
                        except Exception:
                            pass
                except Exception:
                    pass
            telemetry.update_status(
                load_stat_func=load_stat_func,
                load_stat_curr=load_stat_curr,
                load_stat_res=load_stat_res,
                load_stat_imp=load_stat_imp,
                load_stat_short=load_stat_short,
                afg_freq_str=afg_freq_str,
                afg_ampl_str=afg_ampl_str,
                afg_out_str=afg_out_str,
                afg_shape_str=afg_shape_str,
                afg_offset_str=afg_offset_str,
                afg_duty_str=afg_duty_str,
                mrs_id_str=mrs_id_str,
                mrs_out_str=mrs_out_str,
                mrs_mode_str=mrs_mode_str,
                mrs_set_str=mrs_set_str,
                mrs_in_str=mrs_in_str,
                mrs_bo_str=mrs_bo_str,
                ts=now_m,
            )

        # Short wait to avoid pegging a core.
        stop_event.wait(timeout=0.01)



def _decode_mrsignal_control(data: bytes) -> Optional[tuple[bool, int, float]]:
    """Decode MrSignal control frame.

    Expected legacy layout (PiPAT):
      - byte0: bit0 = enable
      - byte1: output_select (0=mA, 1=V, 4=mV, 6=24V)
      - bytes2..5: IEEE754 float (little-endian), setpoint in units per output_select

    Some senders swap byte0/byte1; some send big-endian float. To preserve
    compatibility, we try both layouts and both float endiannesses and pick the
    first plausible interpretation.
    """
    if data is None or len(data) < 6:
        return None

    allowed = (0, 1, 4, 6)
    max_v = float(getattr(config, "MRSIGNAL_MAX_V", 24.0))
    max_ma = float(getattr(config, "MRSIGNAL_MAX_MA", 24.0))

    def plausible(sel: int, v: float) -> bool:
        import math
        if not math.isfinite(v):
            return False
        # Allow small negative noise but clamp later
        if v < -0.001:
            return False
        if sel == 0:
            lim = max_ma * 1.10
            return v <= lim
        if sel in (1, 6):
            lim = max_v * 1.10
            return v <= lim
        if sel == 4:
            lim = (max_v * 1000.0) * 1.10
            return v <= lim
        return False

    # candidate layouts: (enable, output_select, float_bytes)
    layouts = [
        (bool(data[0] & 0x01), int(data[1]) & 0xFF, data[2:6]),  # legacy
        (bool(data[1] & 0x01), int(data[0]) & 0xFF, data[2:6]),  # swapped enable/select
    ]

    # Parse float both LE and BE for each layout
    candidates: list[tuple[bool, int, float]] = []
    for en, sel, fb in layouts:
        if sel not in allowed:
            continue
        try:
            v_le = struct.unpack("<f", fb)[0]
            if plausible(sel, float(v_le)):
                candidates.append((en, sel, float(v_le)))
        except Exception:
            pass
        try:
            v_be = struct.unpack(">f", fb)[0]
            if plausible(sel, float(v_be)):
                candidates.append((en, sel, float(v_be)))
        except Exception:
            pass

    if not candidates:
        return None

    # If any candidate has enable==True, prefer those (common for set commands)
    for en, sel, v in candidates:
        if en:
            return (en, sel, v)
    return candidates[0]


def receive_can_messages(
    cbus: can.BusABC,
    hardware: HardwareManager,
    controls: DeviceControlCoordinator,
    stop_event: threading.Event,
    watchdog: ControlWatchdog,
    busload: BusLoadMeter | None = None,
) -> None:
    """Background thread to process incoming CAN commands.

    IMPORTANT: This thread must never perform instrument/GPIO I/O.
    It only parses frames, updates desired state, and returns to recv().
    """

    _log("Receiver thread started.")

    while not stop_event.is_set():
        try:
            message = cbus.recv(timeout=1.0)
        except Exception:
            continue
        if not message:
            continue

        arb = int(message.arbitration_id)
        data = bytes(message.data or b"")

        if busload:
            try:
                busload.record_rx(len(data))
            except Exception:
                pass

        # Any CAN traffic counts as "CAN is alive" regardless of message type.
        watchdog.mark("can")

        # Relay control (K1 direct drive)
        if arb == int(config.RLY_CTRL_ID):
            watchdog.mark("k1")
            if len(data) < 1:
                continue

            # CAN bit0 (K1)
            k1_is_1 = (data[0] & 0x01) == 0x01
            # Direct drive only (no DUT inference). Optional invert via K1_CAN_INVERT.
            drive = (not k1_is_1) if bool(getattr(config, "K1_CAN_INVERT", False)) else k1_is_1
            controls.request_k1_drive(bool(drive))
            continue

        # AFG Control (Primary)
        if arb == int(config.AFG_CTRL_ID):
            watchdog.mark("afg")
            if len(data) < 8:
                continue

            enable = data[0] != 0
            shape_idx = int(data[1]) & 0xFF
            freq = struct.unpack("<I", data[2:6])[0]
            ampl_mV = struct.unpack("<H", data[6:8])[0]

            # If AFG isn't present, ignore (but keep CAN RX snappy).
            if hardware.afg:
                controls.request_afg_primary(enable=bool(enable), shape_idx=int(shape_idx), freq_hz=int(freq), ampl_mVpp=int(ampl_mV))
            continue

        # AFG Control (Extended)
        if arb == int(config.AFG_CTRL_EXT_ID):
            watchdog.mark("afg")
            if len(data) < 3:
                continue

            offset_mV = struct.unpack("<h", data[0:2])[0]
            duty_cycle = int(data[2])

            if hardware.afg:
                controls.request_afg_ext(offset_mV=int(offset_mV), duty_pct=int(duty_cycle))
            continue

        # Multimeter control (5491B)
        # Control payload (8 bytes):
        #   byte0: mode  (0=VDC, 1=IDC, 2=FREQ, 3=OHM)
        #   byte1: legacy range code (0=AUTO, else best-effort table per mode)
        #   bytes2..5: optional float32 little-endian range value (units depend on mode);
        #             if non-zero, it overrides byte1.
        if arb == int(config.MMETER_CTRL_ID):
            watchdog.mark("mmeter")
            if len(data) < 2:
                continue

            meter_mode = int(data[0]) & 0xFF
            meter_range_code = int(data[1]) & 0xFF

            range_value = None  # None => AUTO (worker resolves legacy mapping)
            if len(data) >= 6:
                rv = bytes(data[2:6])
                if any(b != 0 for b in rv):
                    try:
                        range_value = float(struct.unpack("<f", rv)[0])
                    except Exception:
                        range_value = None

            controls.request_mmeter(mode=int(meter_mode), range_code=int(meter_range_code), range_value=range_value)
            continue

        # E-load control
        if arb == int(config.LOAD_CTRL_ID):
            watchdog.mark("eload")
            if len(data) < 6:
                continue

            first_byte = data[0]
            new_enable = 1 if (first_byte & 0x0C) == 0x04 else 0
            new_mode = 1 if (first_byte & 0x30) == 0x10 else 0
            new_short = 1 if (first_byte & 0xC0) == 0x40 else 0

            val_c = (data[3] << 8) | data[2]
            val_r = (data[5] << 8) | data[4]

            if hardware.e_load:
                controls.request_eload(enable=int(new_enable), mode_res=int(new_mode), short=int(new_short), csetting_mA=int(val_c), rsetting_mOhm=int(val_r))
            continue

        # MrSignal control (MR2.0)
        if arb == int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)):
            cmd = _decode_mrsignal_control(bytes(data))
            if cmd is None:
                continue
            enable, output_select, value = cmd

            # Mark watchdog only when we accept a well-formed command
            watchdog.mark("mrsignal")

            if not getattr(hardware, "mrsignal", None):
                continue

            if bool(getattr(config, "MRSIGNAL_CAN_DEBUG", False)):
                _log(f"[mrsignal] CAN cmd raw={list(data)} -> enable={enable} sel={output_select} value={value}")

            controls.request_mrsignal(enable=bool(enable), output_select=int(output_select), value=float(value))
            continue


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ROI Instrument Bridge")
    p.add_argument("--headless", action="store_true", help="Disable Rich TUI (better for systemd)")
    p.add_argument("--no-can-setup", action="store_true", help="Do not run 'ip link set ... up type can ...'")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    hardware = HardwareManager()
    stop_event = threading.Event()
    watchdog = ControlWatchdog()

    # CAN bus load estimator (dashboard)
    busload = BusLoadMeter(
        bitrate=int(config.CAN_BITRATE),
        window_s=float(getattr(config, 'CAN_BUS_LOAD_WINDOW_SEC', 1.0)),
        stuffing_factor=float(getattr(config, 'CAN_BUS_LOAD_STUFFING_FACTOR', 1.2)),
        overhead_bits=int(getattr(config, 'CAN_BUS_LOAD_OVERHEAD_BITS', 48)),
        smooth_alpha=float(getattr(config, 'CAN_BUS_LOAD_SMOOTH_ALPHA', 0.0)),
        enabled=bool(getattr(config, 'CAN_BUS_LOAD_ENABLE', True)),
    )

    # measurement vars
    meter_current_mA = 0
    load_volts_mV = 0
    load_current_mA = 0

    # outgoing CAN readback publisher state (sent by TX thread)
    tx_state = OutgoingTxState()

    # status vars
    load_stat_func, load_stat_curr, load_stat_imp, load_stat_res, load_stat_short = "", "", "", "", ""
    afg_freq_str, afg_ampl_str, afg_out_str, afg_shape_str = "", "", "", ""
    afg_offset_str, afg_duty_str = "0", "50"
    # Decide UI mode
    headless = bool(args.headless or config.ROI_HEADLESS or (not sys.stdout.isatty()) or (not HAVE_RICH) or (Live is None))

    try:
        hardware.initialize_devices()

        # Device I/O workers (separate from CAN RX)
        controls = DeviceControlCoordinator(hardware, stop_event)
        controls.start()

        if bool(config.APPLY_IDLE_ON_STARTUP):
            # Queue idles through device workers (keeps all device I/O off main/CAN threads).
            controls.apply_idle_all()

        cbus = setup_can_interface(
            config.CAN_CHANNEL,
            int(config.CAN_BITRATE),
            do_setup=bool(config.CAN_SETUP) and (not args.no_can_setup),
        )
        if not cbus:
            return 2

        receiver_thread = threading.Thread(
            target=receive_can_messages,
            args=(cbus, hardware, controls, stop_event, watchdog, busload),
            daemon=True,
        )
        receiver_thread.start()

        tx_thread = None
        if bool(getattr(config, 'CAN_TX_ENABLE', True)):
            try:
                period_ms = float(getattr(config, 'CAN_TX_PERIOD_MS', 50))
            except Exception:
                period_ms = 50.0
            if period_ms > 0:
                tx_thread = threading.Thread(
                    target=can_tx_loop,
                    args=(cbus, tx_state, stop_event, period_ms / 1000.0, busload),
                    daemon=True,
                )
                tx_thread.start()
            else:
                _log('CAN_TX_PERIOD_MS <= 0; TX rate regulation disabled.')

                
        # Start background instrument polling so the UI stays responsive even if instrument I/O blocks.
        try:
            status_period = float(getattr(config, "STATUS_POLL_PERIOD", 1.0))
        except Exception:
            status_period = 1.0
        try:
            meas_period = float(getattr(config, "MEAS_POLL_PERIOD", 0.2))
        except Exception:
            meas_period = 0.2
        try:
            dash_fps = int(getattr(config, "DASH_FPS", 15))
        except Exception:
            dash_fps = 15
        if dash_fps <= 0:
            dash_fps = 10
        
        telemetry = TelemetryState()
        poll_thread = threading.Thread(
            target=instrument_poll_loop,
            args=(hardware, tx_state, telemetry, stop_event, status_period, meas_period),
            daemon=True,
        )
        poll_thread.start()
        
        # Headless loop (no rich Live)
        if headless:
            _log("Running headless (no Rich TUI).")
            next_log = 0.0
            while True:
                now = time.time()
        
                # Enforce watchdog first so timed-out controls go idle promptly
                watchdog.enforce(controls)
        
                # Periodic log line
                if now >= next_log:
                    next_log = now + 5.0
                    wd = watchdog.snapshot()
                    snap = telemetry.snapshot()
        
                    k1_drive = False
                    try:
                        k1_drive = bool(hardware.get_k1_drive())
                    except Exception:
                        k1_drive = bool(getattr(hardware.relay, "is_lit", False))
        
                    try:
                        k1_level = hardware.get_k1_pin_level()
                    except Exception:
                        k1_level = None
        
                    gpio_str = "--" if k1_level is None else ("H" if bool(k1_level) else "L")
        
                    load_pct, rx_fps, tx_fps = busload.snapshot() if busload else (None, None, None)
                    bus_str = '--' if load_pct is None else f"{load_pct:.1f}%"

                    _log(
                        f"K1={'ON' if k1_drive else 'OFF'} GPIO={gpio_str} Bus={bus_str} "
                        f"Load={int(snap.get('load_volts_mV', 0))/1000:.3f}V {int(snap.get('load_current_mA', 0))/1000:.3f}A "
                        f"Meter={int(snap.get('meter_current_mA', 0))/1000:.3f}A "
                        f"WD={wd.get('timed_out')}"
                    )
        
                stop_event.wait(timeout=0.1)
        
        # Rich TUI loop
        else:
            with Live(console=console, screen=True, refresh_per_second=dash_fps) as live:
                render_period = 1.0 / float(dash_fps) if dash_fps > 0 else 0.1
                while True:
                    watchdog.enforce(controls)
                    snap = telemetry.snapshot()
        
                    bus_load_pct, bus_rx_fps, bus_tx_fps = busload.snapshot() if busload else (None, None, None)
                    renderable = build_dashboard(
                        hardware,
                        meter_current_mA=int(snap.get("meter_current_mA", 0)),
                        meter_mode_str=str(snap.get("meter_mode_str", "")),
                        meter_value_str=str(snap.get("meter_value_str", "")),
                        meter_range_str=str(snap.get("meter_range_str", "")),
                        load_volts_mV=int(snap.get("load_volts_mV", 0)),
                        load_current_mA=int(snap.get("load_current_mA", 0)),
                        load_stat_func=str(snap.get("load_stat_func", "")),
                        load_stat_curr=str(snap.get("load_stat_curr", "")),
                        load_stat_res=str(snap.get("load_stat_res", "")),
                        load_stat_imp=str(snap.get("load_stat_imp", "")),
                        load_stat_short=str(snap.get("load_stat_short", "")),
                        afg_freq_read=str(snap.get("afg_freq_str", "")),
                        afg_ampl_read=str(snap.get("afg_ampl_str", "")),
                        afg_offset_read=str(snap.get("afg_offset_str", "0")),
                        afg_duty_read=str(snap.get("afg_duty_str", "50")),
                        afg_out_read=str(snap.get("afg_out_str", "")),
                        afg_shape_read=str(snap.get("afg_shape_str", "")),
                        mrs_id=str(snap.get("mrs_id_str", "")),
                        mrs_out=str(snap.get("mrs_out_str", "")),
                        mrs_mode=str(snap.get("mrs_mode_str", "")),
                        mrs_set=str(snap.get("mrs_set_str", "")),
                        mrs_in=str(snap.get("mrs_in_str", "")),
                        mrs_bo=str(snap.get("mrs_bo_str", "")),
                        can_channel=config.CAN_CHANNEL,
                        can_bitrate=int(config.CAN_BITRATE),
                        status_poll_period=status_period,
                        bus_load_pct=bus_load_pct,
                        bus_rx_fps=bus_rx_fps,
                        bus_tx_fps=bus_tx_fps,
                        watchdog=watchdog.snapshot(),
                    )
                    live.update(renderable, refresh=True)
                    stop_event.wait(timeout=render_period)
        
    except KeyboardInterrupt:
        return 0
    finally:
        stop_event.set()
        try:
            if "receiver_thread" in locals():
                receiver_thread.join(timeout=2.0)
                if 'tx_thread' in locals() and tx_thread:
                    tx_thread.join(timeout=2.0)
        except Exception:
            pass

        try:
            if "cbus" in locals() and cbus:
                cbus.shutdown()
        except Exception:
            pass

        try:
            shutdown_can_interface(config.CAN_CHANNEL, do_setup=bool(config.CAN_SETUP) and (not args.no_can_setup))
        except Exception:
            pass

        try:
            hardware.close_devices()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
