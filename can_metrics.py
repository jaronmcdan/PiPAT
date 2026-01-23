# can_metrics.py
from __future__ import annotations

import time
import threading
from collections import deque
from typing import Deque, Optional, Tuple


class BusLoadMeter:
    """Estimate CAN bus load over a sliding time window.

    This is an estimator (not a physical-layer measurement). It uses:
      bits ~= (overhead_bits + 8*DLC) * stuffing_factor

    It counts:
      - RX frames observed by this SocketCAN interface
      - TX frames sent by PiPAT (recorded in software)
    """

    def __init__(
        self,
        *,
        bitrate: int,
        window_s: float = 1.0,
        stuffing_factor: float = 1.2,
        overhead_bits: int = 48,
        enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self._bitrate = max(1, int(bitrate))
        self._window_s = max(0.1, float(window_s))
        self._stuff = max(1.0, float(stuffing_factor))
        self._overhead = max(0, int(overhead_bits))

        self._lock = threading.Lock()
        self._events: Deque[Tuple[float, int, bool]] = deque()  # (t, bits, is_tx)
        self._sum_bits = 0
        self._rx_frames = 0
        self._tx_frames = 0

    def _estimate_bits(self, dlc: int) -> int:
        dlc = max(0, int(dlc))
        return int(round((self._overhead + 8 * dlc) * self._stuff))

    def _purge(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._events and self._events[0][0] < cutoff:
            _t, bits, is_tx = self._events.popleft()
            self._sum_bits -= bits
            if is_tx:
                self._tx_frames -= 1
            else:
                self._rx_frames -= 1

    def record_rx(self, dlc: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        bits = self._estimate_bits(dlc)
        with self._lock:
            self._purge(now)
            self._events.append((now, bits, False))
            self._sum_bits += bits
            self._rx_frames += 1

    def record_tx(self, dlc: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        bits = self._estimate_bits(dlc)
        with self._lock:
            self._purge(now)
            self._events.append((now, bits, True))
            self._sum_bits += bits
            self._tx_frames += 1

    def snapshot(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Return (load_pct, rx_fps, tx_fps) over the current window."""
        if not self.enabled:
            return (None, None, None)
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            window = self._window_s
            load = 100.0 * (float(self._sum_bits) / float(self._bitrate * window))
            load = max(0.0, min(100.0, load))
            rx_fps = float(self._rx_frames) / window
            tx_fps = float(self._tx_frames) / window
            return (load, rx_fps, tx_fps)
