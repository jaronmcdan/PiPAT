from __future__ import annotations

"""Additional coverage-oriented tests for :mod:`roi.can.comm`.

The existing test suite exercises the primary behavior of the CAN TX/RX loops.
This file focuses on defensive branches that are hard to hit in normal runtime
conditions (e.g., unusual config values, odd snapshot behaviors, and
rate-limiting).

These tests intentionally use small, deterministic stubs so they remain fast.
"""

import threading
import time
import types


def _patch_all_tx_periods(monkeypatch, config_mod, value: int) -> None:
    """Set all per-frame TX period config attributes to ``value`` (ms)."""

    for attr in (
        "CAN_TX_PERIOD_MMETER_LEGACY_MS",
        "CAN_TX_PERIOD_MMETER_EXT_MS",
        "CAN_TX_PERIOD_MMETER_STATUS_MS",
        "CAN_TX_PERIOD_ELOAD_MS",
        "CAN_TX_PERIOD_AFG_EXT_MS",
        "CAN_TX_PERIOD_MRS_STATUS_MS",
        "CAN_TX_PERIOD_MRS_INPUT_MS",
    ):
        monkeypatch.setattr(config_mod, attr, value, raising=False)


def test_can_tx_loop_cfg_period_missing_and_bad_value_defaults(monkeypatch):
    """Cover:
    - _cfg_period_s getattr exception path (missing attr)
    - _cfg_period_s ms is None -> default branch
    - _ms_to_s float conversion exception path
    """

    import roi.config as config
    from roi.can import comm as can_comm

    # Force one missing attribute (AttributeError inside getattr)
    monkeypatch.delattr(config, "CAN_TX_PERIOD_ELOAD_MS", raising=False)
    # Force a bad attribute value (float conversion error inside _ms_to_s)
    monkeypatch.setattr(config, "CAN_TX_PERIOD_MMETER_EXT_MS", "bad", raising=False)

    stop = threading.Event()
    stop.set()  # Avoid running the while-loop; we only want the setup branches.

    logs: list[str] = []
    can_comm.can_tx_loop(types.SimpleNamespace(send=lambda msg: None), can_comm.OutgoingTxState(), stop, period_s=0.01, log_fn=logs.append)
    assert any("TX thread started" in m for m in logs)


def test_can_tx_loop_add_task_disabled_and_empty_sched_logs(monkeypatch):
    """Cover add_task() disabled branch and the startup log when no tasks exist."""

    import roi.config as config
    from roi.can import comm as can_comm

    _patch_all_tx_periods(monkeypatch, config, 0)
    stop = threading.Event()
    stop.set()

    logs: list[str] = []
    can_comm.can_tx_loop(types.SimpleNamespace(send=lambda msg: None), can_comm.OutgoingTxState(), stop, period_s=0.01, log_fn=logs.append)

    # At least one frame should have been disabled.
    assert any("disabled" in m.lower() for m in logs)
    # When all tasks are disabled, the log line does not include the schedule list.
    assert any("TX thread started" in m for m in logs)


def test_can_tx_loop_present_fn_exception_sets_present_false(monkeypatch):
    """Cover the defensive present_fn() exception handler."""

    from roi.can import comm as can_comm

    monkeypatch.setattr(time, "monotonic", lambda: 0.0)

    stop = threading.Event()

    class BadSnap:
        def __getitem__(self, idx):
            raise RuntimeError("boom")

    class OneShotState:
        def __init__(self, stop_event: threading.Event):
            self.stop_event = stop_event
            self.called = False

        def snapshot(self):
            # Stop after the first loop body executes.
            if not self.called:
                self.called = True
                self.stop_event.set()
            return BadSnap()

    # If send() is called, something went wrong (all present_fns should error).
    bus = types.SimpleNamespace(send=lambda msg: (_ for _ in ()).throw(AssertionError("unexpected send")))
    can_comm.can_tx_loop(bus, OneShotState(stop), stop, period_s=0.01, log_fn=lambda s: None)


def test_can_tx_loop_builder_exception_payload_none_marks_absent(monkeypatch):
    """Cover build_payload_fn() exception + payload None handling."""

    from roi.can import comm as can_comm

    monkeypatch.setattr(time, "monotonic", lambda: 0.0)

    stop = threading.Event()

    class BadInt:
        def __int__(self):
            raise RuntimeError("int")

    class Snap:
        def __getitem__(self, idx):
            # Make the legacy meter frame look present, but fail during int() conversion.
            if idx == 0:
                return BadInt()
            return None

    class OneShotState:
        def __init__(self, stop_event: threading.Event):
            self.stop_event = stop_event
            self.called = False

        def snapshot(self):
            if not self.called:
                self.called = True
                self.stop_event.set()
            return Snap()

    bus = types.SimpleNamespace(send=lambda msg: (_ for _ in ()).throw(AssertionError("unexpected send")))
    can_comm.can_tx_loop(bus, OneShotState(stop), stop, period_s=0.01, log_fn=lambda s: None)


