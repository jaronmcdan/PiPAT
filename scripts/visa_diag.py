#!/usr/bin/env python3

"""Quick VISA / USBTMC diagnostics for Raspberry Pi.

Run:
  python3 scripts/visa_diag.py

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

# Allow running this script from any working directory.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from usbtmc_file import UsbTmcFileInstrument  # noqa: E402


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

    backends = []
    for b in [getattr(config, "VISA_BACKEND", ""), "@py", ""]:
        bs = (str(b) if b is not None else "").strip()
        if bs not in backends:
            backends.append(bs)

    for b in backends:
        print(f"--- Backend: {b or '<default>'} ---")
        try:
            rm, label = _rm_for_backend(b)
        except Exception as e:
            print(f"ResourceManager({b or '<default>'}) failed: {e}")
            print()
            continue

        try:
            resources = list(rm.list_resources())
        except Exception as e:
            resources = []
            print(f"list_resources() failed: {e}")

        print(f"Resources: {resources}")
        usb_resources = [r for r in resources if str(r).startswith("USB")]
        if usb_resources:
            print("USB resources (*IDN? probe):")
            for rid in usb_resources:
                try:
                    inst = rm.open_resource(rid)
                    try:
                        inst.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                    except Exception:
                        pass
                    try:
                        inst.read_termination = "\n"
                        inst.write_termination = "\n"
                    except Exception:
                        pass
                    try:
                        idn = str(inst.query("*IDN?")).strip()
                    finally:
                        try:
                            inst.close()
                        except Exception:
                            pass
                    print(f"  {rid} -> {idn}")
                except Exception as e:
                    print(f"  {rid} -> ERROR: {e}")
        else:
            print("No USB resources enumerated by this backend.")

        try:
            rm.close()
        except Exception:
            pass
        print()

    nodes = sorted(glob.glob("/dev/usbtmc*"))
    print(f"/dev/usbtmc nodes: {nodes}")
    if nodes:
        print("/dev/usbtmc* (*IDN? probe):")
        for p in nodes:
            dev = None
            try:
                dev = UsbTmcFileInstrument(p)
                dev.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                idn = str(dev.query("*IDN?")).strip()
                print(f"  {p} -> {idn}")
            except Exception as e:
                print(f"  {p} -> ERROR: {e}")
            finally:
                try:
                    if dev is not None:
                        dev.close()
                except Exception:
                    pass

    print()
    print("If you see no USB resources and no /dev/usbtmc nodes:")
    print("  - verify the USB cable and power")
    print("  - run: lsusb")
    print("  - ensure libusb + udev rules are installed (see README / scripts/pi_install.sh --easy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
