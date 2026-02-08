from __future__ import annotations

import math


def test_call_compat_filters_kwargs():
    from mrsignal import call_compat

    def f(a, *, ok=None):
        return (a, ok)

    # Unsupported kwarg should be dropped.
    assert call_compat(f, 1, ok=2, nope=3) == (1, 2)


class FakeInst:
    def __init__(self):
        # Provide both older and newer minimalmodbus behaviors.
        self.byteorder = None
        self.serial = type("S", (), {"baudrate": None, "parity": None, "stopbits": None, "timeout": None, "close": lambda self: None})()
        self.clear_buffers_before_each_transaction = False
        self._float_map = {}
        self._reg_map = {}

    def read_register(self, reg, decimals=0, *, functioncode=3, signed=False):
        return int(self._reg_map.get(reg, 0))

    def write_register(self, reg, value, *, functioncode=6, signed=False):
        self._reg_map[reg] = int(value)

    def read_float(self, reg, *, functioncode=3, number_of_registers=2, byteorder=None):
        # If the library supports inst.byteorder, mrsignal will set that.
        bo = self.byteorder if self.byteorder is not None else byteorder
        return float(self._float_map.get((reg, bo), self._float_map.get((reg, "DEFAULT"), 0.0)))

    def write_float(self, reg, value, *, functioncode=16, number_of_registers=2, byteorder=None):
        bo = self.byteorder if self.byteorder is not None else byteorder
        self._float_map[(reg, bo)] = float(value)


def test_mrsignal_connect_and_close(monkeypatch):
    import minimalmodbus
    from mrsignal import MrSignalClient

    created = {}

    def fake_instrument(port, slave_id, mode=1):
        inst = FakeInst()
        created["inst"] = inst
        return inst

    monkeypatch.setattr(minimalmodbus, "Instrument", fake_instrument)

    c = MrSignalClient("/dev/ttyUSBX", slave_id=2, baud=19200, parity="E", stopbits=2, timeout_s=0.25)
    c.connect()
    assert c.inst is created["inst"]
    assert c.inst.serial.baudrate == 19200
    assert c.inst.serial.stopbits == 2
    assert c.inst.serial.timeout == 0.25
    c.close()  # should not raise


def test_read_float_configured_byteorder(monkeypatch):
    import minimalmodbus
    from mrsignal import MrSignalClient, REG_OUTPUT_VALUE_FLOAT

    # Make sure BYTEORDER_LITTLE exists in stub minimalmodbus.
    assert hasattr(minimalmodbus, "BYTEORDER_LITTLE")

    inst = FakeInst()
    inst._float_map[(REG_OUTPUT_VALUE_FLOAT, minimalmodbus.BYTEORDER_LITTLE)] = 12.5

    c = MrSignalClient("p", float_byteorder="BYTEORDER_LITTLE", float_byteorder_auto=False)
    c.inst = inst
    v, bo = c._read_float(REG_OUTPUT_VALUE_FLOAT)
    assert v == 12.5
    assert bo == "BYTEORDER_LITTLE"


def test_read_float_auto_detect_uses_sane_value(monkeypatch):
    import minimalmodbus
    from mrsignal import MrSignalClient, REG_INPUT_VALUE_FLOAT

    inst = FakeInst()
    # Give one byteorder a non-sane value and another a sane value.
    inst._float_map[(REG_INPUT_VALUE_FLOAT, minimalmodbus.BYTEORDER_BIG)] = float("nan")
    inst._float_map[(REG_INPUT_VALUE_FLOAT, minimalmodbus.BYTEORDER_LITTLE)] = 1.234

    c = MrSignalClient("p", float_byteorder=None, float_byteorder_auto=True)
    c.inst = inst
    v, bo = c._read_float(REG_INPUT_VALUE_FLOAT)
    assert abs(v - 1.234) < 1e-12
    assert bo in ("BYTEORDER_LITTLE", "BYTEORDER_BIG")  # depends on available_byteorders ordering


