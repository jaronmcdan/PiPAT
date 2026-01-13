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
from dashboard import HAVE_RICH, build_dashboard, console
from hardware import HardwareManager

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
            "relay": True,
            "eload": True,
            "afg": True,
            "mmeter": True,
        }

        self._timeouts: Dict[str, float] = {
            "k1": float(config.K1_TIMEOUT_SEC),
            "eload": float(config.ELOAD_TIMEOUT_SEC),
            "afg": float(config.AFG_TIMEOUT_SEC),
            "mmeter": float(config.MMETER_TIMEOUT_SEC),
        }

    def mark(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._last_seen[key] = now
            self._timed_out[key] = False

    def snapshot(self) -> Dict:
        now = time.time()
        with self._lock:
            ages: Dict[str, Optional[float]] = {}
            for k in self._timeouts.keys():
                if k in self._last_seen:
                    ages[k] = now - self._last_seen[k]
                else:
                    ages[k] = None
            return {
                "ages": ages,
                "timed_out": dict(self._timed_out),
                "timeouts": dict(self._timeouts),
            }

    def enforce(self, hardware: HardwareManager) -> None:
        now = time.time()
        with self._lock:
            for key, timeout_s in self._timeouts.items():
                last = self._last_seen.get(key)
                if last is None:
                    # Never seen => consider timed out, but idle likely already applied.
                    self._timed_out[key] = True
                    continue

                age = now - last
                if age > timeout_s:
                    if not self._timed_out.get(key, False):
                        # Transition into timeout => apply idle once
                        self._timed_out[key] = True
                        if key == "k1":
                            hardware.set_k1_idle()
                        elif key == "eload":
                            hardware.apply_idle_eload()
                        elif key == "afg":
                            hardware.apply_idle_afg()
                        elif key == "mmeter":
                            # Nothing safety-critical to command on timeout.
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
        self._meter_current_mA: Optional[int] = None
        self._load_volts_mV: Optional[int] = None
        self._load_current_mA: Optional[int] = None
        self._afg_offset_mV: Optional[int] = None
        self._afg_duty_pct: Optional[int] = None

    def update_meter_current(self, meter_current_mA: int) -> None:
        with self._lock:
            self._meter_current_mA = int(meter_current_mA)

    def update_eload(self, load_volts_mV: int, load_current_mA: int) -> None:
        with self._lock:
            self._load_volts_mV = int(load_volts_mV)
            self._load_current_mA = int(load_current_mA)

    def update_afg_ext(self, offset_mV: int, duty_pct: int) -> None:
        with self._lock:
            self._afg_offset_mV = int(offset_mV)
            self._afg_duty_pct = int(duty_pct)

    def snapshot(self) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int], Optional[int]]:
        with self._lock:
            return (
                self._meter_current_mA,
                self._load_volts_mV,
                self._load_current_mA,
                self._afg_offset_mV,
                self._afg_duty_pct,
            )


