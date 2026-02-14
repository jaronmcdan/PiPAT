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
from dataclasses import dataclass

from .. import config
from ..devices.bk5491b import (
    BK5491B,
    FUNC_TO_NPLC_PREFIX_FUNC,
    FUNC_TO_RANGE_PREFIX_FUNC,
    FUNC_TO_REF_PREFIX_FUNC,
    FUNC_TO_SCPI_CONF,
    FUNC_TO_SCPI_FUNC,
    FUNC_TO_SCPI_FUNC2,
    MmeterFunc,
)


@dataclass
class _CmdProbe:
    name: str
    cmd: str
    is_query: bool = False
    note: str = ""


@dataclass
class _CmdResult:
    probe: _CmdProbe
    ok: bool
    response: str = ""
    error: str = ""


def _is_no_error(line: str) -> bool:
    u = (line or "").strip().upper()
    return (not u) or u.startswith("0") or ("NO ERROR" in u)


def _func_prefixes(d: dict[int, str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in d.values():
        s = str(p or "").strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _dedup_probes(probes: list[_CmdProbe]) -> list[_CmdProbe]:
    out: list[_CmdProbe] = []
    seen: set[tuple[str, bool]] = set()
    for p in probes:
        k = (str(p.cmd or "").strip().upper(), bool(p.is_query))
        if not k[0]:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _range_value_for_prefix(prefix: str) -> float:
    u = str(prefix or "").upper()
    if ":CURR" in u:
        return 0.1
    if ":RES" in u:
        return 1000.0
    return 10.0


def _primary_func_cmd(style: str, func_i: int) -> str:
    """Return the primary-function command ROI will attempt first for a function."""

    s = str(style or "").strip().lower()
    if s == "conf":
        base = str(FUNC_TO_SCPI_CONF.get(int(func_i), "") or "").strip()
        if not base:
            return ""
        # Match device_comm first-attempt behavior in conf mode.
        if ("@" not in base) and (":VOLT" in base or ":CURR" in base or ":FREQ" in base):
            return f"{base},@1"
        return base
    return str(FUNC_TO_SCPI_FUNC.get(int(func_i), "") or "").strip()


def _mmeter_style_auto_detect(h: BK5491B) -> str:
    # Try FUNC first for this family.
    resp = h.query_line(":FUNCtion?", delay_s=0.05, read_lines=6)
    if _looks_func(resp):
        return "func"
    resp = h.query_line(":CONFigure:FUNCtion?", delay_s=0.05, read_lines=6)
    if _looks_conf(resp):
        return "conf"
    return "func"


def _build_roi_command_matrix(
    *,
    style: str,
    fetch_cmds: list[str],
    include_conf_fallback: bool,
    mode: str = "full",
) -> list[_CmdProbe]:
    mode_s = str(mode or "full").strip().lower()
    if mode_s == "legacy":
        return _build_roi_command_matrix_legacy(style=style, fetch_cmds=fetch_cmds)
    if mode_s == "runtime":
        return _build_roi_command_matrix_runtime(style=style, fetch_cmds=fetch_cmds)
    return _build_roi_command_matrix_full(
        style=style,
        fetch_cmds=fetch_cmds,
        include_conf_fallback=include_conf_fallback,
    )


def _build_roi_command_matrix_legacy(
    *,
    style: str,
    fetch_cmds: list[str],
) -> list[_CmdProbe]:
    """Probe only legacy ROI meter paths (METER_MODE/METER_RANGE + fetch)."""

    cmds: list[_CmdProbe] = []
    cmds.append(_CmdProbe("idn", "*IDN?", is_query=True))

    if bool(getattr(config, "MMETER_CLEAR_ERRORS_ON_STARTUP", True)):
        cmds.append(_CmdProbe("error_queue", ":SYST:ERR?", is_query=True))

    legacy_funcs: list[int] = []
    if bool(getattr(config, "MMETER_LEGACY_MODE0_ENABLE", True)):
        legacy_funcs.append(int(MmeterFunc.VDC))
    if bool(getattr(config, "MMETER_LEGACY_MODE1_ENABLE", True)):
        legacy_funcs.append(int(MmeterFunc.IDC))

    for f_i in legacy_funcs:
        cmd = _primary_func_cmd(style, f_i)
        if cmd:
            cmds.append(_CmdProbe(f"legacy_set_func_{f_i}", cmd))

    if bool(getattr(config, "MMETER_LEGACY_RANGE_ENABLE", False)):
        for f_i in legacy_funcs:
            pfx = str(FUNC_TO_RANGE_PREFIX_FUNC.get(int(f_i), "") or "").strip()
            if not pfx:
                continue
            cmds.append(_CmdProbe(f"legacy_autorange_on_{pfx}", f"{pfx}:RANGe:AUTO ON"))
            cmds.append(_CmdProbe(f"legacy_autorange_off_{pfx}", f"{pfx}:RANGe:AUTO OFF"))

    for c in fetch_cmds:
        cc = str(c or "").strip()
        if cc:
            cmds.append(_CmdProbe(f"fetch_{cc}", cc, is_query=True))

    return _dedup_probes(cmds)


def _build_roi_command_matrix_runtime(
    *,
    style: str,
    fetch_cmds: list[str],
) -> list[_CmdProbe]:
    """Probe ROI commands that are reachable with current runtime config."""

    cmds = _build_roi_command_matrix_legacy(style=style, fetch_cmds=fetch_cmds)

    # Extended control path commands are optional and can be fully disabled.
    if bool(getattr(config, "MMETER_EXT_CTRL_ENABLE", True)):
        for f_i in FUNC_TO_SCPI_FUNC.keys():
            cmd = _primary_func_cmd(style, int(f_i))
            if cmd:
                cmds.append(_CmdProbe(f"ext_set_func_primary_{int(f_i)}", cmd))

        for p in _func_prefixes(FUNC_TO_RANGE_PREFIX_FUNC):
            cmds.append(_CmdProbe(f"ext_set_autorange_on_{p}", f"{p}:RANGe:AUTO ON"))
            cmds.append(_CmdProbe(f"ext_set_autorange_off_{p}", f"{p}:RANGe:AUTO OFF"))
            if bool(getattr(config, "MMETER_EXT_SET_RANGE_ENABLE", True)):
                rv = _range_value_for_prefix(p)
                cmds.append(_CmdProbe(f"ext_set_range_{p}", f"{p}:RANGe {rv:g}"))

        for p in _func_prefixes(FUNC_TO_NPLC_PREFIX_FUNC):
            cmds.append(_CmdProbe(f"ext_set_nplc_{p}", f"{p}:NPLCycles 1"))

        if bool(getattr(config, "MMETER_EXT_SECONDARY_ENABLE", True)):
            cmds.append(_CmdProbe("ext_set_secondary_enable_on", ":FUNCtion2:STATe 1"))
            for f_i, c in FUNC_TO_SCPI_FUNC2.items():
                cmds.append(_CmdProbe(f"ext_set_secondary_func_{int(f_i)}", str(c)))
            cmds.append(_CmdProbe("ext_set_secondary_enable_off", ":FUNCtion2:STATe 0"))

        for p in _func_prefixes(FUNC_TO_REF_PREFIX_FUNC):
            cmds.append(_CmdProbe(f"ext_set_relative_on_{p}", f"{p}:REFerence:STATe ON"))
            cmds.append(_CmdProbe(f"ext_acquire_relative_{p}", f"{p}:REFerence:ACQuire"))
            cmds.append(_CmdProbe(f"ext_set_relative_off_{p}", f"{p}:REFerence:STATe OFF"))

        cmds.append(_CmdProbe("ext_set_trigger_imm", ":TRIGger:SOURce IMM"))
        cmds.append(_CmdProbe("ext_set_trigger_bus", ":TRIGger:SOURce BUS"))
        cmds.append(_CmdProbe("ext_set_trigger_man", ":TRIGger:SOURce MAN"))
        cmds.append(_CmdProbe("ext_bus_trigger", "*TRG"))

    return _dedup_probes(cmds)


def _build_roi_command_matrix_full(
    *,
    style: str,
    fetch_cmds: list[str],
    include_conf_fallback: bool,
) -> list[_CmdProbe]:
    cmds: list[_CmdProbe] = []

    cmds.append(_CmdProbe("idn", "*IDN?", is_query=True))
    cmds.append(_CmdProbe("error_queue", ":SYST:ERR?", is_query=True))

    # Startup/style discovery queries ROI may issue.
    cmds.append(_CmdProbe("query_func", ":FUNCtion?", is_query=True))
    if style == "conf" or include_conf_fallback:
        cmds.append(_CmdProbe("query_conf_func", ":CONFigure:FUNCtion?", is_query=True))

    # Primary function selection commands used by ROI.
    if style in ("func", "auto"):
        for f_i, c in FUNC_TO_SCPI_FUNC.items():
            cmds.append(_CmdProbe(f"set_func_primary_{int(f_i)}", str(c)))

    if style == "conf" or include_conf_fallback:
        for f_i, c in FUNC_TO_SCPI_CONF.items():
            # ROI may synthesize variants in fallback mode.
            base = str(c).strip()
            if base:
                cmds.append(_CmdProbe(f"set_conf_primary_{int(f_i)}", base))
                if ("@" not in base) and (":VOLT" in base or ":CURR" in base or ":FREQ" in base):
                    cmds.append(_CmdProbe(f"set_conf_primary_ch1_{int(f_i)}", f"{base},@1"))
                if not base.startswith(":"):
                    cmds.append(_CmdProbe(f"set_conf_primary_colon_{int(f_i)}", f":{base}"))

    # EXT opcode tree (ROI command paths).
    cmds.append(_CmdProbe("set_secondary_enable_on", ":FUNCtion2:STATe 1"))
    for f_i, c in FUNC_TO_SCPI_FUNC2.items():
        cmds.append(_CmdProbe(f"set_secondary_func_{int(f_i)}", str(c)))
    cmds.append(_CmdProbe("set_secondary_enable_off", ":FUNCtion2:STATe 0"))

    for p in _func_prefixes(FUNC_TO_RANGE_PREFIX_FUNC):
        cmds.append(_CmdProbe(f"set_autorange_on_{p}", f"{p}:RANGe:AUTO ON"))
        cmds.append(_CmdProbe(f"set_autorange_off_{p}", f"{p}:RANGe:AUTO OFF"))
        rv = _range_value_for_prefix(p)
        cmds.append(_CmdProbe(f"set_range_{p}", f"{p}:RANGe {rv:g}"))

    for p in _func_prefixes(FUNC_TO_NPLC_PREFIX_FUNC):
        cmds.append(_CmdProbe(f"set_nplc_{p}", f"{p}:NPLCycles 1"))

    for p in _func_prefixes(FUNC_TO_REF_PREFIX_FUNC):
        cmds.append(_CmdProbe(f"set_relative_on_{p}", f"{p}:REFerence:STATe ON"))
        cmds.append(_CmdProbe(f"acquire_relative_{p}", f"{p}:REFerence:ACQuire"))
        cmds.append(_CmdProbe(f"set_relative_off_{p}", f"{p}:REFerence:STATe OFF"))

    cmds.append(_CmdProbe("set_trigger_imm", ":TRIGger:SOURce IMM"))
    cmds.append(_CmdProbe("set_trigger_bus", ":TRIGger:SOURce BUS"))
    cmds.append(_CmdProbe("set_trigger_man", ":TRIGger:SOURce MAN"))
    cmds.append(_CmdProbe("bus_trigger", "*TRG"))

    for c in fetch_cmds:
        cc = str(c or "").strip()
        if not cc:
            continue
        cmds.append(_CmdProbe(f"fetch_{cc}", cc, is_query=True))

    return _dedup_probes(cmds)


def _print_expected_settings(*, port: str, baud: int, style: str) -> None:
    print("Expected meter settings for ROI:")
    print(f"  - Serial link: {port} @ {baud} (8N1, newline-terminated SCPI)")
    print(f"  - SCPI style: {style} (ROI can use func/conf; func is preferred for 5491B)")
    print("  - Trigger source: ROI may set IMM/BUS/MAN during extended control tests")
    print("  - Range/NPLC/Relative/Function2: ROI may change these at runtime")
    print("  - Keep the port dedicated (no competing process touching the meter serial port)")
    print()


def _run_roi_command_probe(
    h: BK5491B,
    *,
    style: str,
    fetch_cmds: list[str],
    include_conf_fallback: bool,
    mode: str,
) -> int:
    probes = _build_roi_command_matrix(
        style=style,
        fetch_cmds=fetch_cmds,
        include_conf_fallback=include_conf_fallback,
        mode=mode,
    )

    print("=== ROI Meter Command Matrix ===")
    print(f"style={style} mode={mode} include_conf_fallback={bool(include_conf_fallback)}")
    print(f"commands={len(probes)}")

    results: list[_CmdResult] = []

    def _first_bad(lines: list[str]) -> str:
        bad = [e for e in (lines or []) if not _is_no_error(e)]
        return bad[0] if bad else ""

    for i, p in enumerate(probes, start=1):
        # Clear stale/late queue entries before this command.
        # If we see an error here, attribute it to the previous command (late arrival)
        # so the run cannot falsely report all-pass while the front panel showed an error.
        pre_bad = ""
        try:
            pre_errs = h.drain_errors(max_n=8, log=False)
            pre_bad = _first_bad(pre_errs)
        except Exception:
            pre_bad = ""
        if pre_bad:
            if results:
                prev = results[-1]
                if prev.ok:
                    prev.ok = False
                    prev.error = f"late error before next cmd: {pre_bad}"
            else:
                # Error existed before first probe command.
                results.append(
                    _CmdResult(
                        probe=_CmdProbe("preexisting_error_queue", "<pre-run>", is_query=True),
                        ok=False,
                        response="",
                        error=pre_bad,
                    )
                )

        response = ""
        exc_s = ""
        try:
            if p.is_query:
                response = h.query_line(p.cmd, delay_s=0.02, read_lines=6, clear_input=True)
            else:
                h.write(p.cmd, delay_s=0.02, clear_input=True)
        except Exception as e:
            exc_s = str(e)

        bad_err = ""
        try:
            # Some firmware reports command errors slightly later; sample twice.
            time.sleep(0.05)
            errs_a = h.drain_errors(max_n=6, log=False)
            bad_err = _first_bad(errs_a)
            if not bad_err:
                time.sleep(0.05)
                errs_b = h.drain_errors(max_n=6, log=False)
                bad_err = _first_bad(errs_b)
        except Exception as e:
            if not exc_s:
                exc_s = str(e)

        ok = (not exc_s) and (not bad_err)
        err = exc_s or bad_err
        results.append(_CmdResult(probe=p, ok=ok, response=response, error=err))

        kind = "Q" if p.is_query else "W"
        if ok:
            if p.is_query:
                print(f"[{i:03d}] OK   {kind} {p.cmd} -> {response or '<empty>'}")
            else:
                print(f"[{i:03d}] OK   {kind} {p.cmd}")
        else:
            print(f"[{i:03d}] FAIL {kind} {p.cmd} :: {err or 'unknown error'}")

    fail = [r for r in results if not r.ok]
    print()
    print(f"Summary: pass={len(results)-len(fail)} fail={len(fail)} total={len(results)}")

    if fail:
        print("Failing commands:")
        for r in fail:
            print(f"  - {r.probe.cmd} -> {r.error}")
        return 1

    return 0


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
    ap.add_argument(
        "--roi-cmds",
        action="store_true",
        help="Run full command matrix for all SCPI paths ROI may use with the meter",
    )
    ap.add_argument(
        "--include-conf-fallback",
        action="store_true",
        help="Also probe CONF fallback commands while in func mode",
    )
    ap.add_argument(
        "--roi-cmds-mode",
        default="full",
        choices=["full", "runtime", "legacy"],
        help="Command set scope for --roi-cmds: full superset, runtime-enabled paths, or legacy-only paths",
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
            style = _mmeter_style_auto_detect(h)

        print("Detected/selected SCPI style ->", style)
        _print_expected_settings(port=str(args.port), baud=int(args.baud), style=str(style))

        cmds = [
            c.strip()
            for c in str(getattr(config, "MULTI_METER_FETCH_CMDS", ":FETCh?,:FETC?")).split(",")
            if c.strip()
        ]

        if args.roi_cmds:
            return _run_roi_command_probe(
                h,
                style=style,
                fetch_cmds=cmds,
                include_conf_fallback=bool(args.include_conf_fallback),
                mode=str(args.roi_cmds_mode),
            )

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
