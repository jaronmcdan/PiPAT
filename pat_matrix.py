"""PAT switching matrix state helpers.

The PAT uses CAN control frames PAT_J0..PAT_J5 to describe the requested
switching matrix configuration.

Each PAT_Jx frame packs 12 fields, 2-bits each, into the lowest 24 bits of the
payload (little-endian).

This module keeps a thread-safe snapshot of the most recent PAT_J0..PAT_J5
frames so the Rich dashboard can display a compact "matrix" bar.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

# These IDs come from PAT.dbc (29-bit, J1939-style).
PAT_J_BASE_ID = 0x8CFFE727  # PAT_J0
PAT_J_STRIDE = 0x100
PAT_J_COUNT = 6


def pat_j_ids() -> set[int]:
    """Return the set of arbitration IDs for PAT_J0..PAT_J5."""
    return {PAT_J_BASE_ID + (i * PAT_J_STRIDE) for i in range(PAT_J_COUNT)}


def decode_pat_j_payload(data: bytes | bytearray | None) -> List[int]:
    """Decode a PAT_Jx payload into 12 2-bit values.

    The 12 values occupy bits 0..23, little-endian (Intel).
    """

    b = bytes(data or b"")
    b0 = b[0] if len(b) > 0 else 0
    b1 = b[1] if len(b) > 1 else 0
    b2 = b[2] if len(b) > 2 else 0
    u24 = int(b0) | (int(b1) << 8) | (int(b2) << 16)
    return [(u24 >> (2 * i)) & 0x3 for i in range(12)]


class PatSwitchMatrixState:
    """Thread-safe last-seen snapshot of PAT_J0..PAT_J5."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vals: Dict[int, List[int]] = {}
        self._ts: Dict[int, float] = {}

    @staticmethod
    def _id_to_index(arb_id: int) -> Optional[int]:
        try:
            aid = int(arb_id)
        except Exception:
            return None
        d = aid - int(PAT_J_BASE_ID)
        if d < 0:
            return None
        if (d % int(PAT_J_STRIDE)) != 0:
            return None
        idx = d // int(PAT_J_STRIDE)
        if 0 <= idx < int(PAT_J_COUNT):
            return int(idx)
        return None

    def maybe_update(self, arb_id: int, data: bytes | bytearray | None, ts: float | None = None) -> bool:
        """Update state if this is a PAT_J0..PAT_J5 frame.

        Returns True if the frame was recognized and captured.
        """

        idx = self._id_to_index(arb_id)
        if idx is None:
            return False

        vals = decode_pat_j_payload(data)
        t = float(ts if ts is not None else time.monotonic())
        with self._lock:
            self._vals[idx] = vals
            self._ts[idx] = t
        return True

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """Return a snapshot keyed by 'J0'..'J5'."""

        now = time.monotonic()
        out: Dict[str, Dict[str, object]] = {}
        with self._lock:
            for i in range(int(PAT_J_COUNT)):
                vals = self._vals.get(i)
                ts = self._ts.get(i)
                age = None if ts is None else (now - float(ts))
                out[f"J{i}"] = {
                    "vals": list(vals) if isinstance(vals, list) else None,
                    "age": age,
                }
        return out