def test_set_output_order_and_clamps(monkeypatch):
    from mrsignal import MrSignalClient, REG_OUTPUT_ON, REG_OUTPUT_SELECT, REG_OUTPUT_VALUE_FLOAT

    inst = FakeInst()
    c = MrSignalClient("p")
    c.inst = inst

    # Disabling: output OFF first
    c.set_output(enable=False, output_select=1, value=2.0)
    assert inst._reg_map[REG_OUTPUT_ON] == 0
    assert inst._reg_map[REG_OUTPUT_SELECT] == 1
    assert inst._float_map[(REG_OUTPUT_VALUE_FLOAT, None)] == 2.0

    # Enabling: output ON last
    c.set_output(enable=True, output_select=0, value=3.0)
    assert inst._reg_map[REG_OUTPUT_SELECT] == 0
    assert inst._float_map[(REG_OUTPUT_VALUE_FLOAT, None)] == 3.0
    assert inst._reg_map[REG_OUTPUT_ON] == 1


def test_status_mode_label_unknown():
    from mrsignal import MrSignalStatus

    s = MrSignalStatus(output_select=123)
    assert "UNKNOWN" in s.mode_label


def test_available_byteorders_dedupe_and_helpers(monkeypatch):
    import minimalmodbus
    from mrsignal import available_byteorders, get_byteorder_by_name, is_sane_float, MrSignalStatus

    # Force a duplicate value to exercise the de-dupe branch.
    monkeypatch.setattr(minimalmodbus, "BYTEORDER_ABCD", minimalmodbus.BYTEORDER_BIG, raising=False)
    orders = available_byteorders()
    assert orders
    # Values should be unique.
    assert len({v for _, v in orders}) == len(orders)

    assert get_byteorder_by_name(None) is None
    assert get_byteorder_by_name("NOPE") is None
    assert get_byteorder_by_name("BYTEORDER_BIG") == minimalmodbus.BYTEORDER_BIG

    assert is_sane_float(1.0) is True
    assert is_sane_float(float("inf")) is False
    assert is_sane_float(1e9) is False
    assert MrSignalStatus(output_select=None).mode_label == "—"


def test_not_connected_raises_for_register_access():
    from mrsignal import MrSignalClient

    c = MrSignalClient("/dev/null")
    try:
        c._read_u16(0)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    try:
        c._write_u16(0, 1)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    try:
        c._read_float(0)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    try:
        c._write_float(0, 1.0)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_read_float_prefers_previous_auto_detect_byteorder(monkeypatch):
    import minimalmodbus
    from mrsignal import MrSignalClient, REG_INPUT_VALUE_FLOAT

    inst = FakeInst()
    inst._float_map[(REG_INPUT_VALUE_FLOAT, minimalmodbus.BYTEORDER_LITTLE)] = 7.7

    c = MrSignalClient("p", float_byteorder=None, float_byteorder_auto=True)
    c.inst = inst
    c._last_used_bo = "BYTEORDER_LITTLE"

    v, bo = c._read_float(REG_INPUT_VALUE_FLOAT)
    assert v == 7.7
    assert bo == "BYTEORDER_LITTLE"


def test_read_float_invalid_config_name_falls_back_to_default(monkeypatch):
    from mrsignal import MrSignalClient

    inst = FakeInst()
    inst._float_map[(0, "DEFAULT")] = 1.25
    c = MrSignalClient("p", float_byteorder="NOPE", float_byteorder_auto=False)
    c.inst = inst
    v, bo = c._read_float(0)
    assert v == 1.25
    assert bo == "DEFAULT"


class InstNoByteorder:
    """Instrument variant without a 'byteorder' attribute (older minimalmodbus)."""

    def __init__(self):
        self.serial = type("S", (), {"baudrate": None, "parity": None, "stopbits": None, "timeout": None, "close": lambda self: None})()
        self.clear_buffers_before_each_transaction = False
        self._floats = {}
        self._regs = {}

    def read_register(self, reg, decimals=0, *, functioncode=3, signed=False):
        return int(self._regs.get(reg, 0))

    def write_register(self, reg, value, *, functioncode=6, signed=False):
        self._regs[reg] = int(value)

    def read_float(self, reg, *, functioncode=3, number_of_registers=2, byteorder=None):
        return float(self._floats.get((reg, byteorder), 0.0))

    def write_float(self, reg, value, *, functioncode=16, number_of_registers=2, byteorder=None):
        self._floats[(reg, byteorder)] = float(value)


