# can_comm.py

from __future__ import annotations

import struct
from dataclasses import dataclass
import subprocess
import threading
import time
import math
import queue
from typing import Optional, Callable

import can

from .. import config
from .metrics import BusLoadMeter

# Optional dashboard-only state (PAT switching matrix)
try:
    from ..core.pat_matrix import PatSwitchMatrixState
except Exception:  # pragma: no cover
    PatSwitchMatrixState = None  # type: ignore


def setup_can_interface(channel: str, bitrate: int, *, do_setup: bool = True, log_fn=print) -> Optional[can.BusABC]:
    """Open the configured CAN backend.

    Supported backends (see config.CAN_INTERFACE):
      - socketcan  : Linux SocketCAN netdev (e.g. can0)
      - rmcanview  : RM/Proemion CANview USB/RS232 gateways via serial (Byte Command Protocol)

    If the backend is already configured/up, bus open will still succeed.
    """

    iface = str(getattr(config, "CAN_INTERFACE", "socketcan") or "socketcan").strip().lower()

    # --- SocketCAN (default) ---
    if iface in ("socketcan", "socketcan_native", "socketcan_ctypes"):
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
            log_fn(f"CAN Init Failed (socketcan): {e}")
            return None

    # --- RM/Proemion CANview (Byte Command Protocol over serial) ---
    if iface in ("rmcanview", "rm-canview", "proemion"):
        try:
            from .rmcanview import RmCanViewBus

            serial_baud = int(getattr(config, "CAN_SERIAL_BAUD", 115200))
            clear_err = bool(getattr(config, "CAN_CLEAR_ERRORS_ON_INIT", True))
            return RmCanViewBus(
                channel,
                serial_baud=serial_baud,
                can_bitrate=int(bitrate),
                do_setup=bool(do_setup),
                clear_errors_on_init=clear_err,
                log_fn=log_fn,
            )
        except Exception as e:
            log_fn(f"CAN Init Failed (rmcanview): {e}")
            return None

    # --- Best-effort fallback to python-can interface names ---
    try:
        return can.interface.Bus(interface=iface, channel=channel, bitrate=int(bitrate))
    except Exception as e:
        log_fn(f"CAN Init Failed ({iface}): {e}")
        return None