def can_tx_loop(
    cbus: can.BusABC,
    tx_state: OutgoingTxState,
    stop_event: threading.Event,
    period_s: float,
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

        meter_current_mA, load_volts_mV, load_current_mA, afg_offset_mV, afg_duty_pct = tx_state.snapshot()

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
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX MMETER send error (count={err_count}): {e}")

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
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX AFG_EXT send error (count={err_count}): {e}")


def receive_can_messages(cbus: can.BusABC, hardware: HardwareManager, stop_event: threading.Event, watchdog: ControlWatchdog) -> None:
    """Background thread to process incoming CAN commands."""

    _log("Receiver thread started.")

    SHAPE_MAP = {0: "SIN", 1: "SQU", 2: "RAMP"}

    while not stop_event.is_set():
        try:
            message = cbus.recv(timeout=1.0)
        except Exception:
            continue
        if not message:
            continue

        arb = int(message.arbitration_id)
        data = bytes(message.data or b"")

        # Relay control (K1 direct drive)
        if arb == int(config.RLY_CTRL_ID):
            watchdog.mark("k1")
            if len(data) < 1:
                continue

            # CAN bit0 (K1)
            k1_is_1 = (data[0] & 0x01) == 0x01

            # Direct drive only (no DUT inference). Optional invert via K1_CAN_INVERT.
            drive = (not k1_is_1) if bool(getattr(config, "K1_CAN_INVERT", False)) else k1_is_1
            hardware.set_k1_drive(bool(drive))

            continue

# AFG Control (Primary)
        if arb == int(config.AFG_CTRL_ID):
            watchdog.mark("afg")
            if not hardware.afg or len(data) < 8:
                continue

            enable = data[0] != 0
            shape_idx = data[1]
            freq = struct.unpack("<I", data[2:6])[0]
            ampl_mV = struct.unpack("<H", data[6:8])[0]
            ampl_V = ampl_mV / 1000.0

            try:
                with hardware.afg_lock:
                    if hardware.afg_output != enable:
                        hardware.afg.write(f"SOUR1:OUTP {'ON' if enable else 'OFF'}")
                        hardware.afg_output = enable
                    if hardware.afg_shape != shape_idx:
                        shape_str = SHAPE_MAP.get(shape_idx, "SIN")
                        hardware.afg.write(f"SOUR1:FUNC {shape_str}")
                        hardware.afg_shape = shape_idx
                    if hardware.afg_freq != freq:
                        hardware.afg.write(f"SOUR1:FREQ {freq}")
                        hardware.afg_freq = freq
                    if hardware.afg_ampl != ampl_mV:
                        hardware.afg.write(f"SOUR1:AMPL {ampl_V}")
                        hardware.afg_ampl = ampl_mV
            except Exception as e:
                _log(f"AFG Control Error: {e}")
            continue

        # AFG Control (Extended)
        if arb == int(config.AFG_CTRL_EXT_ID):
            watchdog.mark("afg")
            if not hardware.afg or len(data) < 3:
                continue

            offset_mV = struct.unpack("<h", data[0:2])[0]
            offset_V = offset_mV / 1000.0
            duty_cycle = int(data[2])
            duty_cycle = max(1, min(99, duty_cycle))

            try:
                with hardware.afg_lock:
                    if hardware.afg_offset != offset_mV:
                        hardware.afg.write(f"SOUR1:VOLT:OFFS {offset_V}")
                        hardware.afg_offset = offset_mV
                    if hardware.afg_duty != duty_cycle:
                        hardware.afg.write(f"SOUR1:SQU:DCYC {duty_cycle}")
                        hardware.afg_duty = duty_cycle
            except Exception as e:
                _log(f"AFG Ext Error: {e}")
            continue

        # Multimeter control
        if arb == int(config.MMETER_CTRL_ID):
            watchdog.mark("mmeter")
            if len(data) < 2:
                continue

            meter_mode = int(data[0])
            meter_range = int(data[1])

            if hardware.multi_meter and (hardware.multi_meter_mode != meter_mode):
                try:
                    with hardware.mmeter_lock:
                        if meter_mode == 0:
                            hardware.multi_meter.write(b"FUNC VOLT:DC\n")
                        elif meter_mode == 1:
                            hardware.multi_meter.write(b"FUNC CURR:DC\n")
                            time.sleep(0.2)
                            # NOTE: this range is instrument-specific; keep as existing default
                            hardware.multi_meter.write(b"CURR:DC:RANG 5\n")
                        hardware.multi_meter_mode = meter_mode
                except Exception:
                    pass

            hardware.multi_meter_range = meter_range
            continue

        # E-load control
        if arb == int(config.LOAD_CTRL_ID):
            watchdog.mark("eload")
            if not hardware.e_load or len(data) < 6:
                continue

            first_byte = data[0]
            new_enable = 1 if (first_byte & 0x0C) == 0x04 else 0
            new_mode = 1 if (first_byte & 0x30) == 0x10 else 0
            new_short = 1 if (first_byte & 0xC0) == 0x40 else 0

            try:
                if hardware.e_load_enabled != new_enable:
                    hardware.e_load_enabled = new_enable
                    with hardware.eload_lock:
                        hardware.e_load.write("INP ON" if new_enable else "INP OFF")

                if hardware.e_load_mode != new_mode:
                    hardware.e_load_mode = new_mode
                    with hardware.eload_lock:
                        hardware.e_load.write("FUNC RES" if new_mode else "FUNC CURR")

                if hardware.e_load_short != new_short:
                    hardware.e_load_short = new_short
                    with hardware.eload_lock:
                        hardware.e_load.write("INP:SHOR ON" if new_short else "INP:SHOR OFF")

                val_c = (data[3] << 8) | data[2]
                if hardware.e_load_csetting != val_c:
                    hardware.e_load_csetting = val_c
                    with hardware.eload_lock:
                        hardware.e_load.write(f"CURR {val_c/1000}")

                val_r = (data[5] << 8) | data[4]
                if hardware.e_load_rsetting != val_r:
                    hardware.e_load_rsetting = val_r
                    with hardware.eload_lock:
                        hardware.e_load.write(f"RES {val_r/1000}")
            except Exception:
                pass


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

    last_status_poll = 0.0
    STATUS_POLL_PERIOD = 1.0

    # Decide UI mode
    headless = bool(args.headless or config.ROI_HEADLESS or (not sys.stdout.isatty()) or (not HAVE_RICH) or (Live is None))

    try:
        hardware.initialize_devices()

        if bool(config.APPLY_IDLE_ON_STARTUP):
            hardware.apply_idle_all()

        cbus = setup_can_interface(
            config.CAN_CHANNEL,
            int(config.CAN_BITRATE),
            do_setup=bool(config.CAN_SETUP) and (not args.no_can_setup),
        )
        if not cbus:
            return 2

        receiver_thread = threading.Thread(
            target=receive_can_messages,
            args=(cbus, hardware, stop_event, watchdog),
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
                    args=(cbus, tx_state, stop_event, period_ms / 1000.0),
                    daemon=True,
                )
                tx_thread.start()
            else:
                _log('CAN_TX_PERIOD_MS <= 0; TX rate regulation disabled.')

        # Headless loop (no rich Live)
        if headless:
            _log("Running headless (no Rich TUI).")
            next_log = 0.0
            while True:
                now = time.time()

                # Enforce watchdog first so timed-out controls go idle promptly
                watchdog.enforce(hardware)

                # 1. Multimeter Read
                if hardware.multi_meter:
                    try:
                        with hardware.mmeter_lock:
                            hardware.multi_meter.write(b"FETC?\n")
                            raw = hardware.multi_meter.readline()
                        resp = raw.decode("ascii", errors="replace").strip()
                        if resp:
                            val = float(resp)
                            meter_current_mA = int(round(val * 1000))
                            tx_state.update_meter_current(meter_current_mA)
                    except Exception:
                        pass

                # 2. E-Load Meas
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

                # 3. Status Poll
                if now - last_status_poll >= STATUS_POLL_PERIOD:
                    last_status_poll = now

                    if hardware.e_load:
                        try:
                            with hardware.eload_lock:
                                load_stat_func = hardware.e_load.query("FUNC?").strip()
                                load_stat_curr = hardware.e_load.query("CURR?").strip()
                                load_stat_imp = hardware.e_load.query("INP?").strip()
                                load_stat_res = hardware.e_load.query("RES?").strip()
                        except Exception:
                            pass

                    if hardware.afg:
                        try:
                            with hardware.afg_lock:
                                afg_freq_str = hardware.afg.query("SOUR1:FREQ?").strip()
                                afg_ampl_str = hardware.afg.query("SOUR1:AMPL?").strip()
                                afg_out_str = hardware.afg.query("SOUR1:OUTP?").strip()
                                afg_shape_str = hardware.afg.query("SOUR1:FUNC?").strip()
                                afg_offset_str = hardware.afg.query("SOUR1:VOLT:OFFS?").strip()
                                afg_duty_str = hardware.afg.query("SOUR1:SQU:DCYC?").strip()

                                if afg_offset_str and afg_duty_str:
                                    off_mv = _i16_clamp(int(float(afg_offset_str) * 1000))
                                    duty_pct = max(0, min(100, int(float(afg_duty_str))))
                                    tx_state.update_afg_ext(off_mv, duty_pct)
                        except Exception:
                            pass

                # Periodic log line
                if now >= next_log:
                    next_log = now + 5.0
                    wd = watchdog.snapshot()

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

                    _log(
                        f"K1={'ON' if k1_drive else 'OFF'} GPIO={gpio_str} "
                        f"Load={load_volts_mV/1000:.3f}V {load_current_mA/1000:.3f}A "
                        f"Meter={meter_current_mA/1000:.3f}A "
                        f"WD={wd.get('timed_out')}"
                    )

                time.sleep(0.1)

        # Rich TUI loop
        else:
            with Live(console=console, screen=True, refresh_per_second=10) as live:
                while True:
                    now = time.time()

                    watchdog.enforce(hardware)

                    # 1. Multimeter Read
                    if hardware.multi_meter:
                        try:
                            with hardware.mmeter_lock:
                                hardware.multi_meter.write(b"FETC?\n")
                                raw = hardware.multi_meter.readline()
                            resp = raw.decode("ascii", errors="replace").strip()
                            if resp:
                                val = float(resp)
                                meter_current_mA = int(round(val * 1000))
                                tx_state.update_meter_current(meter_current_mA)
                        except Exception:
                            pass

                    # 2. E-Load Meas
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

                    # 3. Status Poll (Low Priority)
                    if now - last_status_poll >= STATUS_POLL_PERIOD:
                        last_status_poll = now

                        if hardware.e_load:
                            try:
                                with hardware.eload_lock:
                                    load_stat_func = hardware.e_load.query("FUNC?").strip()
                                    load_stat_curr = hardware.e_load.query("CURR?").strip()
                                    load_stat_imp = hardware.e_load.query("INP?").strip()
                                    load_stat_res = hardware.e_load.query("RES?").strip()
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

                    # 4. Update UI
                    renderable = build_dashboard(
                        hardware,
                        meter_current_mA=meter_current_mA,
                        load_volts_mV=load_volts_mV,
                        load_current_mA=load_current_mA,
                        load_stat_func=load_stat_func,
                        load_stat_curr=load_stat_curr,
                        load_stat_res=load_stat_res,
                        load_stat_imp=load_stat_imp,
                        load_stat_short=load_stat_short,
                        afg_freq_read=afg_freq_str,
                        afg_ampl_read=afg_ampl_str,
                        afg_offset_read=afg_offset_str,
                        afg_duty_read=afg_duty_str,
                        afg_out_read=afg_out_str,
                        afg_shape_read=afg_shape_str,
                        can_channel=config.CAN_CHANNEL,
                        can_bitrate=int(config.CAN_BITRATE),
                        status_poll_period=STATUS_POLL_PERIOD,
                        watchdog=watchdog.snapshot(),
                    )
                    live.update(renderable)
                    time.sleep(0.1)

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
