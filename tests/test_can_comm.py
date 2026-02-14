from __future__ import annotations

import queue
import threading
import time
import types


def test_setup_can_interface_socketcan_success(monkeypatch):
    import roi.config as config
    import can
    from roi.can import comm as can_comm

    calls = []

    def fake_run(cmd, check=False, capture_output=True, text=True):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(can_comm.subprocess, "run", fake_run)

    def fake_bus_factory(**kwargs):
        return types.SimpleNamespace(backend="socketcan", kwargs=kwargs)

    monkeypatch.setattr(can.interface, "Bus", lambda **kwargs: fake_bus_factory(**kwargs))
    monkeypatch.setattr(config, "CAN_INTERFACE", "socketcan", raising=False)

    bus = can_comm.setup_can_interface("can0", 250000, do_setup=True)
    assert bus is not None
    assert bus.backend == "socketcan"
    assert calls, "ip link should have been attempted"


def test_setup_can_interface_socketcan_handles_missing_ip(monkeypatch):
    """If `ip`/`sudo` aren't present, setup continues to bus open."""
    import roi.config as config
    import can
    from roi.can import comm as can_comm

    monkeypatch.setattr(config, "CAN_INTERFACE", "socketcan", raising=False)

    def fake_run(cmd, check=False, capture_output=True, text=True):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(can_comm.subprocess, "run", fake_run)
    monkeypatch.setattr(can.interface, "Bus", lambda **kwargs: types.SimpleNamespace(kwargs=kwargs))
    bus = can_comm.setup_can_interface("can0", 250000, do_setup=True, log_fn=lambda s: None)
    assert bus is not None


def test_setup_can_interface_rmcanview_failure_logs(monkeypatch):
    import roi.config as config
    from roi.can import comm as can_comm
    from roi.can import rmcanview

    monkeypatch.setattr(config, "CAN_INTERFACE", "rmcanview", raising=False)

    def boom(*args, **kwargs):
        raise RuntimeError("no serial")

    monkeypatch.setattr(rmcanview, "RmCanViewBus", boom)
    logs: list[str] = []
    bus = can_comm.setup_can_interface("/dev/ttyUSB0", 125000, do_setup=False, log_fn=logs.append)
    assert bus is None
    assert any("rmcanview" in m.lower() for m in logs)


