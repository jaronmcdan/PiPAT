#!/usr/bin/env python3
"""Quick auto-detect diagnostics for ROI.

Preferred run methods:
  - roi-autodetect-diag          (after install)
  - python -m roi.tools.autodetect_diag
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Dict

from .. import config
from ..core.device_discovery import autodetect_and_patch_config


KEYS = [
    "CAN_INTERFACE",
    "CAN_CHANNEL",
    "MULTI_METER_PATH",
    "MRSIGNAL_PORT",
    "K1_SERIAL_PORT",
    "AFG_VISA_ID",
    "ELOAD_VISA_ID",
]


def _snapshot() -> Dict[str, object]:
    return {k: getattr(config, k, None) for k in KEYS}


def _print_snapshot(title: str, snap: Dict[str, object], *, before: Dict[str, object] | None = None) -> None:
    print(title)
    for k in KEYS:
        v = snap.get(k)
        mark = ""
        if before is not None and before.get(k) != v:
            mark = " *"
        print(f"  {k}={v}{mark}")
    print()


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ROI auto-detect diagnostics")
    p.add_argument("--quiet", action="store_true", help="Suppress detailed auto-detect probe logs")
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    print("=== ROI Auto-detect Diagnostics ===")
    print("This command does not write files. It only mutates in-process config values.")
    print()

    before = _snapshot()
    _print_snapshot("Before:", before)

    try:
        res = autodetect_and_patch_config(log_fn=None if bool(args.quiet) else print)
    except Exception as e:
        print(f"auto-detect failed: {e}")
        return 2

    after = _snapshot()

    print("Discovery result:")
    for k, v in asdict(res).items():
        if v is None or v == "":
            print(f"  {k}: --")
        else:
            print(f"  {k}: {v}")
    print()

    _print_snapshot("After (changed fields marked with *):", after, before=before)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

