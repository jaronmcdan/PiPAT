import types

import pytest


def test_usbtmc_file_write_adds_termination(monkeypatch):
    """write() should append the configured write termination when missing."""

    from usbtmc_file import UsbTmcFileInstrument

    written = []

    def fake_open(path, flags):
        assert path == "/dev/usbtmc0"
        return 3

    def fake_write(fd, data):
        assert fd == 3
        written.append(data)
        return len(data)

    def fake_close(fd):
        assert fd == 3

    monkeypatch.setattr("os.open", fake_open)
    monkeypatch.setattr("os.write", fake_write)
    monkeypatch.setattr("os.close", fake_close)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.write_termination = "\n"
    dev.write("*IDN?")
    dev.close()

    assert written, "expected a write"
    assert written[0].endswith(b"\n")


def test_usbtmc_file_query_reads_until_termination(monkeypatch):
    """query() should write then read until the configured termination."""

    from usbtmc_file import UsbTmcFileInstrument

    written = []
    reads = [b"GW,INSTEK,AFG-2125\n"]

    def fake_open(path, flags):
        return 4

    def fake_write(fd, data):
        written.append(data)
        return len(data)

    def fake_select(rlist, wlist, xlist, timeout):
        # Always ready for one read.
        return (rlist, [], [])

    def fake_read(fd, n):
        assert fd == 4
        if reads:
            return reads.pop(0)
        return b""

    def fake_close(fd):
        return None

    monkeypatch.setattr("os.open", fake_open)
    monkeypatch.setattr("os.write", fake_write)
    monkeypatch.setattr("select.select", fake_select)
    monkeypatch.setattr("os.read", fake_read)
    monkeypatch.setattr("os.close", fake_close)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.timeout = 100
    out = dev.query("*IDN?")
    dev.close()

    assert out == "GW,INSTEK,AFG-2125"
    assert written, "query() should write"
    assert written[0].startswith(b"*IDN?")


def test_usbtmc_file_read_timeout(monkeypatch):
    """read() should raise UsbTmcTimeout when select() times out."""

    from usbtmc_file import UsbTmcFileInstrument, UsbTmcTimeout

    def fake_open(path, flags):
        return 5

    def fake_select(rlist, wlist, xlist, timeout):
        # Not ready => timeout.
        return ([], [], [])

    def fake_close(fd):
        return None

    monkeypatch.setattr("os.open", fake_open)
    monkeypatch.setattr("select.select", fake_select)
    monkeypatch.setattr("os.close", fake_close)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.timeout = 1
    with pytest.raises(UsbTmcTimeout):
        dev.read()
    dev.close()


def test_usbtmc_file_open_error_raises_usbtmcerror(monkeypatch):
    from usbtmc_file import UsbTmcFileInstrument, UsbTmcError

    def fake_open(path, flags):
        raise OSError("nope")

    monkeypatch.setattr("os.open", fake_open)

    with pytest.raises(UsbTmcError):
        UsbTmcFileInstrument("/dev/usbtmc0")


def test_usbtmc_file_fd_property_raises_when_closed(monkeypatch):
    from usbtmc_file import UsbTmcFileInstrument, UsbTmcError

    monkeypatch.setattr("os.open", lambda path, flags: 99)
    monkeypatch.setattr("os.close", lambda fd: None)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.close()

    with pytest.raises(UsbTmcError):
        _ = dev.fd


def test_usbtmc_file_write_none_is_noop(monkeypatch):
    from usbtmc_file import UsbTmcFileInstrument

    monkeypatch.setattr("os.open", lambda path, flags: 3)
    monkeypatch.setattr("os.close", lambda fd: None)

    calls = {"n": 0}

    def fake_write(fd, data):
        calls["n"] += 1
        return len(data)

    monkeypatch.setattr("os.write", fake_write)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.write(None)  # type: ignore[arg-type]
    dev.close()

    assert calls["n"] == 0


def test_usbtmc_file_write_os_write_error(monkeypatch):
    from usbtmc_file import UsbTmcFileInstrument, UsbTmcError

    monkeypatch.setattr("os.open", lambda path, flags: 3)
    monkeypatch.setattr("os.close", lambda fd: None)

    def fake_write(fd, data):
        raise OSError("boom")

    monkeypatch.setattr("os.write", fake_write)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    with pytest.raises(UsbTmcError):
        dev.write("*IDN?")
    dev.close()


def test_usbtmc_file_read_timeout_before_select(monkeypatch):
    """Cover the remaining<=0 timeout path (no select call)."""
    from usbtmc_file import UsbTmcFileInstrument, UsbTmcTimeout

    monkeypatch.setattr("os.open", lambda path, flags: 7)
    monkeypatch.setattr("os.close", lambda fd: None)

    # Make monotonic constant so deadline==now.
    monkeypatch.setattr("time.monotonic", lambda: 0.0)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.timeout = 0

    with pytest.raises(UsbTmcTimeout):
        dev.read()

    dev.close()


def test_usbtmc_file_select_exception_raises_usbtmcerror(monkeypatch):
    from usbtmc_file import UsbTmcFileInstrument, UsbTmcError

    monkeypatch.setattr("os.open", lambda path, flags: 8)
    monkeypatch.setattr("os.close", lambda fd: None)

    def boom(*args, **kwargs):
        raise OSError("select")

    monkeypatch.setattr("select.select", boom)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.timeout = 10
    with pytest.raises(UsbTmcError):
        dev.read()
    dev.close()


def test_usbtmc_file_read_os_read_error(monkeypatch):
    from usbtmc_file import UsbTmcFileInstrument, UsbTmcError

    monkeypatch.setattr("os.open", lambda path, flags: 9)
    monkeypatch.setattr("os.close", lambda fd: None)
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, [], []))

    def boom(fd, n):
        raise OSError("read")

    monkeypatch.setattr("os.read", boom)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.timeout = 10
    with pytest.raises(UsbTmcError):
        dev.read()
    dev.close()


def test_usbtmc_file_eof_break_returns_buffer(monkeypatch):
    """Cover the EOF/break + final return (no termination) path."""
    from usbtmc_file import UsbTmcFileInstrument

    monkeypatch.setattr("os.open", lambda path, flags: 10)
    monkeypatch.setattr("os.close", lambda fd: None)

    # Always ready
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, [], []))

    reads = [b"ABC", b""]

    def fake_read(fd, n):
        return reads.pop(0)

    monkeypatch.setattr("os.read", fake_read)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.timeout = 10
    dev.read_termination = "\n"  # never seen

    out = dev.read()
    dev.close()

    assert out == "ABC"


def test_usbtmc_file_safety_cap_returns(monkeypatch):
    """Cover the safety cap that prevents unbounded buffer growth."""
    from usbtmc_file import UsbTmcFileInstrument

    monkeypatch.setattr("os.open", lambda path, flags: 11)
    monkeypatch.setattr("os.close", lambda fd: None)
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, [], []))

    big = b"A" * (256 * 1024 + 1)

    def fake_read(fd, n):
        return big

    monkeypatch.setattr("os.read", fake_read)

    dev = UsbTmcFileInstrument("/dev/usbtmc0")
    dev.timeout = 10
    dev.read_termination = "\n"  # not present in chunk

    out = dev.read()
    dev.close()

    assert out.startswith("A")
    assert len(out) == len(big)
