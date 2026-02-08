"""Pytest configuration and dependency stubs.

This repo targets Raspberry Pi hardware and optional third-party libs
(python-can, pyserial, minimalmodbus, pyvisa...). The unit tests in
this suite focus on *core logic* and therefore provide lightweight stubs so the
modules can be imported and exercised in CI/container environments.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


# Ensure repo root is importable.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_stub(name: str, module: types.ModuleType) -> None:
    """Install a stub module into sys.modules if the real one isn't present."""
    if name not in sys.modules:
        sys.modules[name] = module


# --- Stub: serial (pyserial) -------------------------------------------------
serial_stub = types.ModuleType("serial")
serial_stub.PARITY_NONE = "N"
serial_stub.PARITY_EVEN = "E"
serial_stub.PARITY_ODD = "O"
serial_stub.STOPBITS_ONE = 1
serial_stub.STOPBITS_TWO = 2
_install_stub("serial", serial_stub)


# --- Stub: minimalmodbus ------------------------------------------------------
minimalmodbus_stub = types.ModuleType("minimalmodbus")
minimalmodbus_stub.MODE_RTU = 1

# Provide a few byteorder constants; values are arbitrary but must be unique.
minimalmodbus_stub.BYTEORDER_BIG = 0
minimalmodbus_stub.BYTEORDER_LITTLE = 1
minimalmodbus_stub.BYTEORDER_BIG_SWAP = 2
minimalmodbus_stub.BYTEORDER_LITTLE_SWAP = 3


class _StubSerialPort:
    def __init__(self) -> None:
        self.baudrate = None
        self.parity = None
        self.stopbits = None
        self.timeout = None
        self._closed = False

    def close(self) -> None:
        self._closed = True


class _StubInstrument:
    """A minimal minimalmodbus.Instrument compatible stub."""

    def __init__(self, port: str, slaveaddress: int, mode: int = 1):
        self.port = port
        self.address = slaveaddress
        self.mode = mode
        self.clear_buffers_before_each_transaction = False
        self.serial = _StubSerialPort()
        # Newer minimalmodbus has an attribute; older takes kwarg.
        self.byteorder = None

    # Note: explicit keyword args are important so mrsignal.call_compat keeps them.
    def read_register(self, registeraddress: int, number_of_decimals: int = 0, *, functioncode: int = 3, signed: bool = False):
        return 0

    def write_register(self, registeraddress: int, value: int, *, functioncode: int = 6, signed: bool = False):
        return None

    def read_float(self, registeraddress: int, *, functioncode: int = 3, number_of_registers: int = 2, byteorder=None):
        return 0.0

    def write_float(self, registeraddress: int, value: float, *, functioncode: int = 16, number_of_registers: int = 2, byteorder=None):
        return None


minimalmodbus_stub.Instrument = _StubInstrument
_install_stub("minimalmodbus", minimalmodbus_stub)


# --- Stub: python-can ---------------------------------------------------------
can_stub = types.ModuleType("can")


class BusABC:
    pass


class Message:
    def __init__(
        self,
        *,
        arbitration_id: int,
        data=None,
        is_extended_id: bool = False,
    ) -> None:
        self.arbitration_id = arbitration_id
        self.data = data
        self.is_extended_id = is_extended_id


can_stub.BusABC = BusABC
can_stub.Message = Message


def _default_bus_factory(**kwargs):
    # A simple object with send/recv methods; tests typically monkeypatch this.
    b = types.SimpleNamespace()
    b.kwargs = dict(kwargs)
    b.sent = []

    def send(msg):
        b.sent.append(msg)
        return None

    def recv(timeout: float | None = None):
        return None

    b.send = send
    b.recv = recv
    return b


can_stub.interface = types.SimpleNamespace(Bus=_default_bus_factory)
_install_stub("can", can_stub)


# --- Stub: hardware -----------------------------------------------------------
# device_comm imports HardwareManager from hardware.py, which has heavy deps.
# Provide a tiny placeholder type to satisfy imports.
hardware_stub = types.ModuleType("hardware")


class HardwareManager:  # pragma: no cover - used only for typing/imports
    pass


hardware_stub.HardwareManager = HardwareManager
_install_stub("hardware", hardware_stub)


# --- Stub: rmcanview ----------------------------------------------------------
# We omit rmcanview.py from coverage and treat it as an optional backend.
rmcanview_stub = types.ModuleType("rmcanview")


class RmCanViewBus(can_stub.BusABC):
    def __init__(self, channel, **kwargs):
        self.channel = channel
        self.kwargs = dict(kwargs)


rmcanview_stub.RmCanViewBus = RmCanViewBus
_install_stub("rmcanview", rmcanview_stub)
