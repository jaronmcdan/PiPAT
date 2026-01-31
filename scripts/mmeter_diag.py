#!/usr/bin/env python3
"""mmeter_diag.py - quick SCPI smoke test for the B&K Precision 2831E/5491B.

This script is intentionally minimal and uses the same BK5491B helper that
PiPAT uses. It's useful when the meter front panel shows a persistent "BUS"
error or when you want to confirm which SCPI commands your meter supports.

It will:
  1) query *IDN?
  2) drain the error queue (:SYST:ERRor?)
  3) query :FUNCtion?
  4) enable secondary display, set :FUNCtion2 to VOLTage:DC (if supported)
  5) fetch readings (:FETC?)

Run it with the same user as PiPAT.
"""

from __future__ import annotations

import argparse
import serial
import time

import config
from bk5491b import BK5491B


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=getattr(config, "MULTI_METER_PATH", "/dev/ttyUSB0"))
    ap.add_argument("--baud", type=int, default=int(getattr(config, "MULTI_METER_BAUD", 38400)))
    ap.add_argument("--timeout", type=float, default=float(getattr(config, "MULTI_METER_TIMEOUT", 1.0)))
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

        # Query function.
        fn = h.query_line(":FUNCtion?", delay_s=0.05, read_lines=6)
        print(":FUNC? ->", fn)

        # Enable secondary and set function2 (if supported)
        print("Enabling secondary display (FUNC2:STAT 1)...")
        h.write(":FUNCtion2:STATe 1", delay_s=0.05)
        # Per B&K 'Added Commands' doc, FUNC2 must be enabled first.
        print("Setting secondary function to VOLTage:DC (FUNC2 VOLT:DC)...")
        h.write(":FUNCtion2 VOLTage:DC", delay_s=0.05)

        # Fetch
        time.sleep(0.05)
        r = h.fetch_values(":FETC?", delay_s=0.02, read_lines=6)
        print(":FETC? ->", r.raw)
        print("primary:", r.primary, "secondary:", r.secondary)

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
