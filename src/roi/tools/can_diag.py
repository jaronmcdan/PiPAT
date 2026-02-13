#!/usr/bin/env python3
"""Quick CAN diagnostics for ROI backends.

Preferred run methods:
  - roi-can-diag             (after install)
  - python -m roi.tools.can_diag
"""

from __future__ import annotations

import argparse
import time
from typing import Iterable

import can

from .. import config
from ..can.comm import setup_can_interface, shutdown_can_interface


def _parse_data_bytes(text: str) -> bytes:
    s = (text or "").strip()
    if not s:
        return b""
    # Accept separators like spaces/colons/dashes/underscores.
    for ch in (" ", ":", "-", "_"):
        s = s.replace(ch, "")
    if len(s) % 2 != 0:
        raise ValueError("hex payload must have an even number of nibbles")
    return bytes.fromhex(s)


def _fmt_data(data: Iterable[int]) -> str:
    b = bytes(data)
    if not b:
        return "--"
    return b.hex(" ").upper()


def _fmt_id(arbitration_id: int, is_extended: bool) -> str:
    if is_extended:
        return f"0x{int(arbitration_id) & 0x1FFFFFFF:08X}"
    return f"0x{int(arbitration_id) & 0x7FF:03X}"


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ROI CAN backend diagnostics")
    p.add_argument("--interface", default=str(getattr(config, "CAN_INTERFACE", "socketcan")))
    p.add_argument("--channel", default=str(getattr(config, "CAN_CHANNEL", "can0")))
    p.add_argument("--bitrate", type=int, default=int(getattr(config, "CAN_BITRATE", 250000)))
    p.add_argument("--serial-baud", type=int, default=int(getattr(config, "CAN_SERIAL_BAUD", 115200)))
    p.add_argument("--setup", action="store_true", help="Bring up/configure CAN backend before opening")
    p.add_argument("--duration", type=float, default=5.0, help="How long to listen for frames")
    p.add_argument("--max-frames", type=int, default=50, help="Max frames to print (<=0 means unlimited)")
    p.add_argument("--rx-timeout", type=float, default=0.25, help="Per-recv timeout in seconds")
    p.add_argument("--send-id", default="", help="Optional test arbitration id, e.g. 0x123 or 0x18FF50E5")
    p.add_argument("--send-data", default="01 02 03 04", help="Optional hex payload for --send-id")
    p.add_argument("--standard-id", action="store_true", help="Force standard-id format for --send-id")
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    print("=== ROI CAN Diagnostics ===")
    print(f"interface={args.interface} channel={args.channel} bitrate={int(args.bitrate)}")
    print(f"serial_baud={int(args.serial_baud)} setup={'yes' if args.setup else 'no'}")
    print()

    # setup_can_interface() reads CAN backend selection from roi.config.
    old_iface = getattr(config, "CAN_INTERFACE", "socketcan")
    old_serial_baud = getattr(config, "CAN_SERIAL_BAUD", 115200)
    setattr(config, "CAN_INTERFACE", str(args.interface).strip().lower())
    setattr(config, "CAN_SERIAL_BAUD", int(args.serial_baud))

    cbus = None
    try:
        cbus = setup_can_interface(
            str(args.channel),
            int(args.bitrate),
            do_setup=bool(args.setup),
            log_fn=print,
        )
        if cbus is None:
            print("Failed to open CAN bus.")
            return 2

        print("CAN bus opened.")

        if str(args.send_id).strip():
            try:
                arb_id = int(str(args.send_id).strip(), 0)
                payload = _parse_data_bytes(str(args.send_data))
                is_extended = (not bool(args.standard_id)) and (arb_id > 0x7FF)
                msg = can.Message(
                    arbitration_id=int(arb_id),
                    data=payload,
                    is_extended_id=bool(is_extended),
                )
                cbus.send(msg)
                print(f"TX id={_fmt_id(arb_id, bool(is_extended))} dlc={len(payload)} data={_fmt_data(payload)}")
            except Exception as e:
                print(f"TX failed: {e}")
                return 2

        duration_s = max(0.0, float(args.duration))
        max_frames = int(args.max_frames)
        if max_frames <= 0:
            max_frames = 1_000_000_000

        start = time.monotonic()
        deadline = start + duration_s
        rx_count = 0
        seen_ids: set[int] = set()

        while (time.monotonic() < deadline) and (rx_count < max_frames):
            now = time.monotonic()
            remain = max(0.0, deadline - now)
            timeout = min(max(0.01, float(args.rx_timeout)), max(0.01, remain if duration_s > 0 else 0.01))
            msg = cbus.recv(timeout=timeout)
            if not msg:
                continue

            data = bytes(msg.data or b"")
            dlc = int(getattr(msg, "dlc", len(data)))
            is_ext = bool(getattr(msg, "is_extended_id", True))
            arb = int(getattr(msg, "arbitration_id", 0))
            seen_ids.add(arb)
            rx_count += 1
            print(f"RX id={_fmt_id(arb, is_ext)} dlc={dlc} data={_fmt_data(data)}")

        elapsed = max(0.0, time.monotonic() - start)
        fps = (float(rx_count) / elapsed) if elapsed > 0 else 0.0
        print()
        print(f"Summary: rx_frames={rx_count} unique_ids={len(seen_ids)} elapsed_s={elapsed:.2f} rx_fps={fps:.1f}")
        return 0

    finally:
        try:
            if cbus is not None:
                cbus.shutdown()
        except Exception:
            pass

        try:
            shutdown_can_interface(str(args.channel), do_setup=bool(args.setup))
        except Exception:
            pass

        setattr(config, "CAN_INTERFACE", old_iface)
        setattr(config, "CAN_SERIAL_BAUD", old_serial_baud)


if __name__ == "__main__":
    raise SystemExit(main())

