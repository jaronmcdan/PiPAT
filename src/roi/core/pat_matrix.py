"""PAT switching matrix state helpers.

The PAT uses CAN control frames PAT_J0..PAT_J5 to describe the requested
switching matrix configuration.

Each PAT_Jx frame packs 12 fields, 2-bits each, into the lowest 24 bits of the
payload (little-endian).

This module keeps a thread-safe snapshot of the most recent PAT_J0..PAT_J5
frames so the Rich dashboard can display a compact "matrix" bar.
"""

from __future__ import annotations

import builtins
import re
import threading
import time
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Dict, List, Optional


# NOTE on IDs:
#
# The PAT.dbc included with this project encodes extended-frame IDs using the
# common SocketCAN "can_id" convention where bit31 (0x8000_0000) is the EFF
# (extended frame format) flag.
#
# python-can (and our rmcanview backend) exposes ``Message.arbitration_id`` as
# the *pure* 29-bit identifier, and stores EFF separately as
# ``Message.is_extended_id``.
#
# Therefore, the "real" 29-bit ID for PAT_J0 is 0x0CFFE727, while the DBC shows
# 0x8CFFE727 (= 0x80000000 | 0x0CFFE727).
#
# To be robust across backends (in case any returns can_id-with-flags), we mask
# arbitration IDs down to 29 bits before matching.
PAT_J_BASE_ID = 0x0CFFE727  # PAT_J0 (29-bit ID)
PAT_J_STRIDE = 0x100
PAT_J_COUNT = 6


def _parse_j0_pin_names_from_dbc_text(txt: str) -> Dict[int, str]:
    """Best-effort parse of J0 pin labels from a DBC file (text).

    We look for a `BO_ ... PAT_J0:` section and then extract signals of the form:

        SG_ J0_01_3A_LOAD : ...

    returning a mapping of {1: "3A_LOAD", 2: "5A_LOAD", ...}.
    """

    in_j0 = False
    out: Dict[int, str] = {}
    sig_re = re.compile(r"\bSG_\s+J0_(\d{2})_([A-Za-z0-9_]+)\s*:")
    for raw in (txt or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("BO_"):
            # Enter/exit PAT_J0 section.
            if "PAT_J0" in line:
                in_j0 = True
                continue
            if in_j0:
                break
        if not in_j0:
            continue

        m = sig_re.search(line)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except Exception:
            continue
        name = str(m.group(2)).strip()
        if 1 <= idx <= 12 and name:
            out[idx] = name

    return out




def _parse_j0_pin_names_from_dbc(path: Path) -> Dict[int, str]:
    """Best-effort parse of J0 pin labels from a DBC file on disk.

    This helper exists mainly for local development and unit tests.
    Production code prefers the packaged resource lookup.
    """
    try:
        txt = Path(path).read_text(errors="ignore")
    except Exception:
        return {}
    try:
        return _parse_j0_pin_names_from_dbc_text(txt)
    except Exception:
        return {}
def _read_packaged_pat_dbc_text() -> str | None:
    """Read the packaged PAT.dbc (if included in the installed package)."""
    try:
        return resource_files("roi.assets").joinpath("PAT.dbc").read_text(errors="ignore")
    except Exception:
        return None


def j0_pin_names() -> Dict[int, str]:
    """Return a {pin_index: label} mapping for PAT_J0.

    Prefer parsing the packaged PAT.dbc shipped with ROI. Falls back to a small
    hardcoded mapping if the DBC is missing or doesn't match.
    """

    # Cache on first call.
    global _J0_PIN_NAMES  # type: ignore
    try:
        cached = _J0_PIN_NAMES  # type: ignore
        if isinstance(cached, builtins.dict) and cached:
            return cached.copy()
    except Exception:
        pass

    names: Dict[int, str] = {}
    txt = _read_packaged_pat_dbc_text()
    if txt:
        try:
            names = _parse_j0_pin_names_from_dbc_text(txt)
        except Exception:
            names = {}

    if not names:
        # Fallback mapping (matches the PAT.dbc in this repo as of Feb 2026).
        names = {
            1: "3A_LOAD",
            2: "5A_LOAD",
            3: "7A_LOAD",
            4: "12A_LOAD",
            5: "200MA_PULLUP",
            6: "12MA_PULLUP",
            7: "GND_LOAD",
            8: "METER_LOAD",
            9: "TEST_SUPPLY",
            10: "MAIN_SUPPLY",
            11: "FREQ_GEN",
            12: "PROBE",
        }

    try:
        _J0_PIN_NAMES = names.copy()  # type: ignore
    except Exception:
        pass
    return names.copy()


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
            # Mask to a raw 29-bit arbitration ID. This makes us tolerant of
            # backends that might pass a SocketCAN-style "can_id" with flags
            # (e.g. EFF=0x8000_0000) mixed into the integer.
            aid = int(arb_id) & 0x1FFFFFFF
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
