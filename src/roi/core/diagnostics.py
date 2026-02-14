"""Lightweight diagnostics + health tracking.

ROI is built to keep running even when hardware is missing, disconnected, or
timing out. That is great for robustness, but it can make failures invisible
because exceptions are often swallowed to keep the control loop responsive.

This module provides a tiny, dependency-free way to surface what is going on:

* A ring-buffer of recent log/events that a UI can display.
* Per-device health stats (last OK time, last error, error count).

The intent is *observability*, not heavy structured logging.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional


@dataclass(frozen=True)
class DiagEvent:
    """One UI-friendly log/event entry."""

    ts_unix: float
    ts_mono: float
    level: str
    source: str
    message: str


class Diagnostics:
    """Thread-safe event log + per-device health information."""

    def __init__(self, *, max_events: int = 250, dedupe_window_s: float = 0.75) -> None:
        self._lock = threading.Lock()
        self._events: Deque[DiagEvent] = deque(maxlen=int(max_events) if max_events else 250)
        self._dedupe_window_s = float(dedupe_window_s) if dedupe_window_s else 0.0

        # Per-key health
        self._health: Dict[str, Dict[str, Any]] = {}

        # Per-key event dedupe
        self._last_event: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def log(self, message: str, *, level: str = "info", source: str = "roi") -> None:
        """Append an event to the ring buffer."""

        msg = str(message or "")
        lvl = str(level or "info")
        src = str(source or "roi")
        now_u = time.time()
        now_m = time.monotonic()

        # Dedupe: suppress repeated identical messages from the same source in a
        # short window to avoid spamming the UI during hard failures.
        if self._dedupe_window_s > 0:
            with self._lock:
                prev = self._last_event.get(src)
                if prev and prev.get("msg") == msg:
                    try:
                        if (now_m - float(prev.get("ts_m", 0.0))) < self._dedupe_window_s:
                            prev["n"] = int(prev.get("n", 1)) + 1
                            prev["ts_m"] = now_m
                            return
                    except Exception:
                        pass

                self._last_event[src] = {"msg": msg, "ts_m": now_m, "n": 1}
                self._events.append(
                    DiagEvent(ts_unix=now_u, ts_mono=now_m, level=lvl, source=src, message=msg)
                )
                return

        with self._lock:
            self._events.append(DiagEvent(ts_unix=now_u, ts_mono=now_m, level=lvl, source=src, message=msg))

    def events_snapshot(self) -> List[Dict[str, Any]]:
        """Return the current ring buffer as JSON-friendly dicts."""

        with self._lock:
            return [
                {
                    "ts_unix": float(e.ts_unix),
                    "ts_mono": float(e.ts_mono),
                    "level": e.level,
                    "source": e.source,
                    "message": e.message,
                }
                for e in list(self._events)
            ]

    # ------------------------------------------------------------------
    # Per-device health
    # ------------------------------------------------------------------

    def mark_ok(self, key: str) -> None:
        """Record a successful interaction for a given device/key."""

        k = str(key or "")
        if not k:
            return
        now_m = time.monotonic()
        now_u = time.time()
        with self._lock:
            st = self._health.setdefault(k, {})
            st["last_ok_mono"] = float(now_m)
            st["last_ok_unix"] = float(now_u)

    def mark_error(self, key: str, exc: BaseException, *, where: str = "") -> None:
        """Record an error for a given device/key.

        Also adds a condensed event to the event ring.
        """

        k = str(key or "")
        if not k:
            return

        now_m = time.monotonic()
        now_u = time.time()

        et = type(exc).__name__
        emsg = str(exc)
        loc = f" ({where})" if where else ""
        msg = f"{et}: {emsg}{loc}".strip()

        tb = ""
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        except Exception:
            tb = ""

        with self._lock:
            st = self._health.setdefault(k, {})
            st["error_count"] = int(st.get("error_count", 0)) + 1
            st["last_error_mono"] = float(now_m)
            st["last_error_unix"] = float(now_u)
            st["last_error"] = msg
            if tb:
                # Keep it bounded; web UI should not become a crash dump.
                st["last_error_trace"] = tb[-8000:]

        # Also emit an event (dedupe will throttle if the same error repeats).
        self.log(msg, level="error", source=k)

    def health_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return device health information as JSON-friendly dict."""

        now_m = time.monotonic()
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for k, st in self._health.items():
                d = dict(st)
                try:
                    if "last_ok_mono" in d:
                        d["last_ok_age_s"] = float(now_m) - float(d["last_ok_mono"])
                    if "last_error_mono" in d:
                        d["last_error_age_s"] = float(now_m) - float(d["last_error_mono"])
                except Exception:
                    pass
                out[str(k)] = d
            return out

    def snapshot(self) -> Dict[str, Any]:
        return {
            "events": self.events_snapshot(),
            "health": self.health_snapshot(),
        }
