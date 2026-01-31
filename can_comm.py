# can_comm.py

from __future__ import annotations

import struct
import subprocess
import threading
import time
from typing import Optional

import can

import config
from can_metrics import BusLoadMeter


def setup_can_interface(channel: str, bitrate: int, *, do_setup: bool = True, log_fn=print) -> Optional[can.BusABC]:
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
        log_fn(f"CAN Init Failed: {e}")
        return None


def shutdown_can_interface(channel: str, *, do_setup: bool = True) -> None:
    if not do_setup:
        return
    for cmd in (
        ["ip", "link", "set", channel, "down"],
        ["sudo", "ip", "link", "set", channel, "down"],
    ):
        try:
            subprocess.run(cmd, check=False, capture_output=True, text=True)
            break
        except FileNotFoundError:
            break


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


class OutgoingTxState:
    """Thread-safe container for outgoing CAN readback values.

    Device-side code updates this state whenever it polls instruments.
    A dedicated TX thread publishes the latest values on CAN at a fixed rate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meter_current_mA: Optional[int] = None
        self._load_volts_mV: Optional[int] = None
        self._load_current_mA: Optional[int] = None
        self._afg_offset_mV: Optional[int] = None
        self._afg_duty_pct: Optional[int] = None
        self._mrs_status: Optional[tuple[int, int, float]] = None  # (on, mode, out_val)
        self._mrs_input: Optional[float] = None

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

    def update_mrsignal_status(self, *, output_on: bool, output_select: int, output_value: float) -> None:
        with self._lock:
            self._mrs_status = (1 if output_on else 0, int(output_select) & 0xFF, float(output_value))

    def update_mrsignal_input(self, input_value: float) -> None:
        with self._lock:
            self._mrs_input = float(input_value)

    def snapshot(
        self,
    ) -> tuple[
        Optional[int],
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
    log_fn=print,
) -> None:
    """Publish outgoing readback frames at a fixed period (e.g., 50 ms)."""

    try:
        period_s = float(period_s)
    except Exception:
        period_s = 0.05

    if period_s <= 0:
        log_fn("TX thread disabled (period <= 0).")
        return

    log_fn(f"TX thread started (period={period_s*1000:.0f} ms).")

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

        (
            meter_current_mA,
            load_volts_mV,
            load_current_mA,
            afg_offset_mV,
            afg_duty_pct,
            mrs_status,
            mrs_input,
        ) = tx_state.snapshot()

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
                    log_fn(f"TX MMETER send error (count={err_count}): {e}")

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
                    log_fn(f"TX ELOAD send error (count={err_count}): {e}")

        if (afg_offset_mV is not None) and (afg_duty_pct is not None):
            try:
                off_mv = _i16_clamp(afg_offset_mV)
                duty_pct_i = max(0, min(100, int(afg_duty_pct)))
                payload = bytearray(struct.pack("<h", off_mv))
                payload.append(duty_pct_i & 0xFF)
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
                    log_fn(f"TX AFG_EXT send error (count={err_count}): {e}")

        # MrSignal readback (status + input)
        if mrs_status is not None:
            try:
                on_i, mode_i, out_val = mrs_status
                payload = bytearray(8)
                payload[0] = 1 if int(on_i) else 0
                payload[1] = int(mode_i) & 0xFF
                payload[2:6] = struct.pack("<f", float(out_val))
                msg = can.Message(
                    arbitration_id=int(getattr(config, "MRSIGNAL_READ_STATUS_ID", 0x0CFF0007)),
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
                    log_fn(f"TX MRSIGNAL_STATUS send error (count={err_count}): {e}")

        if mrs_input is not None:
            try:
                payload = bytearray(8)
                payload[0:4] = struct.pack("<f", float(mrs_input))
                msg = can.Message(
                    arbitration_id=int(getattr(config, "MRSIGNAL_READ_INPUT_ID", 0x0CFF0008)),
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
                    log_fn(f"TX MRSIGNAL_INPUT send error (count={err_count}): {e}")


def can_rx_loop(
    cbus: can.BusABC,
    cmd_queue,
    stop_event: threading.Event,
    watchdog,
    busload: BusLoadMeter | None = None,
    log_fn=print,
) -> None:
    """Read CAN frames and enqueue them for the device comms worker.

    This thread intentionally does *no* hardware I/O.
    """

    log_fn("CAN RX thread started.")
    drop = 0

    # Only control frames should be forwarded to the device thread.
    # This prevents unrelated bus chatter from filling the bounded queue and
    # causing control latency (or drops) when the bus is busy.
    ctrl_ids = {
        int(getattr(config, "RLY_CTRL_ID", 0)),
        int(getattr(config, "AFG_CTRL_ID", 0)),
        int(getattr(config, "AFG_CTRL_EXT_ID", 0)),
        int(getattr(config, "MMETER_CTRL_ID", 0)),
        int(getattr(config, "LOAD_CTRL_ID", 0)),
        int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)),
    }

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
        try:
            watchdog.mark("can")
        except Exception:
            pass

        # Ignore non-control frames to keep the control path responsive.
        if arb not in ctrl_ids:
            continue

        # Non-blocking enqueue; never stall CAN recv due to slow devices.
        try:
            cmd_queue.put_nowait((arb, data))
        except Exception:
            drop += 1
            if drop in (1, 10, 100) or (drop % 500 == 0):
                log_fn(f"CAN RX: command queue full; dropped {drop} frames")
