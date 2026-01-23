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
        # Best-effort configuration: bring the link down, set bitrate/options,
        # tune txqueuelen, then bring it up.
        try:
            bitrate_s = str(int(bitrate))
        except Exception:
            bitrate_s = str(bitrate)

        restart_ms = str(int(getattr(config, 'CAN_RESTART_MS', 100)))
        txq = str(int(getattr(config, 'CAN_TXQUEUELEN', 512)))

        cmds = [
            ['ip', 'link', 'set', channel, 'down'],
            ['ip', 'link', 'set', channel, 'type', 'can', 'bitrate', bitrate_s, 'restart-ms', restart_ms],
            ['ip', 'link', 'set', channel, 'txqueuelen', txq],
            ['ip', 'link', 'set', channel, 'up'],
        ]

        for seq in (cmds, [['sudo'] + c for c in cmds]):
            ok = True
            for cmd in seq:
                try:
                    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
                    if res.returncode != 0:
                        ok = False
                        break
                except FileNotFoundError:
                    ok = False
                    break
            if ok:
                break

    try:
        bus: can.BusABC = can.interface.Bus(interface='socketcan', channel=channel)

        # Optional: wrap in a thread-safe bus when available.
        try:
            ThreadSafeBus = getattr(can, 'ThreadSafeBus', None)
            if ThreadSafeBus is not None:
                try:
                    bus = ThreadSafeBus(bus)
                except Exception:
                    pass
        except Exception:
            pass

        # Optional: apply RX filters for control frames only.
        if bool(getattr(config, 'CAN_RX_FILTERS_ENABLE', True)):
            try:
                filters = []
                for _id in (
                    int(config.RLY_CTRL_ID),
                    int(config.LOAD_CTRL_ID),
                    int(config.MMETER_CTRL_ID),
                    int(config.AFG_CTRL_ID),
                    int(config.AFG_CTRL_EXT_ID),
                ):
                    filters.append({'can_id': _id, 'can_mask': 0x1FFFFFFF, 'extended': True})
                bus.set_filters(filters)
            except Exception as e:
                _log(f"CAN filter set failed: {e}")

        return bus
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
            "k1": True,
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


