from __future__ import annotations

import math
import time


class FakeSerial:
    def __init__(self, lines: list[bytes] | None = None):
        self._lines = list(lines or [])
        self.writes: list[bytes] = []
        self.reset_in_calls = 0
        self.flush_calls = 0

    def write(self, b: bytes) -> int:
        self.writes.append(bytes(b))
        return len(b)

    def flush(self) -> None:
        self.flush_calls += 1

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""

    def reset_input_buffer(self) -> None:
        self.reset_in_calls += 1


def test_extract_floats_filters_bad():
    from bk5491b import _extract_floats

    assert _extract_floats("abc") == []
    assert _extract_floats("1,2") == [1.0, 2.0]
    assert _extract_floats("-1.2e3") == [-1200.0]


def test_query_line_skips_echo_and_empty():
    from bk5491b import BK5491B

    # Instrument echoes command, then returns a response.
    ser = FakeSerial(
        [
            b":FETC?\n",  # echo
            b"\n",        # empty
            b"  1.25,2.5  \n",
        ]
    )
    dmm = BK5491B(ser)
    line = dmm.query_line(":FETC?", read_lines=6)
    assert line == "1.25,2.5"


def test_fetch_values_overload_to_nan():
    from bk5491b import BK5491B

    ser = FakeSerial([b"9.9E37, 1.0\n"])
    dmm = BK5491B(ser)
    r = dmm.fetch_values(":FETC?")
    assert math.isnan(r.primary)
    assert r.secondary == 1.0


def test_query_values_tuple_shape():
    from bk5491b import BK5491B

    ser = FakeSerial([b"1.0,2.0\n"])
    dmm = BK5491B(ser)
    p, s, raw = dmm.query_values(":FETC?")
    assert p == 1.0
    assert s == 2.0
    assert "1.0" in raw


def test_system_error_uses_correct_scpi():
    from bk5491b import BK5491B

    ser = FakeSerial([b"0,No error\n"])
    dmm = BK5491B(ser)
    _ = dmm.system_error()
    assert any(b":SYSTem:ERRor?" in w for w in ser.writes)


def test_drain_errors_preview(monkeypatch):
    from bk5491b import BK5491B

    logs: list[str] = []
    ser = FakeSerial(
        [
            b"-100,BUS\n",
            b"-200,FAIL\n",
            b"-300,NO\n",
            b"-400,NO\n",
            b"0,No error\n",
        ]
    )
    dmm = BK5491B(ser, log_fn=logs.append)
    out = dmm.drain_errors(max_n=10, log=True)
    assert len(out) >= 2
    assert logs
    assert "..." in logs[0]


def test_write_respects_clear_input_and_delay(monkeypatch):
    from bk5491b import BK5491B

    slept: list[float] = []

    def fake_sleep(dt: float):
        slept.append(float(dt))

    monkeypatch.setattr(time, "sleep", fake_sleep)

    ser = FakeSerial([])
    dmm = BK5491B(ser)
    dmm.write("CONF:VOLT:DC", delay_s=0.01, clear_input=True)
    assert ser.reset_in_calls == 1
    assert slept == [0.01]


def test_func_helpers():
    from bk5491b import MmeterFunc, func_name, func_unit

    assert func_name(MmeterFunc.VDC) == "VDC"
    assert func_unit(MmeterFunc.VDC) == "V"
    assert func_unit(MmeterFunc.RES) == "Ohm"


def test_func_unit_covers_more_branches():
    from bk5491b import MmeterFunc, func_unit

    assert func_unit(MmeterFunc.IDC) == "A"
    assert func_unit(MmeterFunc.IAC) == "A"
    assert func_unit(MmeterFunc.FREQ) == "Hz"
    assert func_unit(MmeterFunc.PERIOD) == "s"
    assert func_unit(MmeterFunc.DIODE) == "V"
    assert func_unit(MmeterFunc.CONT) == "Ohm"
    assert func_unit(255) == ""


def test_extract_floats_exception_path(monkeypatch):
    import bk5491b

    # Force float(...) to raise for a specific token to exercise the exception path.
    import builtins

    real_float = builtins.float

    def bad_float(token):
        if str(token) == "1":
            raise ValueError("boom")
        return real_float(token)

    monkeypatch.setattr(bk5491b, "float", bad_float, raising=False)
    assert bk5491b._extract_floats("1 2") == [2.0]


class RaisingSerial(FakeSerial):
    def flush(self) -> None:  # type: ignore[override]
        raise RuntimeError("flush")

    def reset_input_buffer(self) -> None:  # type: ignore[override]
        raise RuntimeError("reset")


def test_write_swallow_serial_exceptions_and_delay(monkeypatch):
    from bk5491b import BK5491B

    slept: list[float] = []

    def fake_sleep(dt: float):
        slept.append(float(dt))

    monkeypatch.setattr(time, "sleep", fake_sleep)

    ser = RaisingSerial([])
    dmm = BK5491B(ser)
    # Should not raise even though reset_input_buffer/flush fail.
    dmm.write("CONF:VOLT:DC", delay_s=0.01, clear_input=True)
    assert slept == [0.01]


def test_query_line_can_return_empty(monkeypatch):
    from bk5491b import BK5491B

    # No non-empty line is returned.
    ser = RaisingSerial([b"", b"", b""])
    dmm = BK5491B(ser)
    assert dmm.query_line(":FETC?", read_lines=3, delay_s=0.0, clear_input=True) == ""


def test_drain_errors_breaks_on_empty_line():
    from bk5491b import BK5491B

    # system_error() will see no response and return ""; drain should stop.
    ser = FakeSerial([b"", b""])
    dmm = BK5491B(ser)
    assert dmm.drain_errors(max_n=3, log=True) == []


def test_fetch_values_empty_and_non_numeric_and_secondary_overload():
    from bk5491b import BK5491B

    # Empty response
    ser1 = FakeSerial([b"", b""])
    dmm1 = BK5491B(ser1)
    r1 = dmm1.fetch_values(":FETC?", read_lines=2)
    assert r1.primary is None and r1.secondary is None and r1.raw == ""

    # Non-numeric response
    ser2 = FakeSerial([b"abc\n"])
    dmm2 = BK5491B(ser2)
    r2 = dmm2.fetch_values(":FETC?")
    assert r2.primary is None and r2.secondary is None and r2.raw == "abc"

    # Secondary overload
    ser3 = FakeSerial([b"1.0,9.9E37\n"])
    dmm3 = BK5491B(ser3)
    r3 = dmm3.fetch_values(":FETC?")
    assert r3.primary == 1.0
    assert math.isnan(r3.secondary)

    # Single value -> secondary None
    ser4 = FakeSerial([b"5\n"])
    dmm4 = BK5491B(ser4)
    r4 = dmm4.fetch_values(":FETC?")
    assert r4.primary == 5.0
    assert r4.secondary is None
