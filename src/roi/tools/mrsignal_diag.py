#!/usr/bin/env python3
"""Quick MrSignal / LANYI MR2.x Modbus diagnostics.

Preferred run methods:
  - roi-mrsignal-diag           (after install)
  - python -m roi.tools.mrsignal_diag
"""

from __future__ import annotations

import argparse
import time

from .. import config
from ..devices.mrsignal import MrSignalClient


def _fmt_opt(x) -> str:
    if x is None:
        return "--"
    return str(x)


def _fmt_float(x) -> str:
    if x is None:
        return "--"
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ROI MrSignal diagnostics")
    p.add_argument("--port", default=str(getattr(config, "MRSIGNAL_PORT", "/dev/ttyUSB1")))
    p.add_argument("--slave-id", type=int, default=int(getattr(config, "MRSIGNAL_SLAVE_ID", 1)))
    p.add_argument("--baud", type=int, default=int(getattr(config, "MRSIGNAL_BAUD", 9600)))
    p.add_argument("--parity", default=str(getattr(config, "MRSIGNAL_PARITY", "N")), choices=["N", "E", "O"])
    p.add_argument("--stopbits", type=int, default=int(getattr(config, "MRSIGNAL_STOPBITS", 1)), choices=[1, 2])
    p.add_argument("--timeout", type=float, default=float(getattr(config, "MRSIGNAL_TIMEOUT", 0.5)))
    p.add_argument(
        "--byteorder",
        default=str(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER", "") or "").strip(),
        help="minimalmodbus byteorder name, e.g. BYTEORDER_BIG_SWAP",
    )
    p.add_argument(
        "--byteorder-auto",
        action=argparse.BooleanOptionalAction,
        default=bool(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER_AUTO", True)),
        help="Enable/disable byteorder auto-detection",
    )
    p.add_argument("--read-count", type=int, default=1, help="How many status reads to perform")
    p.add_argument("--interval", type=float, default=0.5, help="Delay between reads")
    p.add_argument("--enable", type=int, choices=[0, 1], default=None, help="Set output enable (0/1)")
    p.add_argument("--set-mode", type=int, default=None, help="Set output mode register")
    p.add_argument("--set-value", type=float, default=None, help="Set output float value")
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()
    if int(args.read_count) <= 0:
        print("--read-count must be >= 1")
        return 2

    mode_given = args.set_mode is not None
    value_given = args.set_value is not None
    if mode_given != value_given:
        print("--set-mode and --set-value must be provided together")
        return 2

    # Safety: require explicit output enable when applying mode/value writes.
    if mode_given and (args.enable is None):
        print("--enable is required when using --set-mode/--set-value")
        return 2

    print("=== ROI MrSignal Diagnostics ===")
    print(
        f"port={args.port} slave_id={int(args.slave_id)} baud={int(args.baud)} "
        f"parity={args.parity} stopbits={int(args.stopbits)} timeout={float(args.timeout):.3f}s"
    )
    print(
        f"byteorder={(args.byteorder or '<default>')} "
        f"byteorder_auto={'yes' if bool(args.byteorder_auto) else 'no'}"
    )
    print()

    client = MrSignalClient(
        port=str(args.port),
        slave_id=int(args.slave_id),
        baud=int(args.baud),
        parity=str(args.parity).upper(),
        stopbits=int(args.stopbits),
        timeout_s=float(args.timeout),
        float_byteorder=(str(args.byteorder).strip() or None),
        float_byteorder_auto=bool(args.byteorder_auto),
    )

    try:
        client.connect()
        print("Connected.")

        if (args.enable is not None) and (not mode_given):
            en = bool(int(args.enable))
            print(f"Applying set_enable({1 if en else 0}) ...")
            client.set_enable(en)

        if mode_given:
            en = bool(int(args.enable))
            print(
                f"Applying set_output(enable={1 if en else 0}, "
                f"mode={int(args.set_mode)}, value={float(args.set_value)}) ..."
            )
            client.set_output(enable=en, output_select=int(args.set_mode), value=float(args.set_value))

        reads = int(args.read_count)
        for i in range(reads):
            st = client.read_status()
            out_state = "--" if st.output_on is None else ("ON" if bool(st.output_on) else "OFF")
            print(
                f"read {i+1}/{reads}: "
                f"id={_fmt_opt(st.device_id)} "
                f"out={out_state} "
                f"mode={_fmt_opt(st.mode_label)} "
                f"set={_fmt_float(st.output_value)} "
                f"in={_fmt_float(st.input_value)} "
                f"byteorder={_fmt_opt(st.float_byteorder)}"
            )

            if i + 1 < reads:
                time.sleep(max(0.0, float(args.interval)))

        return 0

    except Exception as e:
        print(f"MrSignal diag failed: {e}")
        return 2
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

