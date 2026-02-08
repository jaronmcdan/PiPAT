#!/usr/bin/env python3
"""mmeter_diag.py - quick SCPI smoke test for the B&K Precision 2831E/5491B.

This script is intentionally minimal and uses the same BK5491B helper that
ROI uses. It's useful when the meter front panel shows a persistent "BUS"
error or when you want to confirm which SCPI commands your meter supports.

It will:
  1) query *IDN?
  2) drain the error queue (:SYST:ERRor?)
  3) detect/query the measurement function dialect (FUNC vs CONF)
  4) optionally enable secondary display (FUNC2) when supported
  5) fetch readings (tries MULTI_METER_FETCH_CMDS)

Run it with the same user as ROI.
"""

from __future__ import annotations

import argparse
import serial
import time

from .. import config
from ..devices.bk5491b import BK5491B


def _looks_conf(resp: str) -> bool:
    r = (resp or "").upper()
    return any(tok in r for tok in ("DCV", "ACV", "DCA", "ACA", "HZ", "RES", "DIOC", "NONE"))


def _looks_func(resp: str) -> bool:
    r = (resp or "").upper()
    return any(tok in r for tok in ("VOLT", "CURR", "RES", "FREQ", "PER", "DIO", "CONT"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=getattr(config, "MULTI_METER_PATH", "/dev/ttyUSB0"))
    ap.add_argument("--baud", type=int, default=int(getattr(config, "MULTI_METER_BAUD", 38400)))
    ap.add_argument("--timeout", type=float, default=float(getattr(config, "MULTI_METER_TIMEOUT", 1.0)))
    ap.add_argument(
        "--style",
        default=str(getattr(config, "MMETER_SCPI_STYLE", "auto")).strip().lower(),
        choices=["auto", "func", "conf"],
        help="SCPI dialect to use (default: auto)",
    )
    args = ap.parse_args()

    print(f"Opening {args.port} @ {args.baud}...")
    s = serial.Serial(args.port, args.baud, timeout=args.timeout, write_timeout=args.timeout)
    try:
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
        except Exception:
            pass

        h = BK5491B(s, log_fn=print)

        # *IDN?
        idn = h.query_line("*IDN?", delay_s=0.05, read_lines=6)
        print("*IDN? ->", idn)

        # Drain error queue controlledly.
        h.drain_errors(max_n=8, log=True)

        style = args.style
        if style == "auto":
            # Try CONF first (more backwards-compatible).
            resp = h.query_line(":CONFigure:FUNCtion?", delay_s=0.05, read_lines=6)
            if _looks_conf(resp):
                style = "conf"
            else:
                resp2 = h.query_line(":FUNCtion?", delay_s=0.05, read_lines=6)
                if _looks_func(resp2):
                    style = "func"
                else:
                    style = "conf"

        print("Detected/selected SCPI style ->", style)

        # Query function in the chosen dialect.
        if style == "conf":
            fn = h.query_line(":CONFigure:FUNCtion?", delay_s=0.05, read_lines=6)
            print(":CONF:FUNC? ->", fn)
        else:
            fn = h.query_line(":FUNCtion?", delay_s=0.05, read_lines=6)
            print(":FUNC? ->", fn)

        # Try setting primary function to DC volts (safe default)
        if style == "conf":
            print("Setting primary function: CONF:VOLT:DC,@1")
            h.write("CONF:VOLT:DC,@1", delay_s=0.05)
        else:
            print("Setting primary function: :FUNCtion VOLTage:DC")
            h.write(":FUNCtion VOLTage:DC", delay_s=0.05)

        # Enable secondary and set function2 (func-dialect only)
        if style == "func":
            print("Enabling secondary display (FUNC2:STAT 1)...")
            h.write(":FUNCtion2:STATe 1", delay_s=0.05)
            # Per B&K 'Added Commands' doc, FUNC2 must be enabled first.
            print("Setting secondary function to VOLTage:DC (FUNC2 VOLTage:DC)...")
            h.write(":FUNCtion2 VOLTage:DC", delay_s=0.05)
        else:
            print("Skipping FUNC2 test (conf dialect)")

        # Fetch (try the same list ROI uses)
        cmds = [
            c.strip()
            for c in str(getattr(config, "MULTI_METER_FETCH_CMDS", ":FETC?,READ?")).split(",")
            if c.strip()
        ]
        print("Fetch candidates:", cmds)

        time.sleep(0.05)
        got = False
        for cmd in cmds:
            try:
                r = h.fetch_values(cmd, delay_s=0.02, read_lines=6)
                if r.primary is not None:
                    print(f"{cmd} -> {r.raw}")
                    print("primary:", r.primary, "secondary:", r.secondary)
                    got = True
                    break
                print(f"{cmd} -> (no numeric) {r.raw}")
            except Exception as e:
                print(f"{cmd} -> error: {e}")

        if not got:
            print("No fetch command returned a numeric reading.")

        # Final error queue
        h.drain_errors(max_n=8, log=True)

    finally:
        try:
            s.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
