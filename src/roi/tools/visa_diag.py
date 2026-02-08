#!/usr/bin/env python3
"""Quick VISA / USBTMC diagnostics.

Preferred run methods:
  - roi-visa-diag          (after install)
  - python -m roi.tools.visa_diag

This prints:
  - which PyVISA backend is being used
  - the resources that backend can enumerate
  - *IDN? responses for USB resources (safe)
  - any /dev/usbtmc* nodes and their *IDN? responses (fallback path)
"""

from __future__ import annotations

import glob
import os
import sys

import pyvisa

from .. import config
from ..devices.usbtmc_file import UsbTmcFileInstrument


def _try_import(name: str) -> str:
    try:
        mod = __import__(name)
        ver = getattr(mod, "__version__", "?")
        return f"{name}={ver}"
    except Exception as e:
        return f"{name}=<unavailable> ({e})"


def _rm_for_backend(backend: str):
    b = (backend or "").strip()
    if not b:
        return pyvisa.ResourceManager(), "<default>"
    return pyvisa.ResourceManager(b), b


def main() -> int:
    print("=== ROI VISA Diagnostics ===")
    print(f"Python: {sys.version.splitlines()[0]}")
    print(f"PyVISA: {getattr(pyvisa, '__version__', '?')}")
    print(_try_import("pyvisa_py"))
    print(_try_import("usb"))
    print(f"Configured VISA_BACKEND: {getattr(config, 'VISA_BACKEND', '') or '<default>'}")
    print(f"VISA_TIMEOUT_MS: {getattr(config, 'VISA_TIMEOUT_MS', 500)}")
    print()

    backends: list[str] = []
    for b in [getattr(config, "VISA_BACKEND", ""), "@py", ""]:
        bs = (str(b) if b is not None else "").strip()
        if bs not in backends:
            backends.append(bs)

    for backend in backends:
        try:
            rm, label = _rm_for_backend(backend)
        except Exception as e:
            print(f"-- ResourceManager({backend or '<default>'}) failed: {e}")
            continue

        print(f"-- ResourceManager backend: {label}")
        try:
            resources = list(rm.list_resources())
        except Exception as e:
            print(f"   list_resources failed: {e}")
            resources = []

        for r in resources:
            print("   ", r)
        if not resources:
            print("    (none)")
        print()

        # Probe USB instruments only (safe-ish). Avoid ASRL probing here.
        for r in resources:
            if not str(r).upper().startswith("USB"):
                continue
            try:
                inst = rm.open_resource(r)
                try:
                    inst.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                except Exception:
                    pass
                idn = inst.query("*IDN?").strip()
                print(f"   {r} -> {idn}")
            except Exception as e:
                print(f"   {r} -> <error> {e}")

        print()

    # Fallback: raw /dev/usbtmc* path reads
    devs = sorted(glob.glob("/dev/usbtmc*")) if os.name == "posix" else []
    if devs:
        print("-- /dev/usbtmc* fallback")
        for dev in devs:
            try:
                u = UsbTmcFileInstrument(dev)
                u.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                idn = u.query("*IDN?")
                print(f"   {dev} -> {idn}")
            except Exception as e:
                print(f"   {dev} -> <error> {e}")
            finally:
                try:
                    u.close()  # type: ignore[name-defined]
                except Exception:
                    pass
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
