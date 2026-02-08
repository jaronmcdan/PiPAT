"""Minimal /dev/usbtmc* SCPI transport.

This is a **fallback** path for USBTMC instruments on Linux.

Why it exists:
  - PyVISA (pyvisa-py) normally talks to USBTMC devices via PyUSB/libusb.
  - On a fresh Raspberry Pi install, USB enumeration can fail if libusb is
    missing or if udev permissions prevent access to /dev/bus/usb.
  - The Linux kernel's `usbtmc` driver can expose compatible instruments as
    /dev/usbtmc0, /dev/usbtmc1, ... which can be used without PyUSB.

We only implement the tiny subset of the PyVISA instrument API that PiPAT
uses (`write`, `read`, `query`, `close`, plus a `timeout` attribute).

This code is intentionally conservative:
  - ASCII encoding with replacement on decode
  - single-threaded per-device use (PiPAT already uses locks)
  - bounded reads with select()-based timeouts
"""

from __future__ import annotations

import os
import select
import time
from dataclasses import dataclass
from typing import Optional


class UsbTmcError(Exception):
    pass


class UsbTmcTimeout(UsbTmcError, TimeoutError):
    pass


@dataclass
class UsbTmcFileInstrument:
    """A tiny SCPI I/O wrapper around a /dev/usbtmc* character device."""

    path: str
    timeout: int = 500  # ms (matches PyVISA semantics)
    read_termination: str = "\n"
    write_termination: str = "\n"

    def __post_init__(self) -> None:
        self._fd: Optional[int] = None

        # Open read/write so we can do SCPI queries.
        # Keep it blocking; we use select() to bound read time.
        try:
            self._fd = os.open(self.path, os.O_RDWR)
        except Exception as e:
            raise UsbTmcError(f"Failed to open {self.path}: {e}") from e

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise UsbTmcError("Device is closed")
        return int(self._fd)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def write(self, cmd: str) -> None:
        if cmd is None:
            return
        s = str(cmd)
        term = self.write_termination or ""
        if term and not s.endswith(term):
            s += term
        data = s.encode("ascii", errors="replace")
        try:
            os.write(self.fd, data)
        except Exception as e:
            raise UsbTmcError(f"Write failed ({self.path}): {e}") from e

    def read(self) -> str:
        """Read until `read_termination` (if set) or until timeout."""

        term = (self.read_termination or "").encode("ascii", errors="ignore")
        want_term = bool(term)

        deadline = time.monotonic() + max(0.0, float(self.timeout) / 1000.0)
        buf = bytearray()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsbTmcTimeout(f"Read timeout on {self.path}")

            try:
                r, _w, _x = select.select([self.fd], [], [], remaining)
            except Exception as e:
                raise UsbTmcError(f"select() failed ({self.path}): {e}") from e

            if not r:
                raise UsbTmcTimeout(f"Read timeout on {self.path}")

            try:
                chunk = os.read(self.fd, 4096)
            except Exception as e:
                raise UsbTmcError(f"Read failed ({self.path}): {e}") from e

            if not chunk:
                # EOF / device vanished
                break

            buf.extend(chunk)

            if want_term and term in buf:
                # Return up to (and including) the first termination.
                i = buf.index(term) + len(term)
                out = bytes(buf[:i])
                return out.decode("ascii", errors="replace").rstrip("\r\n")

            # Safety cap to avoid unbounded growth if the instrument misbehaves.
            if len(buf) > 256 * 1024:
                out = bytes(buf)
                return out.decode("ascii", errors="replace").rstrip("\r\n")

        # No termination seen; return whatever we got.
        return bytes(buf).decode("ascii", errors="replace").rstrip("\r\n")

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()
