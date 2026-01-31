"""USB / VISA device discovery for Raspberry Pi.

Goal: make PiPAT resilient to /dev/ttyUSB* renumbering.

We try to discover:
  - MULTI_METER_PATH (USB-serial multimeter)
  - MRSIGNAL_PORT (USB-serial Modbus)
  - AFG_VISA_ID (PyVISA ASRL resource)
  - ELOAD_VISA_ID (PyVISA USBTMC resource)

Discovery is best-effort and safe:
  - We keep timeouts short.
  - We prefer stable symlinks under /dev/serial/by-id when available.
  - If a configured value already works, we keep it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pyvisa
import serial
from serial.tools import list_ports

import config
from mrsignal import MrSignalClient


LogFn = Callable[[str], None]


def _split_hints(s: str) -> List[str]:
    toks = []
    for t in (s or "").split(","):
        t = t.strip().lower()
        if t:
            toks.append(t)
    return toks


def _contains_any(hay: str, needles: Sequence[str]) -> bool:
    h = (hay or "").lower()
    return any(n in h for n in needles)


def _log(log_fn: Optional[LogFn], msg: str) -> None:
    if log_fn:
        try:
            log_fn(msg)
            return
        except Exception:
            pass
    print(msg)


def _stable_serial_path(dev: str, prefer_by_id: bool = True) -> str:
    """Return a stable /dev/serial/by-id (or by-path) symlink if it exists."""

    dev = os.path.realpath(dev)

    def _search_dir(d: str) -> Optional[str]:
        if not os.path.isdir(d):
            return None
        try:
            for name in sorted(os.listdir(d)):
                p = os.path.join(d, name)
                try:
                    if os.path.realpath(p) == dev:
                        return p
                except Exception:
                    continue
        except Exception:
            return None
        return None

    by_id = _search_dir("/dev/serial/by-id")
    by_path = _search_dir("/dev/serial/by-path")
    if prefer_by_id and by_id:
        return by_id
    if by_path:
        return by_path
    if by_id:
        return by_id
    return dev


def _serial_candidates() -> List[str]:
    """Return candidate serial device nodes (e.g. /dev/ttyUSB0, /dev/ttyACM0)."""
    out = []
    try:
        for p in list_ports.comports():
            if p.device:
                out.append(p.device)
    except Exception:
        pass
    # De-dupe while preserving order
    seen = set()
    uniq = []
    for d in out:
        if d not in seen:
            uniq.append(d)
            seen.add(d)
    return uniq


def _probe_multimeter_idn(port: str, baud: int) -> Optional[str]:
    """Try to read an ASCII *IDN? response from a serial multimeter."""
    try:
        with serial.Serial(
            port,
            int(baud),
            timeout=0.2,
            write_timeout=0.2,
        ) as s:
            try:
                s.reset_input_buffer()
                s.reset_output_buffer()
            except Exception:
                pass
            s.write(b"*IDN?\n")
            s.flush()
            time.sleep(0.05)

            # Some instruments echo the command then return IDN on the next line.
            idn: Optional[str] = None
            for _ in range(int(getattr(config, "MULTI_METER_IDN_READ_LINES", 4))):
                raw = s.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                if line.upper().startswith("*IDN?"):
                    continue
                # Prefer an IDN-like line.
                if ("," in line) or ("multimeter" in line.lower()) or ("5491" in line.lower()):
                    idn = line
                    break
                if idn is None:
                    idn = line
            return idn or None
    except Exception:
        return None


def _try_mrsignal_on_port(port: str) -> Tuple[bool, Optional[int]]:
    """Return (ok, device_id)"""
    try:
        client = MrSignalClient(
            port=port,
            slave_id=int(getattr(config, "MRSIGNAL_SLAVE_ID", 1)),
            baud=int(getattr(config, "MRSIGNAL_BAUD", 9600)),
            parity=str(getattr(config, "MRSIGNAL_PARITY", "N")),
            stopbits=int(getattr(config, "MRSIGNAL_STOPBITS", 1)),
            timeout_s=float(getattr(config, "MRSIGNAL_TIMEOUT", 0.5)),
            float_byteorder=(str(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER", "") or "").strip() or None),
            float_byteorder_auto=bool(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER_AUTO", True)),
        )
        client.connect()
        st = client.read_status()
        client.close()
        if st and st.device_id is not None:
            return True, int(st.device_id)
    except Exception:
        pass
    return False, None


def _visa_rm() -> Optional[pyvisa.ResourceManager]:
    """Create a ResourceManager; prefer configured backend if set."""
    backend = str(getattr(config, "AUTO_DETECT_VISA_BACKEND", "") or "").strip()
    try:
        if backend:
            return pyvisa.ResourceManager(backend)
        return pyvisa.ResourceManager()
    except Exception:
        # Best-effort fallback to pyvisa-py
        try:
            return pyvisa.ResourceManager("@py")
        except Exception:
            return None


def _probe_visa_idn(rm: pyvisa.ResourceManager, rid: str) -> Optional[str]:
    try:
        inst = rm.open_resource(rid)
        try:
            inst.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
        except Exception:
            pass

        # Be friendly to serial instruments
        try:
            inst.read_termination = "\n"
            inst.write_termination = "\n"
        except Exception:
            pass
        try:
            # some serial SCPI devices need baud set
            if rid.startswith("ASRL"):
                try:
                    inst.baud_rate = 115200
                except Exception:
                    pass
        except Exception:
            pass

        try:
            txt = str(inst.query("*IDN?")).strip()
        finally:
            try:
                inst.close()
            except Exception:
                pass
        return txt or None
    except Exception:
        return None


@dataclass
class DiscoveryResult:
    multimeter_path: Optional[str] = None
    multimeter_idn: Optional[str] = None
    mrsignal_port: Optional[str] = None
    mrsignal_id: Optional[int] = None
    afg_visa_id: Optional[str] = None
    afg_idn: Optional[str] = None
    eload_visa_id: Optional[str] = None
    eload_idn: Optional[str] = None


def autodetect_and_patch_config(*, log_fn: Optional[LogFn] = None) -> DiscoveryResult:
    """Best-effort discovery. Mutates the imported config module in-place."""

    res = DiscoveryResult()

    if not bool(getattr(config, "AUTO_DETECT_ENABLE", True)):
        return res

    verbose = bool(getattr(config, "AUTO_DETECT_VERBOSE", True))
    prefer_by_id = bool(getattr(config, "AUTO_DETECT_PREFER_BY_ID", True))

    mm_hints = _split_hints(str(getattr(config, "AUTO_DETECT_MMETER_IDN_HINTS", "") or ""))
    mrs_enabled = bool(getattr(config, "MRSIGNAL_ENABLE", False))
    afg_hints = _split_hints(str(getattr(config, "AUTO_DETECT_AFG_IDN_HINTS", "") or ""))
    eload_hints = _split_hints(str(getattr(config, "AUTO_DETECT_ELOAD_IDN_HINTS", "") or ""))

    # --- Serial discovery (multimeter + MrSignal) ---
    ports = _serial_candidates()
    if verbose:
        _log(log_fn, f"[autodetect] serial ports: {ports}")

    # Multimeter: keep current if it answers with expected IDN
    if bool(getattr(config, "AUTO_DETECT_MMETER", True)):
        cur = str(getattr(config, "MULTI_METER_PATH", "") or "").strip()
        if cur:
            idn = _probe_multimeter_idn(cur, int(getattr(config, "MULTI_METER_BAUD", 38400)))
            if idn and (not mm_hints or _contains_any(idn, mm_hints)):
                res.multimeter_path = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
                res.multimeter_idn = idn
        if not res.multimeter_path:
            for dev in ports:
                idn = _probe_multimeter_idn(dev, int(getattr(config, "MULTI_METER_BAUD", 38400)))
                if not idn:
                    continue
                if mm_hints and not _contains_any(idn, mm_hints):
                    continue
                res.multimeter_path = _stable_serial_path(dev, prefer_by_id=prefer_by_id)
                res.multimeter_idn = idn
                break
        if res.multimeter_path:
            setattr(config, "MULTI_METER_PATH", res.multimeter_path)
            if verbose:
                _log(log_fn, f"[autodetect] multimeter: {res.multimeter_path} ({res.multimeter_idn})")

    # MrSignal: only if enabled
    if mrs_enabled and bool(getattr(config, "AUTO_DETECT_MRSIGNAL", True)):
        # Keep current if it works
        cur = str(getattr(config, "MRSIGNAL_PORT", "") or "").strip()
        if cur:
            ok, dev_id = _try_mrsignal_on_port(cur)
            if ok:
                res.mrsignal_port = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
                res.mrsignal_id = dev_id
        if not res.mrsignal_port:
            for dev in ports:
                # Avoid probing the same port chosen for the multimeter
                if res.multimeter_path and os.path.realpath(dev) == os.path.realpath(res.multimeter_path):
                    continue
                ok, dev_id = _try_mrsignal_on_port(dev)
                if not ok:
                    continue
                res.mrsignal_port = _stable_serial_path(dev, prefer_by_id=prefer_by_id)
                res.mrsignal_id = dev_id
                break
        if res.mrsignal_port:
            setattr(config, "MRSIGNAL_PORT", res.mrsignal_port)
            if verbose:
                _log(log_fn, f"[autodetect] mrsignal: {res.mrsignal_port} (id={res.mrsignal_id})")

    # --- VISA discovery (E-Load + AFG) ---
    if bool(getattr(config, "AUTO_DETECT_VISA", True)):
        rm = _visa_rm()
        if rm is None:
            if verbose:
                _log(log_fn, "[autodetect] pyvisa resource manager unavailable; skipping VISA discovery")
        else:
            try:
                rids = list(rm.list_resources())
            except Exception:
                rids = []
            # Narrow to USB + serial instruments
            cand = [r for r in rids if r.startswith("USB") or r.startswith("ASRL")]
            if verbose:
                _log(log_fn, f"[autodetect] visa resources: {cand}")

            idn_map: Dict[str, str] = {}
            for rid in cand:
                idn = _probe_visa_idn(rm, rid)
                if idn:
                    idn_map[rid] = idn
                    if verbose:
                        _log(log_fn, f"[autodetect] visa idn: {rid} -> {idn}")

            # Prefer configured patterns first
            cfg_eload_pat = str(getattr(config, "ELOAD_VISA_ID", "") or "").strip()
            cfg_afg = str(getattr(config, "AFG_VISA_ID", "") or "").strip()

            # E-load
            if bool(getattr(config, "AUTO_DETECT_ELOAD", True)):
                # 1) if current config matches a discovered resource, keep it
                chosen = None
                chosen_idn = None
                for rid, idn in idn_map.items():
                    try:
                        import fnmatch
                        if cfg_eload_pat and fnmatch.fnmatch(rid, cfg_eload_pat):
                            chosen = rid
                            chosen_idn = idn
                            break
                    except Exception:
                        pass
                # 2) otherwise match by IDN hints
                if not chosen and eload_hints:
                    for rid, idn in idn_map.items():
                        if rid.startswith("USB") and _contains_any(idn, eload_hints):
                            chosen = rid
                            chosen_idn = idn
                            break
                if chosen:
                    res.eload_visa_id = chosen
                    res.eload_idn = chosen_idn
                    setattr(config, "ELOAD_VISA_ID", chosen)
                    if verbose:
                        _log(log_fn, f"[autodetect] eload: {chosen} ({chosen_idn})")

            # AFG
            if bool(getattr(config, "AUTO_DETECT_AFG", True)):
                chosen = None
                chosen_idn = None
                # 1) if configured AFG is present and responds, keep it
                if cfg_afg and cfg_afg in idn_map:
                    chosen = cfg_afg
                    chosen_idn = idn_map.get(cfg_afg)
                # 2) otherwise match by IDN hints over ASRL resources
                if not chosen and afg_hints:
                    for rid, idn in idn_map.items():
                        if rid.startswith("ASRL") and _contains_any(idn, afg_hints):
                            chosen = rid
                            chosen_idn = idn
                            break
                # 3) as a fallback, pick *any* ASRL device with an IDN response
                if not chosen:
                    for rid, idn in idn_map.items():
                        if rid.startswith("ASRL"):
                            chosen = rid
                            chosen_idn = idn
                            break
                if chosen:
                    res.afg_visa_id = chosen
                    res.afg_idn = chosen_idn
                    setattr(config, "AFG_VISA_ID", chosen)
                    if verbose:
                        _log(log_fn, f"[autodetect] afg: {chosen} ({chosen_idn})")

            try:
                rm.close()
            except Exception:
                pass

    return res
