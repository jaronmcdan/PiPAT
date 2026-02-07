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