def test_can_tx_loop_builder_returns_none_paths_for_all_frames(monkeypatch):
    """Cover the (normally redundant) builder early-return branches."""

    from roi.can import comm as can_comm

    monkeypatch.setattr(time, "monotonic", lambda: 0.0)

    stop = threading.Event()

    class FlakySnap:
        """Return a value on the first access per index, then None.

        present_fn() sees non-None, but build_payload_fn() sees None for the
        second access, exercising the defensive early-return branches.
        """

        def __init__(self):
            self.calls: dict[int, int] = {}

        def __getitem__(self, idx):
            self.calls[idx] = self.calls.get(idx, 0) + 1
            first = self.calls[idx] == 1
            if not first:
                return None

            if idx == 0:
                return 1
            if idx == 1:
                return 1.0
            if idx == 3:
                return 1
            if idx == 4:
                return 2
            if idx == 5:
                return 100
            if idx == 6:
                return 200
            if idx == 7:
                return -5
            if idx == 8:
                return 50
            if idx == 9:
                return (1, 2, 3.0)
            if idx == 10:
                return 4.0
            return None

    class OneShotState:
        def __init__(self, stop_event: threading.Event):
            self.stop_event = stop_event
            self.called = False

        def snapshot(self):
            if not self.called:
                self.called = True
                self.stop_event.set()
            return FlakySnap()

    bus = types.SimpleNamespace(send=lambda msg: (_ for _ in ()).throw(AssertionError("unexpected send")))
    can_comm.can_tx_loop(bus, OneShotState(stop), stop, period_s=0.01, log_fn=lambda s: None)


def test_can_tx_loop_send_on_change_min_zero_sends_between_due(monkeypatch):
    """Cover send-on-change path with min_change_s <= 0."""

    import roi.config as config
    from roi.can import comm as can_comm

    # Only keep the legacy MMETER task enabled to keep the test minimal.
    _patch_all_tx_periods(monkeypatch, config, 0)
    monkeypatch.setattr(config, "CAN_TX_PERIOD_MMETER_LEGACY_MS", 1000, raising=False)  # 1s period
    monkeypatch.setattr(config, "CAN_TX_SEND_ON_CHANGE", True, raising=False)
    monkeypatch.setattr(config, "CAN_TX_SEND_ON_CHANGE_MIN_MS", 0, raising=False)

    t = {"x": 0.0}

    def fake_monotonic():
        # Advance in tick-sized steps so the loop never sleeps.
        v = t["x"]
        t["x"] += 0.01
        return v

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    stop = threading.Event()

    class ChangingState:
        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            # Change the meter reading between ticks.
            mA = 1 if self.calls == 1 else 2
            return (mA, None, None, None, None, None, None, None, None, None, None)

    class Bus:
        def __init__(self, stop_event: threading.Event):
            self.sent = []
            self.stop_event = stop_event

        def send(self, msg):
            self.sent.append(msg)
            if len(self.sent) >= 2:
                self.stop_event.set()

    bus = Bus(stop)
    can_comm.can_tx_loop(bus, ChangingState(), stop, period_s=0.01, log_fn=lambda s: None)

    # First send is due=True, second send is due=False but send_on_change=True.
    assert len(bus.sent) == 2


def test_can_tx_loop_send_on_change_rate_limited_skips_send(monkeypatch):
    """Cover send-on-change min_change_s > 0 and the do_send False continue."""

    import roi.config as config
    from roi.can import comm as can_comm

    _patch_all_tx_periods(monkeypatch, config, 0)
    monkeypatch.setattr(config, "CAN_TX_PERIOD_MMETER_LEGACY_MS", 1000, raising=False)
    monkeypatch.setattr(config, "CAN_TX_SEND_ON_CHANGE", True, raising=False)
    monkeypatch.setattr(config, "CAN_TX_SEND_ON_CHANGE_MIN_MS", 100, raising=False)  # 0.1s

    # Times: first loop sends at t=0.0, second loop at t=0.01 (rate-limited).
    seq = iter([0.0, 0.0, 0.01, 0.01, 0.01])
    monkeypatch.setattr(time, "monotonic", lambda: next(seq))

    stop = threading.Event()

    class TwoShotState:
        def __init__(self, stop_event: threading.Event):
            self.calls = 0
            self.stop_event = stop_event

        def snapshot(self):
            self.calls += 1
            if self.calls >= 2:
                # Stop after the second iteration (regardless of send count).
                self.stop_event.set()
            mA = 1 if self.calls == 1 else 2
            return (mA, None, None, None, None, None, None, None, None, None, None)

    class Bus:
        def __init__(self):
            self.sent = []

        def send(self, msg):
            self.sent.append(msg)

    bus = Bus()
    can_comm.can_tx_loop(bus, TwoShotState(stop), stop, period_s=0.01, log_fn=lambda s: None)

    # Only the first (due=True) send should happen; the second is rate-limited.
    assert len(bus.sent) == 1