def shutdown_can_interface(channel: str, *, do_setup: bool = True) -> None:
    """Tear down the configured CAN backend (if requested).

    For SocketCAN this brings the netdev down. For serial adapters (rmcanview)
    no explicit teardown is necessary.
    """
    if not do_setup:
        return

    iface = str(getattr(config, "CAN_INTERFACE", "socketcan") or "socketcan").strip().lower()
    if iface not in ("socketcan", "socketcan_native", "socketcan_ctypes"):
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
        # Extended multimeter readback (float32 primary/secondary) + status
        self._mmeter_primary: Optional[float] = None
        self._mmeter_secondary: Optional[float] = None
        self._mmeter_func: Optional[int] = None
        self._mmeter_flags: Optional[int] = None
        self._load_volts_mV: Optional[int] = None
        self._load_current_mA: Optional[int] = None
        self._afg_offset_mV: Optional[int] = None
        self._afg_duty_pct: Optional[int] = None
        self._mrs_status: Optional[tuple[int, int, float]] = None  # (on, mode, out_val)
        self._mrs_input: Optional[float] = None

    def update_meter_current(self, meter_current_mA: int) -> None:
        with self._lock:
            self._meter_current_mA = int(meter_current_mA)

    def clear_meter_current(self) -> None:
        """Stop transmitting legacy MMETER_READ_ID (prevents stale values)."""
        with self._lock:
            self._meter_current_mA = None

    def update_mmeter_values(self, primary: float | None, secondary: float | None = None) -> None:
        with self._lock:
            self._mmeter_primary = None if primary is None else float(primary)
            self._mmeter_secondary = None if secondary is None else float(secondary)

    def update_mmeter_status(self, *, func: int, flags: int) -> None:
        with self._lock:
            self._mmeter_func = int(func) & 0xFF
            self._mmeter_flags = int(flags) & 0xFF

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
        Optional[float],
        Optional[float],
        Optional[int],
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
                self._mmeter_primary,
                self._mmeter_secondary,
                self._mmeter_func,
                self._mmeter_flags,
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
    """Publish outgoing readback frames at a fixed period.

    Notes
    - ``period_s`` is the TX scheduler *tick* (resolution). This is typically
      0.05s (50 ms). Individual frames can be sent slower via per-frame
      ``CAN_TX_PERIOD_*_MS`` settings in roi.config.
    - This loop never touches instruments; it only publishes the latest values
      from ``OutgoingTxState``.
    """

    def _ms_to_s(ms: float, *, default_s: float) -> float:
        try:
            v = float(ms)
        except Exception:
            return float(default_s)
        if v <= 0:
            return 0.0
        return float(v) / 1000.0

    def _cfg_period_s(attr: str, *, default_s: float) -> float:
        try:
            ms = getattr(config, attr)
        except Exception:
            ms = None
        if ms is None:
            return float(default_s)
        return _ms_to_s(ms, default_s=float(default_s))

    try:
        tick_s = float(period_s)
    except Exception:
        tick_s = 0.05

    if tick_s <= 0:
        log_fn("TX thread disabled (period <= 0).")
        return

    # Optional "send on change" (still rate limited).
    send_on_change = bool(getattr(config, "CAN_TX_SEND_ON_CHANGE", False))
    min_change_s = _ms_to_s(float(getattr(config, "CAN_TX_SEND_ON_CHANGE_MIN_MS", 0)), default_s=0.0)

    @dataclass
    class _TxTask:
        name: str
        arbitration_id: int
        period_s: float
        present_fn: Callable[[tuple], bool]
        build_payload_fn: Callable[[tuple], Optional[bytes]]
        is_extended_id: bool = True

        # State
        present_last: bool = False
        last_payload: bytes | None = None
        last_sent: float = 0.0
        next_due: float = 0.0

        def mark_absent(self, now: float) -> None:
            self.present_last = False
            self.last_payload = None
            self.last_sent = 0.0
            # Force immediate send once the signal returns.
            self.next_due = float(now)

    # Build scheduler tasks (per-frame periods default to CAN_TX_PERIOD_MS).
    default_period_s = float(tick_s)

    tasks: list[_TxTask] = []

    def add_task(
        *,
        name: str,
        arb_id: int,
        period_attr: str,
        present_fn: Callable[[tuple], bool],
        build_payload_fn: Callable[[tuple], Optional[bytes]],
    ) -> None:
        p_s = _cfg_period_s(period_attr, default_s=default_period_s)
        if p_s <= 0:
            log_fn(f"TX {name} disabled ({period_attr} <= 0).")
            return
        tasks.append(
            _TxTask(
                name=name,
                arbitration_id=int(arb_id),
                period_s=float(p_s),
                present_fn=present_fn,
                build_payload_fn=build_payload_fn,
                is_extended_id=True,
                present_last=False,
                last_payload=None,
                last_sent=0.0,
                next_due=0.0,  # send ASAP once present
            )
        )

    # Snapshot tuple indices (to keep builder functions readable).
    # snapshot() returns:
    #   0 meter_current_mA
    #   1 mmeter_primary
    #   2 mmeter_secondary
    #   3 mmeter_func
    #   4 mmeter_flags
    #   5 load_volts_mV
    #   6 load_current_mA
    #   7 afg_offset_mV
    #   8 afg_duty_pct
    #   9 mrs_status
    #  10 mrs_input

    def _present_meter(snap: tuple) -> bool:
        return snap[0] is not None

    def _build_meter(snap: tuple) -> Optional[bytes]:
        mA = snap[0]
        if mA is None:
            return None
        u16 = _u16_clamp(int(mA))
        return int(u16).to_bytes(2, "little") + (b"\x00" * 6)

    def _present_mmeter_ext(snap: tuple) -> bool:
        return snap[1] is not None

    def _build_mmeter_ext(snap: tuple) -> Optional[bytes]:
        primary = snap[1]
        if primary is None:
            return None
        secondary = snap[2]
        p = float(primary)
        s = float("nan") if (secondary is None) else float(secondary)
        return struct.pack("<ff", p, s)

    def _present_mmeter_status(snap: tuple) -> bool:
        return (snap[3] is not None) and (snap[4] is not None)

    def _build_mmeter_status(snap: tuple) -> Optional[bytes]:
        func = snap[3]
        flags = snap[4]
        if func is None or flags is None:
            return None
        payload = bytearray(8)
        payload[0] = int(func) & 0xFF
        payload[1] = int(flags) & 0xFF
        return bytes(payload)

    def _present_eload(snap: tuple) -> bool:
        return (snap[5] is not None) and (snap[6] is not None)

    def _build_eload(snap: tuple) -> Optional[bytes]:
        v = snap[5]
        i = snap[6]
        if v is None or i is None:
            return None
        v_u16 = _u16_clamp(int(v))
        i_u16 = _u16_clamp(int(i))
        return (
            int(v_u16).to_bytes(2, "little")
            + int(i_u16).to_bytes(2, "little")
            + (b"\x00" * 4)
        )

    def _present_afg_ext(snap: tuple) -> bool:
        return (snap[7] is not None) and (snap[8] is not None)

    def _build_afg_ext(snap: tuple) -> Optional[bytes]:
        off = snap[7]
        duty = snap[8]
        if off is None or duty is None:
            return None
        off_mv = _i16_clamp(int(off))
        duty_pct_i = max(0, min(100, int(duty)))
        payload = bytearray(struct.pack("<h", off_mv))
        payload.append(duty_pct_i & 0xFF)
        payload.extend([0] * 5)
        return bytes(payload)

    def _present_mrs_status(snap: tuple) -> bool:
        return snap[9] is not None

    def _build_mrs_status(snap: tuple) -> Optional[bytes]:
        st = snap[9]
        if st is None:
            return None
        on_i, mode_i, out_val = st
        payload = bytearray(8)
        payload[0] = 1 if int(on_i) else 0
        payload[1] = int(mode_i) & 0xFF
        payload[2:6] = struct.pack("<f", float(out_val))
        return bytes(payload)

    def _present_mrs_input(snap: tuple) -> bool:
        return snap[10] is not None

    def _build_mrs_input(snap: tuple) -> Optional[bytes]:
        v = snap[10]
        if v is None:
            return None
        payload = bytearray(8)
        payload[0:4] = struct.pack("<f", float(v))
        return bytes(payload)

    # Register tasks (IDs are extended IDs).
    add_task(
        name="MMETER",
        arb_id=int(getattr(config, "MMETER_READ_ID", 0x0CFF0004)),
        period_attr="CAN_TX_PERIOD_MMETER_LEGACY_MS",
        present_fn=_present_meter,
        build_payload_fn=_build_meter,
    )
    add_task(
        name="MMETER_EXT",
        arb_id=int(getattr(config, "MMETER_READ_EXT_ID", 0x0CFF0009)),
        period_attr="CAN_TX_PERIOD_MMETER_EXT_MS",
        present_fn=_present_mmeter_ext,
        build_payload_fn=_build_mmeter_ext,
    )
    add_task(
        name="MMETER_STATUS",
        arb_id=int(getattr(config, "MMETER_STATUS_ID", 0x0CFF000A)),
        period_attr="CAN_TX_PERIOD_MMETER_STATUS_MS",
        present_fn=_present_mmeter_status,
        build_payload_fn=_build_mmeter_status,
    )
    add_task(
        name="ELOAD",
        arb_id=int(getattr(config, "ELOAD_READ_ID", 0x0CFF0003)),
        period_attr="CAN_TX_PERIOD_ELOAD_MS",
        present_fn=_present_eload,
        build_payload_fn=_build_eload,
    )
    add_task(
        name="AFG_EXT",
        arb_id=int(getattr(config, "AFG_READ_EXT_ID", 0x0CFF0006)),
        period_attr="CAN_TX_PERIOD_AFG_EXT_MS",
        present_fn=_present_afg_ext,
        build_payload_fn=_build_afg_ext,
    )
    add_task(
        name="MRSIGNAL_STATUS",
        arb_id=int(getattr(config, "MRSIGNAL_READ_STATUS_ID", 0x0CFF0007)),
        period_attr="CAN_TX_PERIOD_MRS_STATUS_MS",
        present_fn=_present_mrs_status,
        build_payload_fn=_build_mrs_status,
    )
    add_task(
        name="MRSIGNAL_INPUT",
        arb_id=int(getattr(config, "MRSIGNAL_READ_INPUT_ID", 0x0CFF0008)),
        period_attr="CAN_TX_PERIOD_MRS_INPUT_MS",
        present_fn=_present_mrs_input,
        build_payload_fn=_build_mrs_input,
    )

    # Friendly startup log (includes per-frame periods so tuning is obvious in logs).
    try:
        sched = ", ".join([f"{t.name}={t.period_s*1000:.0f}ms" for t in tasks])
    except Exception:
        sched = ""
    soc = "on-change" if send_on_change else "periodic-only"
    if sched:
        log_fn(f"TX thread started (tick={tick_s*1000:.0f} ms, {soc}; {sched}).")
    else:
        log_fn(f"TX thread started (tick={tick_s*1000:.0f} ms, {soc}).")

    next_t = time.monotonic()
    err_count = 0

    while not stop_event.is_set():
        # NEW OPTIMIZED APPROACH:
        # 1. Find the minimum next_due time among all present tasks
        now = time.monotonic()
        earliest_due = now + 10.0 # Upper bound
        
        active_tasks = [t for t in tasks if t.present_last or send_on_change]
        if active_tasks:
            earliest_due = min(t.next_due for t in active_tasks)
        
        # 2. Ensure we don't sleep longer than our max reaction time (e.g. 100ms)
        #    to catch new "present" signals or stop events.
        sleep_s = max(0.005, earliest_due - now)
        sleep_s = min(sleep_s, 0.1) 

        stop_event.wait(timeout=sleep_s)
        
        if next_t < now - (10.0 * tick_s):
            next_t = now + tick_s

        snap = tx_state.snapshot()

        for task in tasks:
            try:
                present = bool(task.present_fn(snap))
            except Exception:
                present = False

            if not present:
                task.mark_absent(now)
                continue

            # If it was absent and is now present, force an immediate send.
            if not task.present_last:
                task.present_last = True
                task.next_due = float(now)

            due = now >= float(task.next_due)

            # Only build payload when needed. (Saves CPU on long periods.)
            payload: bytes | None = None
            if due or send_on_change:
                try:
                    payload = task.build_payload_fn(snap)
                except Exception:
                    payload = None

            if payload is None:
                # Treat as absent; clears last_payload so the next good payload sends immediately.
                task.mark_absent(now)
                continue

            changed = (task.last_payload is None) or (payload != task.last_payload)

            # Decide whether to send.
            do_send = False
            if due:
                do_send = True
            elif send_on_change and changed:
                if min_change_s <= 0:
                    do_send = True
                else:
                    if (now - float(task.last_sent)) >= float(min_change_s):
                        do_send = True

            if not do_send:
                continue

            try:
                msg = can.Message(
                    arbitration_id=int(task.arbitration_id),
                    data=payload,
                    is_extended_id=bool(task.is_extended_id),
                )
                cbus.send(msg)
                if busload:
                    try:
                        busload.record_tx(len(payload))
                    except Exception:
                        pass

                task.last_payload = payload
                task.last_sent = float(now)
                # Next periodic keepalive
                task.next_due = float(now) + float(task.period_s)

            except Exception as e:
                err_count += 1
                if err_count in (1, 10, 100) or (err_count % 500 == 0):
                    log_fn(f"TX {task.name} send error (count={err_count}): {e}")