class DesiredControlState:
    """Holds latest desired control values (from CAN) for slow/IO-bound devices.

    Key design goal: keep the CAN RX thread *fast* (no VISA/serial IO, no sleeps).
    We coalesce bursts of CAN commands into the latest desired state and apply them
    asynchronously in a worker loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()

        # Desired values (None means "unset / ignore")
        self._eload = None  # tuple(enable, mode, short, c_mA, r_mOhm)
        self._afg_primary = None  # tuple(enable, shape_idx, freq_hz, ampl_mVpp)
        self._afg_ext = None  # tuple(offset_mV, duty_pct)
        self._mmeter = None  # tuple(mode, range)

        self._dirty = set()

    def set_eload(self, enable: int, mode: int, short: int, c_setting: int, r_setting: int) -> None:
        with self._lock:
            self._eload = (int(enable), int(mode), int(short), int(c_setting), int(r_setting))
            self._dirty.add('eload')
            self._event.set()

    def set_afg_primary(self, enable: bool, shape_idx: int, freq: int, ampl_mV: int) -> None:
        with self._lock:
            self._afg_primary = (bool(enable), int(shape_idx), int(freq), int(ampl_mV))
            self._dirty.add('afg')
            self._event.set()

    def set_afg_ext(self, offset_mV: int, duty_pct: int) -> None:
        with self._lock:
            self._afg_ext = (int(offset_mV), int(duty_pct))
            self._dirty.add('afg_ext')
            self._event.set()

    def set_mmeter(self, mode: int, rng: int) -> None:
        with self._lock:
            self._mmeter = (int(mode), int(rng))
            self._dirty.add('mmeter')
            self._event.set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout=timeout)

    def consume(self) -> tuple[set[str], tuple]:
        """Atomically snapshot & clear dirty flags.

        Returns:
          dirty_keys, (eload, afg_primary, afg_ext, mmeter)
        """
        with self._lock:
            dirty = set(self._dirty)
            self._dirty.clear()
            # If nothing else pending, clear the event.
            if not self._dirty:
                self._event.clear()
            return dirty, (self._eload, self._afg_primary, self._afg_ext, self._mmeter)

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._dirty)


def _apply_eload(hardware: HardwareManager, desired: tuple[int, int, int, int, int]) -> None:
    if not hardware.e_load:
        return
    enable, mode, short, c_setting, r_setting = desired

    try:
        with hardware.eload_lock:
            if hardware.e_load_enabled != enable:
                hardware.e_load.write('INP ON' if enable else 'INP OFF')
                hardware.e_load_enabled = enable

            if hardware.e_load_mode != mode:
                hardware.e_load.write('FUNC RES' if mode else 'FUNC CURR')
                hardware.e_load_mode = mode

            if hardware.e_load_short != short:
                hardware.e_load.write('INP:SHOR ON' if short else 'INP:SHOR OFF')
                hardware.e_load_short = short

            # Only write setpoints when they change; SCPI writes can be slow on some adapters.
            if hardware.e_load_csetting != c_setting:
                hardware.e_load.write(f"CURR {c_setting/1000}")
                hardware.e_load_csetting = c_setting

            if hardware.e_load_rsetting != r_setting:
                hardware.e_load.write(f"RES {r_setting/1000}")
                hardware.e_load_rsetting = r_setting

    except Exception as e:
        _log(f"E-Load apply error: {e}")


def _apply_afg(hardware: HardwareManager, desired_primary, desired_ext) -> None:
    if not hardware.afg:
        return

    SHAPE_MAP = {0: 'SIN', 1: 'SQU', 2: 'RAMP'}

    try:
        with hardware.afg_lock:
            if desired_primary is not None:
                enable, shape_idx, freq, ampl_mV = desired_primary
                ampl_V = ampl_mV / 1000.0

                if hardware.afg_output != enable:
                    hardware.afg.write(f"SOUR1:OUTP {'ON' if enable else 'OFF'}")
                    hardware.afg_output = enable

                if hardware.afg_shape != shape_idx:
                    hardware.afg.write(f"SOUR1:FUNC {SHAPE_MAP.get(shape_idx, 'SIN')}")
                    hardware.afg_shape = shape_idx

                if hardware.afg_freq != freq:
                    hardware.afg.write(f"SOUR1:FREQ {freq}")
                    hardware.afg_freq = freq

                if hardware.afg_ampl != ampl_mV:
                    hardware.afg.write(f"SOUR1:AMPL {ampl_V}")
                    hardware.afg_ampl = ampl_mV

            if desired_ext is not None:
                offset_mV, duty_pct = desired_ext
                duty_pct = max(1, min(99, int(duty_pct)))
                offset_V = _i16_clamp(int(offset_mV)) / 1000.0

                if hardware.afg_offset != offset_mV:
                    hardware.afg.write(f"SOUR1:VOLT:OFFS {offset_V}")
                    hardware.afg_offset = offset_mV

                if hardware.afg_duty != duty_pct:
                    hardware.afg.write(f"SOUR1:SQU:DCYC {duty_pct}")
                    hardware.afg_duty = duty_pct

    except Exception as e:
        _log(f"AFG apply error: {e}")


def _apply_mmeter(hardware: HardwareManager, desired: tuple[int, int], pending: dict) -> None:
    """Apply multimeter mode changes with non-blocking deferred range set.

    Some meters require a short settle time after switching to current mode
    before accepting a range command. We avoid sleeping in the CAN RX thread and
    also avoid blocking other device applies by scheduling a deferred command.

    pending dict keys:
      - 'due_t': monotonic time when range command should be issued
      - 'cmd': bytes command to write
    """
    if not hardware.multi_meter:
        return

    meter_mode, meter_range = desired

    try:
        with hardware.mmeter_lock:
            if hardware.multi_meter_mode != meter_mode:
                if meter_mode == 0:
                    hardware.multi_meter.write(b"FUNC VOLT:DC\n")
                    hardware.multi_meter_mode = meter_mode
                    # Clear any deferred command
                    pending.pop('due_t', None)
                    pending.pop('cmd', None)
                elif meter_mode == 1:
                    hardware.multi_meter.write(b"FUNC CURR:DC\n")
                    hardware.multi_meter_mode = meter_mode
                    # Defer range set slightly (instrument-specific behavior)
                    pending['due_t'] = time.monotonic() + 0.20
                    pending['cmd'] = b"CURR:DC:RANG 5\n"

            # Keep range in state even if we don't map it to a SCPI command yet.
            hardware.multi_meter_range = meter_range

    except Exception as e:
        _log(f"MMeter apply error: {e}")


def control_apply_loop(hardware: HardwareManager, desired_state: DesiredControlState, stop_event: threading.Event) -> None:
    """Worker loop that applies desired control values to slow instruments."""

    _log('Control apply thread started.')

    pending_mmeter = {}

    # Short wait keeps latency down, but we don't spin when idle.
    wait_s = 0.02

    while not stop_event.is_set():
        # Wake on new controls or periodically to service deferred work.
        desired_state.wait(timeout=wait_s)

        dirty, (eload, afg_primary, afg_ext, mmeter) = desired_state.consume()

        # Apply deferred multimeter range command when due.
        if pending_mmeter.get('due_t') is not None:
            if time.monotonic() >= float(pending_mmeter['due_t']):
                cmd = pending_mmeter.get('cmd')
                if cmd and hardware.multi_meter:
                    try:
                        with hardware.mmeter_lock:
                            hardware.multi_meter.write(cmd)
                    except Exception:
                        pass
                pending_mmeter.pop('due_t', None)
                pending_mmeter.pop('cmd', None)

        # Apply coalesced updates.
        if 'eload' in dirty and eload is not None:
            _apply_eload(hardware, eload)

        if ('afg' in dirty) or ('afg_ext' in dirty):
            _apply_afg(hardware, afg_primary if 'afg' in dirty else None, afg_ext if 'afg_ext' in dirty else None)

        if 'mmeter' in dirty and mmeter is not None:
            _apply_mmeter(hardware, mmeter, pending_mmeter)


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

    send_timeout = None
    try:
        send_timeout = float(getattr(config, 'CAN_SEND_TIMEOUT_S', 0.01))
    except Exception:
        send_timeout = None

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
                cbus.send(msg, timeout=send_timeout)
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
                cbus.send(msg, timeout=send_timeout)
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
                cbus.send(msg, timeout=send_timeout)
            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100):
                    _log(f"TX AFG_EXT send error (count={err_count}): {e}")



def receive_can_messages(
    cbus: can.BusABC,
    hardware: HardwareManager,
    stop_event: threading.Event,
    watchdog: ControlWatchdog,
    desired_state: DesiredControlState,
) -> None:
    """Background thread to process incoming CAN commands.

    This thread must remain fast: no VISA/serial I/O and no sleeps.  We decode
    frames, update the coalesced desired state, and return to recv().
    """

    _log("Receiver thread started.")

    while not stop_event.is_set():
        try:
            message = cbus.recv(timeout=0.2)
        except Exception:
            continue
        if not message:
            continue

        arb = int(message.arbitration_id)
        data = bytes(message.data or b"")

        # Relay control (K1 direct drive) is fast enough to do inline.
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
            if len(data) < 8:
                continue

            enable = data[0] != 0
            shape_idx = int(data[1])
            freq = int(struct.unpack("<I", data[2:6])[0])
            ampl_mV = int(struct.unpack("<H", data[6:8])[0])

            desired_state.set_afg_primary(enable, shape_idx, freq, ampl_mV)
            continue

        # AFG Control (Extended)
        if arb == int(config.AFG_CTRL_EXT_ID):
            watchdog.mark("afg")
            if len(data) < 3:
                continue

            offset_mV = int(struct.unpack("<h", data[0:2])[0])
            duty_cycle = max(1, min(99, int(data[2])))

            desired_state.set_afg_ext(offset_mV, duty_cycle)
            continue

        # Multimeter control
        if arb == int(config.MMETER_CTRL_ID):
            watchdog.mark("mmeter")
            if len(data) < 2:
                continue

            meter_mode = int(data[0])
            meter_range = int(data[1])

            desired_state.set_mmeter(meter_mode, meter_range)
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

            desired_state.set_eload(new_enable, new_mode, new_short, val_c, val_r)
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

        desired_state = DesiredControlState()

        apply_thread = threading.Thread(
            target=control_apply_loop,
            args=(hardware, desired_state, stop_event),
            daemon=True,
        )
        apply_thread.start()

        receiver_thread = threading.Thread(
            target=receive_can_messages,
            args=(cbus, hardware, stop_event, watchdog, desired_state),
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
                if 'apply_thread' in locals() and apply_thread:
                    apply_thread.join(timeout=2.0)
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