def test_read_write_float_without_inst_byteorder_attribute(monkeypatch):
    import minimalmodbus
    from mrsignal import MrSignalClient

    inst = InstNoByteorder()
    inst._floats[(1, minimalmodbus.BYTEORDER_BIG)] = 3.0

    c = MrSignalClient("p", float_byteorder="BYTEORDER_BIG", float_byteorder_auto=False)
    c.inst = inst
    v, bo = c._read_float(1)
    assert v == 3.0
    assert bo == "BYTEORDER_BIG"
    wbo = c._write_float(2, 4.0)
    assert wbo == "BYTEORDER_BIG"
    assert inst._floats[(2, minimalmodbus.BYTEORDER_BIG)] == 4.0


def test_auto_detect_continues_on_exception(monkeypatch):
    import minimalmodbus
    from mrsignal import MrSignalClient

    class Inst(FakeInst):
        def read_float(self, reg, *, functioncode=3, number_of_registers=2, byteorder=None):
            bo = self.byteorder if self.byteorder is not None else byteorder
            if bo == minimalmodbus.BYTEORDER_BIG:
                raise RuntimeError("bad")
            return 2.5

    inst = Inst()
    c = MrSignalClient("p", float_byteorder=None, float_byteorder_auto=True)
    c.inst = inst
    v, bo = c._read_float(0)
    assert v == 2.5
    assert bo != "DEFAULT"


def test_write_float_default_path(monkeypatch):
    from mrsignal import MrSignalClient

    inst = FakeInst()
    c = MrSignalClient("p", float_byteorder=None, float_byteorder_auto=False)
    c.inst = inst
    bo = c._write_float(10, 1.5)
    assert bo == "DEFAULT"
    assert inst._float_map[(10, None)] == 1.5


def test_read_status_handles_float_failures(monkeypatch):
    from mrsignal import MrSignalClient, REG_OUTPUT_VALUE_FLOAT, REG_INPUT_VALUE_FLOAT

    inst = FakeInst()
    inst._reg_map[0] = 55
    inst._reg_map[20] = 1
    inst._reg_map[21] = 6

    c = MrSignalClient("p")
    c.inst = inst

    calls = {"n": 0}

    def flaky(reg):
        calls["n"] += 1
        if reg == REG_OUTPUT_VALUE_FLOAT:
            raise RuntimeError("nope")
        if reg == REG_INPUT_VALUE_FLOAT:
            return (9.0, "DEFAULT")
        return (0.0, "DEFAULT")

    monkeypatch.setattr(c, "_read_float", flaky)
    st = c.read_status()
    assert st.device_id == 55
    assert st.output_value is None
    assert st.input_value == 9.0


def test_set_enable_writes_register(monkeypatch):
    from mrsignal import MrSignalClient, REG_OUTPUT_ON

    inst = FakeInst()
    c = MrSignalClient("p")
    c.inst = inst

    c.set_enable(True)
    assert inst._reg_map[REG_OUTPUT_ON] == 1
    c.set_enable(False)
    assert inst._reg_map[REG_OUTPUT_ON] == 0


def test_mrsignal_close_swallow_serial_close_error():
    """Cover MrSignalClient.close() exception swallow path."""
    from mrsignal import MrSignalClient

    class BadSerial:
        def close(self):
            raise RuntimeError("boom")

    inst = type("Inst", (), {"serial": BadSerial()})()

    c = MrSignalClient("p")
    c.inst = inst
    # Should not raise even though serial.close() blows up.
    c.close()
    assert c.inst is None