def can_rx_loop(
    cbus: can.BusABC,
    cmd_queue,
    stop_event: threading.Event,
    watchdog,
    pat_matrix: "PatSwitchMatrixState | None" = None,
    busload: BusLoadMeter | None = None,
    log_fn=print,
) -> None:
    """Read CAN frames and enqueue them for the device comms worker.

    This thread intentionally does *no* hardware I/O.
    """

    log_fn("CAN RX thread started.")
    # Keep separate counters so logs reflect whether we are dropping *incoming*
    # frames (worst case) or dropping *oldest buffered* frames to make room for
    # the newest command (preferred under backpressure).
    drop_oldest = 0
    drop_newest = 0

    # Only control frames should be forwarded to the device thread.
    # This prevents unrelated bus chatter from filling the bounded queue and
    # causing control latency (or drops) when the bus is busy.
    ctrl_ids = {
        int(getattr(config, "RLY_CTRL_ID", 0)),
        int(getattr(config, "AFG_CTRL_ID", 0)),
        int(getattr(config, "AFG_CTRL_EXT_ID", 0)),
        int(getattr(config, "MMETER_CTRL_ID", 0)),
        int(getattr(config, "MMETER_CTRL_EXT_ID", 0)),
        int(getattr(config, "LOAD_CTRL_ID", 0)),
        int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)),
    }

    # Optional: apply driver/kernel-level CAN ID filters (reduces CPU on busy buses).
    # NOTE: When filters are enabled, the bus-load estimator (if enabled) will only
    # observe the filtered subset of traffic.
    filter_mode = str(getattr(config, "CAN_RX_KERNEL_FILTER_MODE", "none") or "none").strip().lower()
    if filter_mode not in ("", "none", "off", "0", "false"):
        filt_ids: set[int] = set()

        if filter_mode in ("control", "ctrl", "control_only"):
            filt_ids = {int(i) for i in ctrl_ids if int(i) != 0}
        elif filter_mode in ("control+pat", "control_pat", "pat", "ctrl+pat"):
            filt_ids = {int(i) for i in ctrl_ids if int(i) != 0}
            try:
                from ..core.pat_matrix import pat_j_ids as _pat_j_ids

                filt_ids |= set(int(i) for i in _pat_j_ids())
            except Exception:
                pass
        else:
            log_fn(f"CAN_RX_KERNEL_FILTER_MODE={filter_mode!r} not recognized; ignoring.")
            filt_ids = set()

        if filt_ids:
            try:
                # python-can filter format: {"can_id": ..., "can_mask": ...}
                # Use a full 29-bit mask for extended IDs.
                filters = [{"can_id": int(i) & 0x1FFFFFFF, "can_mask": 0x1FFFFFFF} for i in filt_ids]
                cbus.set_filters(filters)
                log_fn(f"CAN RX kernel filter enabled ({filter_mode}): {len(filters)} IDs.")
            except Exception as e:
                log_fn(f"CAN RX kernel filter requested ({filter_mode}) but could not be applied: {e}")


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

        # Capture PAT switching-matrix frames for the dashboard (do not enqueue).
        if pat_matrix is not None:
            try:
                pat_matrix.maybe_update(arb, data)
            except Exception:
                pass

        # Ignore non-control frames to keep the control path responsive.
        if arb not in ctrl_ids:
            continue

        # Non-blocking enqueue; never stall CAN recv due to slow devices.
        try:
            cmd_queue.put_nowait((arb, data))
        except queue.Full:
            # Prefer dropping the *oldest* queued command so the newest state
            # update wins (better for knob/slider style controls).
            try:
                cmd_queue.get_nowait()
                drop_oldest += 1
            except queue.Empty:
                pass

            try:
                cmd_queue.put_nowait((arb, data))
            except queue.Full:
                # Queue is still full (consumer is extremely behind). Drop this
                # newest frame as a last resort.
                drop_newest += 1
                if drop_newest in (1, 10, 100) or (drop_newest % 500 == 0):
                    log_fn(
                        f"CAN RX: command queue saturated; dropped NEWEST frame {drop_newest} times "
                        f"(also dropped {drop_oldest} OLDEST frames to make room)"
                    )
            except Exception:
                drop_newest += 1
        except Exception:
            drop_newest += 1
