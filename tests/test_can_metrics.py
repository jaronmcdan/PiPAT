from __future__ import annotations

import time


def test_busload_disabled():
    from roi.can.metrics import BusLoadMeter

    m = BusLoadMeter(bitrate=250_000, enabled=False)
    m.record_rx(8)
    m.record_tx(8)
    assert m.snapshot() == (None, None, None)


def test_busload_basic_counts(monkeypatch):
    from roi.can.metrics import BusLoadMeter

    t = 1000.0

    def fake_monotonic():
        return t

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    m = BusLoadMeter(bitrate=1000, window_s=1.0, stuffing_factor=1.0, overhead_bits=0, smooth_alpha=0.0)

    # At t=1000, record one RX frame with dlc=1 => 8 bits
    m.record_rx(1)
    # And one TX frame with dlc=2 => 16 bits
    m.record_tx(2)
    load, rx_fps, tx_fps = m.snapshot()
    assert rx_fps == 1.0
    assert tx_fps == 1.0
    # total bits = 24 over 1 second at bitrate 1000 => 2.4%
    assert abs(load - 2.4) < 1e-6

    # Advance beyond window and verify purge.
    nonlocal_t = {"t": t}

    def advance(dt):
        nonlocal_t["t"] += dt

    def fake_monotonic2():
        return nonlocal_t["t"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic2)
    advance(2.0)
    load2, rx_fps2, tx_fps2 = m.snapshot()
    assert load2 == 0.0
    assert rx_fps2 == 0.0
    assert tx_fps2 == 0.0


def test_busload_smoothing_converges(monkeypatch):
    from roi.can.metrics import BusLoadMeter

    # Use a very small bitrate so values are obvious.
    t = 0.0

    def fake_monotonic():
        return t

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    m = BusLoadMeter(bitrate=100, window_s=1.0, stuffing_factor=1.0, overhead_bits=0, smooth_alpha=0.5)

    # Add one TX dlc=1 => 8 bits, raw load=8%
    m.record_tx(1)
    load1, *_ = m.snapshot()
    # First EMA initializes to raw
    assert abs(load1 - 8.0) < 1e-6

    # Add more bits so raw load changes; EMA should move half-way each snapshot.
    t += 0.1
    m.record_tx(1)  # now 16 bits => 16%
    load2, *_ = m.snapshot()
    assert 8.0 <= load2 <= 16.0