def test_read_float_uses_prev_byteorder_without_inst_byteorder_attr(monkeypatch):
    """Cover the prev-byteorder fast path when Instrument lacks .byteorder."""
    import minimalmodbus
    from mrsignal import MrSignalClient

    inst = InstNoByteorder()
    inst._floats[(1, minimalmodbus.BYTEORDER_BIG)] = 7.25

    c = MrSignalClient("p", float_byteorder=None, float_byteorder_auto=True)
    c.inst = inst
    c._last_used_bo = "BYTEORDER_BIG"

    v, bo = c._read_float(1)
    assert v == 7.25
    assert bo == "BYTEORDER_BIG"


def test_read_float_prev_byteorder_exception_is_swallowed(monkeypatch):
    """Cover the exception swallow path when using the previous auto-detected byteorder."""
    import mrsignal
    from mrsignal import MrSignalClient

    class Inst(InstNoByteorder):
        def read_float(self, reg, *, functioncode=3, number_of_registers=2, byteorder=None):
            if byteorder is not None:
                raise RuntimeError("bad")
            return 1.0

    inst = Inst()
    c = MrSignalClient("p", float_byteorder=None, float_byteorder_auto=True)
    c.inst = inst
    c._last_used_bo = "BYTEORDER_BIG"

    # Ensure we don't accidentally return from the auto-detect loop.
    monkeypatch.setattr(mrsignal, "available_byteorders", lambda: [])

    v, bo = c._read_float(0)
    assert v == 1.0
    assert bo == "DEFAULT"


def test_read_float_configured_byteorder_exception_falls_back(monkeypatch):
    """Cover the exception swallow path for configured byteorder reads."""
    import minimalmodbus
    from mrsignal import MrSignalClient

    class Inst(InstNoByteorder):
        def read_float(self, reg, *, functioncode=3, number_of_registers=2, byteorder=None):
            if byteorder == minimalmodbus.BYTEORDER_BIG:
                raise RuntimeError("bad")
            return 2.0

    inst = Inst()
    c = MrSignalClient("p", float_byteorder="BYTEORDER_BIG", float_byteorder_auto=False)
    c.inst = inst

    v, bo = c._read_float(0)
    assert v == 2.0
    assert bo == "DEFAULT"


def test_auto_detect_path_without_inst_byteorder_attr(monkeypatch):
    """Cover the auto-detect loop branch that passes byteorder=... kwarg."""
    import minimalmodbus
    from mrsignal import MrSignalClient

    inst = InstNoByteorder()
    inst._floats[(0, minimalmodbus.BYTEORDER_BIG)] = float("nan")
    inst._floats[(0, minimalmodbus.BYTEORDER_LITTLE)] = 3.5

    c = MrSignalClient("p", float_byteorder=None, float_byteorder_auto=True)
    c.inst = inst
    c._last_used_bo = "DEFAULT"  # skip prev fast-path

    v, bo = c._read_float(0)
    assert v == 3.5
    assert bo in {name for name, _ in __import__("mrsignal").available_byteorders()}


def test_write_float_sets_inst_byteorder_when_supported(monkeypatch):
    """Cover the write_float path for newer minimalmodbus Instrument.byteorder."""
    import minimalmodbus
    from mrsignal import MrSignalClient

    inst = FakeInst()
    c = MrSignalClient("p", float_byteorder="BYTEORDER_LITTLE", float_byteorder_auto=False)
    c.inst = inst

    bo = c._write_float(10, 1.25)
    assert bo == "BYTEORDER_LITTLE"
    assert inst._float_map[(10, minimalmodbus.BYTEORDER_LITTLE)] == 1.25


def test_read_status_handles_input_float_failure(monkeypatch):
    """Cover the input-value exception path in read_status()."""
    from mrsignal import MrSignalClient, REG_OUTPUT_VALUE_FLOAT, REG_INPUT_VALUE_FLOAT

    c = MrSignalClient("p")
    c.inst = FakeInst()

    def flaky(reg):
        if reg == REG_INPUT_VALUE_FLOAT:
            raise RuntimeError("nope")
        return (1.0, "DEFAULT")

    monkeypatch.setattr(c, "_read_float", flaky)
    st = c.read_status()
    assert st.output_value == 1.0
    assert st.input_value is None