def test_can_tx_loop_send_on_change_rate_limit_allows_send_after_delay(monkeypatch):
    """Cover the min_change_s > 0 path where the rate-limit condition is met."""

    import roi.config as config
    from roi.can import comm as can_comm

    _patch_all_tx_periods(monkeypatch, config, 0)
    monkeypatch.setattr(config, "CAN_TX_PERIOD_MMETER_LEGACY_MS", 1000, raising=False)
    monkeypatch.setattr(config, "CAN_TX_SEND_ON_CHANGE", True, raising=False)
    monkeypatch.setattr(config, "CAN_TX_SEND_ON_CHANGE_MIN_MS", 100, raising=False)  # 0.1s

    # First loop sends at t=0.0, second loop at t=0.11 (allowed by min_change_s).
    seq = iter([0.0, 0.0, 0.11, 0.11, 0.11])
    monkeypatch.setattr(time, "monotonic", lambda: next(seq))

    stop = threading.Event()

    class ChangingState:
        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            mA = 1 if self.calls == 1 else 2
            return (mA, None, None, None, None, None, None, None, None, None, None)

    class Bus:
        def __init__(self, stop_event: threading.Event):
            self.sent = []
            self.stop_event = stop_event

        def send(self, msg):
            self.sent.append(msg)
            if len(self.sent) >= 2:
                self.stop_event.set()

    bus = Bus(stop)
    can_comm.can_tx_loop(bus, ChangingState(), stop, period_s=0.01, log_fn=lambda s: None)
    assert len(bus.sent) == 2


def test_can_rx_loop_kernel_filter_modes(monkeypatch):
    """Exercise CAN_RX_KERNEL_FILTER_MODE branches (control, control+pat, unknown, exception)."""

    import roi.config as config
    from roi.can import comm as can_comm
    from roi.core import pat_matrix

    # 1) control: set_filters called
    stop = threading.Event()
    stop.set()
    logs: list[str] = []

    applied = {}

    class Bus:
        def set_filters(self, filters):
            applied["filters"] = list(filters)

    monkeypatch.setattr(config, "CAN_RX_KERNEL_FILTER_MODE", "control", raising=False)
    can_comm.can_rx_loop(Bus(), object(), stop, object(), pat_matrix=None, busload=None, log_fn=logs.append)
    assert applied.get("filters"), "expected set_filters() to be called"

    # 2) control+pat: includes pat IDs
    applied.clear()
    logs.clear()
    monkeypatch.setattr(config, "CAN_RX_KERNEL_FILTER_MODE", "control+pat", raising=False)
    monkeypatch.setattr(pat_matrix, "pat_j_ids", lambda: {0x8CFFE727}, raising=False)
    can_comm.can_rx_loop(Bus(), object(), stop, object(), pat_matrix=None, busload=None, log_fn=logs.append)
    assert any((f.get("can_id") == (0x8CFFE727 & 0x1FFFFFFF)) for f in applied.get("filters", []))

    # 2b) control+pat but pat_j_ids explodes: exception is swallowed and we still apply ctrl filters.
    applied.clear()
    logs.clear()
    monkeypatch.setattr(config, "CAN_RX_KERNEL_FILTER_MODE", "control+pat", raising=False)
    monkeypatch.setattr(pat_matrix, "pat_j_ids", lambda: (_ for _ in ()).throw(RuntimeError("boom")), raising=False)
    can_comm.can_rx_loop(Bus(), object(), stop, object(), pat_matrix=None, busload=None, log_fn=logs.append)
    assert applied.get("filters"), "expected set_filters() despite pat_j_ids error"

    # 3) unknown mode: logs and does not apply filters
    class BusNoCall:
        def set_filters(self, filters):
            raise AssertionError("should not be called")

    logs.clear()
    monkeypatch.setattr(config, "CAN_RX_KERNEL_FILTER_MODE", "weird_mode", raising=False)
    can_comm.can_rx_loop(BusNoCall(), object(), stop, object(), pat_matrix=None, busload=None, log_fn=logs.append)
    assert any("not recognized" in m.lower() for m in logs)

    # 4) set_filters raises: exception is logged
    class BusBoom:
        def set_filters(self, filters):
            raise RuntimeError("boom")

    logs.clear()
    monkeypatch.setattr(config, "CAN_RX_KERNEL_FILTER_MODE", "control", raising=False)
    can_comm.can_rx_loop(BusBoom(), object(), stop, object(), pat_matrix=None, busload=None, log_fn=logs.append)
    assert any("could not be applied" in m.lower() for m in logs)