def test_setup_can_interface_fallback_failure_logs(monkeypatch):
    import roi.config as config
    import can
    from roi.can import comm as can_comm

    monkeypatch.setattr(config, "CAN_INTERFACE", "unknown_iface", raising=False)
    monkeypatch.setattr(can.interface, "Bus", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    logs: list[str] = []
    bus = can_comm.setup_can_interface("can0", 125000, do_setup=False, log_fn=logs.append)
    assert bus is None
    assert any("unknown_iface" in m for m in logs)


def test_setup_can_interface_socketcan_failure_logs(monkeypatch):
    import roi.config as config
    import can
    from roi.can import comm as can_comm

    monkeypatch.setattr(config, "CAN_INTERFACE", "socketcan", raising=False)
    monkeypatch.setattr(can.interface, "Bus", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    logs: list[str] = []
    bus = can_comm.setup_can_interface("can0", 250000, do_setup=False, log_fn=logs.append)
    assert bus is None
    assert any("CAN Init Failed" in m for m in logs)


def test_setup_can_interface_rmcanview(monkeypatch):
    import roi.config as config
    from roi.can import comm as can_comm
    from roi.can import rmcanview

    monkeypatch.setattr(config, "CAN_INTERFACE", "rmcanview", raising=False)
    monkeypatch.setattr(config, "CAN_SERIAL_BAUD", 57600, raising=False)

    created = {}

    class FakeBus:
        def __init__(self, channel, **kwargs):
            created["channel"] = channel
            created["kwargs"] = dict(kwargs)
            self.channel = channel
            self.kwargs = dict(kwargs)
            self.did_setup = False
            self.setup_bitrate = None
            # Mimic real backend: call do_setup from __init__ when requested.
            if self.kwargs.get("do_setup"):
                self.do_setup(can_bitrate=int(self.kwargs.get("can_bitrate") or 0))
    
        def do_setup(self, *, can_bitrate: int):
            self.did_setup = True
            self.setup_bitrate = can_bitrate
    monkeypatch.setattr(rmcanview, "RmCanViewBus", FakeBus)

    bus = can_comm.setup_can_interface("/dev/ttyUSB0", 125000, do_setup=True, log_fn=lambda s: None)
    assert bus is not None
    assert getattr(bus, "did_setup", False) is True
    assert getattr(bus, "setup_bitrate", None) == 125000
    assert created["channel"] == "/dev/ttyUSB0"
    assert created["kwargs"]["serial_baud"] == 57600
    assert created["kwargs"]["can_bitrate"] == 125000


def test_shutdown_can_interface_handles_missing_ip(monkeypatch):
    import roi.config as config
    from roi.can import comm as can_comm

    monkeypatch.setattr(config, "CAN_INTERFACE", "socketcan", raising=False)

    def fake_run(cmd, check=False, capture_output=True, text=True):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(can_comm.subprocess, "run", fake_run)
    # should not raise
    can_comm.shutdown_can_interface("can0", do_setup=True)


def test_shutdown_can_interface_noop_cases(monkeypatch):
    import roi.config as config
    from roi.can import comm as can_comm

    # do_setup False -> early return
    can_comm.shutdown_can_interface("can0", do_setup=False)

    # Non-socketcan interfaces do not do teardown
    monkeypatch.setattr(config, "CAN_INTERFACE", "rmcanview", raising=False)
    can_comm.shutdown_can_interface("can0", do_setup=True)


def test_clamps():
    from roi.can import comm as can_comm

    assert can_comm._u16_clamp(-1) == 0
    assert can_comm._u16_clamp(0x1FFFF) == 0xFFFF
    assert can_comm._i16_clamp(-40000) == -32768
    assert can_comm._i16_clamp(40000) == 32767


def test_tx_state_clear_meter_current():
    from roi.can import comm as can_comm

    s = can_comm.OutgoingTxState()
    s.update_meter_current(123)
    assert s.snapshot()[0] == 123
    s.clear_meter_current()
    assert s.snapshot()[0] is None


def test_can_tx_loop_period_disabled_logs():
    from roi.can import comm as can_comm

    state = can_comm.OutgoingTxState()
    stop = threading.Event()
    bus = FakeBus(stop_after_sends=0)
    bus.stop_event = stop

    logs: list[str] = []
    can_comm.can_tx_loop(bus, state, stop, period_s=0.0, log_fn=logs.append)
    assert any("disabled" in m.lower() for m in logs)


def test_can_tx_loop_period_parse_error_defaults(monkeypatch):
    from roi.can import comm as can_comm

    stop = threading.Event()
    stop.set()
    bus = FakeBus()
    state = can_comm.OutgoingTxState()

    logs: list[str] = []
    # period_s is intentionally invalid -> falls back to 0.05
    can_comm.can_tx_loop(bus, state, stop, period_s="bad", log_fn=logs.append)
    assert any("TX thread started" in m for m in logs)


def test_can_tx_loop_waits_when_ahead_of_schedule(monkeypatch):
    from roi.can import comm as can_comm

    # Make monotonic non-monotonic for test: next_t is large, now is small.
    seq = iter([10.0, 0.0])

    def fake_monotonic():
        return next(seq)

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    class FakeEvent:
        def __init__(self):
            self._set = False

        def is_set(self):
            return self._set

        def wait(self, timeout=None):
            # Stop after the first wait.
            self._set = True
            return True

    stop = FakeEvent()
    bus = FakeBus()
    state = can_comm.OutgoingTxState()
    # Should not raise; should exercise the delay>0 branch.
    can_comm.can_tx_loop(bus, state, stop, period_s=0.01)


def test_can_tx_loop_drift_correction(monkeypatch):
    from roi.can import comm as can_comm

    seq = iter([0.0, 100.0])

    def fake_monotonic():
        return next(seq)

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    state = can_comm.OutgoingTxState()
    state.update_meter_current(1)
    stop = threading.Event()

    bus = FakeBus(stop_after_sends=1)
    bus.stop_event = stop
    # Should run one iteration and hit the drift correction path.
    can_comm.can_tx_loop(bus, state, stop, period_s=0.01)
    assert bus.sent, "expected at least one send"


def test_can_tx_loop_busload_record_tx_exception_in_first_block(monkeypatch):
    from roi.can import comm as can_comm

    # Deterministic time so the TX loop never sleeps.
    t = {"x": 0.0}

    def fake_monotonic():
        t["x"] += 0.01
        return t["x"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    state = can_comm.OutgoingTxState()
    state.update_meter_current(123)
    stop = threading.Event()
    bus = FakeBus(stop_after_sends=1)
    bus.stop_event = stop

    busload = FakeBusLoad(raise_on=1)
    can_comm.can_tx_loop(bus, state, stop, period_s=0.01, busload=busload)
    assert len(bus.sent) == 1


def test_can_tx_loop_logs_each_send_error_branch(monkeypatch):
    from roi.can import comm as can_comm

    # Fixed monotonic that always advances enough to avoid sleeping.
    t = {"x": 0.0}

    def fake_monotonic():
        t["x"] += 0.1
        return t["x"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    class ErrorBus:
        def __init__(self, stop_event):
            self.stop_event = stop_event

        def send(self, msg):
            # Stop immediately and raise to trigger the per-frame exception.
            self.stop_event.set()
            raise RuntimeError("boom")

    def run_with_state(configure_state):
        stop = threading.Event()
        bus = ErrorBus(stop)
        state = can_comm.OutgoingTxState()
        configure_state(state)
        logs: list[str] = []
        can_comm.can_tx_loop(bus, state, stop, period_s=0.01, log_fn=logs.append)
        return "\n".join(logs)

    assert "MMETER send error" in run_with_state(lambda s: s.update_meter_current(1))
    assert "MMETER_EXT" in run_with_state(lambda s: s.update_mmeter_values(1.0, 2.0))
    assert "MMETER_STATUS" in run_with_state(lambda s: s.update_mmeter_status(func=1, flags=2))
    assert "ELOAD" in run_with_state(lambda s: s.update_eload(1, 2))
    assert "AFG_EXT" in run_with_state(lambda s: s.update_afg_ext(0, 50))
    assert "MRSIGNAL_STATUS" in run_with_state(lambda s: s.update_mrsignal_status(output_on=True, output_select=1, output_value=1.0))
    assert "MRSIGNAL_INPUT" in run_with_state(lambda s: s.update_mrsignal_input(1.0))


class FakeBus:
    def __init__(self, *, stop_after_sends: int | None = None, fail_on_send: int | None = None):
        self.sent = []
        self.stop_after_sends = stop_after_sends
        self.fail_on_send = fail_on_send
        self._send_count = 0
        self.stop_event: threading.Event | None = None

    def send(self, msg):
        self._send_count += 1
        if self.fail_on_send is not None and self._send_count == self.fail_on_send:
            raise RuntimeError("send fail")
        self.sent.append(msg)
        if self.stop_after_sends is not None and self._send_count >= self.stop_after_sends and self.stop_event:
            self.stop_event.set()


class FakeBusLoad:
    def __init__(self, *, raise_on: int | None = None):
        self.calls = 0
        self.raise_on = raise_on

    def record_tx(self, dlc: int):
        self.calls += 1
        if self.raise_on is not None and self.calls == self.raise_on:
            raise RuntimeError("busload")

    def record_rx(self, dlc: int):
        self.calls += 1


def test_can_tx_loop_sends_all_frames(monkeypatch):
    from roi.can import comm as can_comm
    import roi.config as config

    # Deterministic time so the TX loop never sleeps.
    t = {"x": 0.0}

    def fake_monotonic():
        # Advance by period each call.
        t["x"] += 0.01
        return t["x"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    state = can_comm.OutgoingTxState()
    state.update_meter_current(123)
    state.update_mmeter_values(1.0, None)  # secondary should be NaN
    state.update_mmeter_status(func=2, flags=3)
    state.update_eload(1000, 2000)
    state.update_afg_ext(-100, 42)
    state.update_mrsignal_status(output_on=True, output_select=1, output_value=2.5)
    state.update_mrsignal_input(9.0)

    stop = threading.Event()
    bus = FakeBus(stop_after_sends=7)
    bus.stop_event = stop
    busload = FakeBusLoad(raise_on=2)  # exercise swallow branch
    logs: list[str] = []

    can_comm.can_tx_loop(bus, state, stop, period_s=0.01, busload=busload, log_fn=logs.append)
    # Should have sent all readback frames once.
    assert len(bus.sent) == 7
    assert any("TX thread started" in m for m in logs)


def test_can_tx_loop_send_error_is_logged(monkeypatch):
    from roi.can import comm as can_comm

    t = {"x": 0.0}

    def fake_monotonic():
        t["x"] += 0.01
        return t["x"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    state = can_comm.OutgoingTxState()
    state.update_meter_current(1)
    stop = threading.Event()
    bus = FakeBus(stop_after_sends=1, fail_on_send=1)
    bus.stop_event = stop
    logs: list[str] = []

    can_comm.can_tx_loop(bus, state, stop, period_s=0.01, busload=None, log_fn=logs.append)
    assert any("send error" in m for m in logs)


def test_can_rx_loop_filters_and_drops(monkeypatch):
    from roi.can import comm as can_comm
    import roi.config as config
    import can

    # Fake messages from python-can
    class Msg(can.Message):
        def __init__(self, arb, data=b""):
            super().__init__(arbitration_id=arb, data=data, is_extended_id=True)

    msgs = [
        Msg(0x123, b"\x00"),  # non-control, ignored
        Msg(int(config.RLY_CTRL_ID), b"\x01"),  # control
        Msg(int(config.RLY_CTRL_ID), b"\x00"),  # control (forces queue.Full)
    ]

    stop = threading.Event()

    class Bus:
        def __init__(self):
            self.i = 0

        def recv(self, timeout=1.0):
            if self.i == 0:
                # exercise recv exception branch
                self.i += 1
                raise RuntimeError("recv")
            if self.i - 1 < len(msgs):
                m = msgs[self.i - 1]
                self.i += 1
                if self.i - 1 >= len(msgs):
                    stop.set()
                return m
            stop.set()
            return None

    bus = Bus()

    # Custom queue to force both drop_oldest and drop_newest branches.
    class FakeQ:
        def __init__(self):
            self.items = [(int(config.RLY_CTRL_ID), b"\x01")]
            self.put_calls = 0
            self.get_calls = 0

        def put_nowait(self, item):
            self.put_calls += 1
            # First put sees queue full.
            if self.put_calls in (1, 2):
                raise queue.Full()
            self.items.append(item)

        def get_nowait(self):
            self.get_calls += 1
            if self.items:
                return self.items.pop(0)
            raise queue.Empty()

    q = FakeQ()

    class WD:
        def __init__(self):
            self.marks = []

        def mark(self, name):
            self.marks.append(name)
            # exercise swallow
            if name == "can" and len(self.marks) == 1:
                raise RuntimeError("boom")

    wd = WD()

    logs: list[str] = []
    can_comm.can_rx_loop(bus, q, stop, wd, pat_matrix=None, busload=None, log_fn=logs.append)
    assert any("dropped NEWEST" in m for m in logs)


def test_can_rx_loop_records_busload_and_pat_matrix_errors(monkeypatch):
    from roi.can import comm as can_comm
    import roi.config as config
    import can

    class Msg(can.Message):
        def __init__(self, arb, data=b""):
            super().__init__(arbitration_id=arb, data=data, is_extended_id=True)

    stop = threading.Event()

    # Sequence includes a falsy message to exercise the `if not message` branch.
    seq = [None, Msg(int(config.RLY_CTRL_ID), b"\x01")]

    class Bus:
        def __init__(self):
            self.i = 0

        def recv(self, timeout=1.0):
            if self.i < len(seq):
                v = seq[self.i]
                self.i += 1
                if self.i >= len(seq):
                    stop.set()
                return v
            stop.set()
            return None

    class Busload:
        def record_rx(self, dlc: int):
            raise RuntimeError("boom")

    class Pat:
        def maybe_update(self, arb: int, data: bytes):
            raise RuntimeError("boom")

    class Q:
        def put_nowait(self, item):
            # accept
            return None

    class WD:
        def mark(self, name):
            return None

    can_comm.can_rx_loop(Bus(), Q(), stop, WD(), pat_matrix=Pat(), busload=Busload(), log_fn=lambda s: None)


def test_can_rx_loop_queue_full_drop_oldest_empty_then_second_put_generic(monkeypatch):
    from roi.can import comm as can_comm
    import roi.config as config
    import can

    class Msg(can.Message):
        def __init__(self, arb, data=b""):
            super().__init__(arbitration_id=arb, data=data, is_extended_id=True)

    stop = threading.Event()

    class Bus:
        def __init__(self):
            self.done = False

        def recv(self, timeout=1.0):
            if self.done:
                stop.set()
                return None
            self.done = True
            stop.set()
            return Msg(int(config.RLY_CTRL_ID), b"\x00")

    class Q:
        def __init__(self):
            self.calls = 0

        def put_nowait(self, item):
            self.calls += 1
            if self.calls == 1:
                raise queue.Full()
            # Second put raises a generic exception (not queue.Full)
            raise RuntimeError("oops")

        def get_nowait(self):
            # Queue reports empty when trying to drop oldest.
            raise queue.Empty()

    class WD:
        def mark(self, name):
            return None

    can_comm.can_rx_loop(Bus(), Q(), stop, WD(), pat_matrix=None, busload=None, log_fn=lambda s: None)


def test_can_rx_loop_put_nowait_generic_exception(monkeypatch):
    from roi.can import comm as can_comm
    import roi.config as config
    import can

    class Msg(can.Message):
        def __init__(self, arb, data=b""):
            super().__init__(arbitration_id=arb, data=data, is_extended_id=True)

    stop = threading.Event()

    class Bus:
        def __init__(self):
            self.done = False

        def recv(self, timeout=1.0):
            if self.done:
                stop.set()
                return None
            self.done = True
            stop.set()
            return Msg(int(config.RLY_CTRL_ID), b"\x00")

    class Q:
        def put_nowait(self, item):
            raise RuntimeError("boom")

    class WD:
        def mark(self, name):
            return None

    can_comm.can_rx_loop(Bus(), Q(), stop, WD(), pat_matrix=None, busload=None, log_fn=lambda s: None)


def test_shutdown_can_interface_breaks_on_first_success(monkeypatch):
    """Cover the shutdown_can_interface() success + break path."""
    import roi.config as config
    from roi.can import comm as can_comm

    monkeypatch.setattr(config, "CAN_INTERFACE", "socketcan", raising=False)

    calls: list[list[str]] = []

    def fake_run(cmd, check=False, capture_output=True, text=True):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(can_comm.subprocess, "run", fake_run)

    can_comm.shutdown_can_interface("can0", do_setup=True)

    # We should stop after the first successful attempt.
    assert len(calls) == 1
    assert calls[0][:2] == ["ip", "link"]


def test_can_tx_loop_busload_exceptions_in_each_branch(monkeypatch):
    """Exercise the busload.record_tx() exception swallow blocks for every frame type."""
    from roi.can import comm as can_comm

    # Deterministic time so the TX loop never sleeps.
    t = {"x": 0.0}

    def fake_monotonic():
        t["x"] += 0.01
        return t["x"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    state = can_comm.OutgoingTxState()
    state.update_meter_current(123)
    state.update_mmeter_values(1.0, None)
    state.update_mmeter_status(func=2, flags=3)
    state.update_eload(1000, 2000)
    state.update_afg_ext(-100, 42)
    state.update_mrsignal_status(output_on=True, output_select=1, output_value=2.5)
    state.update_mrsignal_input(9.0)

    stop = threading.Event()
    bus = FakeBus(stop_after_sends=7)
    bus.stop_event = stop

    class AlwaysBoomBusLoad:
        def record_tx(self, dlc: int):
            raise RuntimeError("boom")

    can_comm.can_tx_loop(bus, state, stop, period_s=0.01, busload=AlwaysBoomBusLoad(), log_fn=lambda s: None)
    assert len(bus.sent) == 7
