from __future__ import annotations

import sys
from types import SimpleNamespace


def test_can_diag_open_failure(monkeypatch, capsys):
    import roi.tools.can_diag as can_diag

    monkeypatch.setattr(can_diag, "setup_can_interface", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["roi-can-diag", "--duration", "0"])

    rc = can_diag.main()
    out = capsys.readouterr().out

    assert rc == 2
    assert "Failed to open CAN bus." in out


def test_can_diag_send_once(monkeypatch, capsys):
    import roi.tools.can_diag as can_diag

    class FakeBus:
        def __init__(self):
            self.sent = []
            self.closed = False

        def send(self, msg):
            self.sent.append(msg)

        def recv(self, timeout=0.0):
            return None

        def shutdown(self):
            self.closed = True

    fake_bus = FakeBus()
    monkeypatch.setattr(can_diag, "setup_can_interface", lambda *a, **k: fake_bus)
    monkeypatch.setattr(can_diag, "shutdown_can_interface", lambda *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "roi-can-diag",
            "--duration",
            "0",
            "--send-id",
            "0x123",
            "--send-data",
            "DE AD BE EF",
        ],
    )

    rc = can_diag.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert len(fake_bus.sent) == 1
    assert int(fake_bus.sent[0].arbitration_id) == 0x123
    assert bytes(fake_bus.sent[0].data) == bytes.fromhex("DEADBEEF")
    assert "Summary:" in out


def test_mrsignal_diag_read_only(monkeypatch, capsys):
    import roi.tools.mrsignal_diag as mrs_diag

    class FakeClient:
        def __init__(self, *a, **k):
            self.connected = False
            self.closed = False

        def connect(self):
            self.connected = True

        def close(self):
            self.closed = True

        def read_status(self):
            return SimpleNamespace(
                device_id=77,
                output_on=True,
                output_select=1,
                output_value=5.0,
                input_value=4.95,
                float_byteorder="DEFAULT",
                mode_label="V",
            )

        def set_enable(self, enable):  # pragma: no cover - should not be called in this test
            raise AssertionError("set_enable should not be called")

        def set_output(self, *, enable, output_select, value):  # pragma: no cover
            raise AssertionError("set_output should not be called")

    monkeypatch.setattr(mrs_diag, "MrSignalClient", FakeClient)
    monkeypatch.setattr(mrs_diag.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sys, "argv", ["roi-mrsignal-diag", "--read-count", "2", "--interval", "0"])

    rc = mrs_diag.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "Connected." in out
    assert "read 1/2" in out
    assert "read 2/2" in out


def test_mrsignal_diag_rejects_partial_set_args(monkeypatch, capsys):
    import roi.tools.mrsignal_diag as mrs_diag

    monkeypatch.setattr(sys, "argv", ["roi-mrsignal-diag", "--set-mode", "1"])
    rc = mrs_diag.main()
    out = capsys.readouterr().out

    assert rc == 2
    assert "--set-mode and --set-value must be provided together" in out


def test_autodetect_diag_prints_result(monkeypatch, capsys):
    import roi.tools.autodetect_diag as ad_diag
    from roi.core.device_discovery import DiscoveryResult

    fake_res = DiscoveryResult(
        multimeter_path="/dev/serial/by-id/mmeter",
        multimeter_idn="BK,5491B",
        can_channel="/dev/serial/by-id/canview",
    )
    monkeypatch.setattr(ad_diag, "autodetect_and_patch_config", lambda log_fn=None: fake_res)
    monkeypatch.setattr(sys, "argv", ["roi-autodetect-diag", "--quiet"])

    rc = ad_diag.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "Discovery result:" in out
    assert "/dev/serial/by-id/mmeter" in out


def test_env_hardcode_dry_run(monkeypatch, capsys, tmp_path):
    import roi.tools.env_hardcode as env_tool

    fake = env_tool.DetectedDevices(
        can_channel="/dev/serial/by-id/canview",
        multimeter_path="/dev/serial/by-id/mmeter",
        multimeter_idn="5491B Multimeter",
        mrsignal_port="/dev/serial/by-id/mrs",
        mrsignal_id=2,
        k1_serial_port="/dev/serial/by-id/k1",
        afg_visa_id="ASRL/dev/serial/by-id/afg::INSTR",
        eload_visa_id="USB0::1::2::3::0::INSTR",
        eload_idn="eload",
    )
    monkeypatch.setattr(env_tool, "detect_devices", lambda **_k: fake)
    monkeypatch.setattr(sys, "argv", ["roi-env-hardcode", "--output", str(tmp_path / "roi.env")])

    rc = env_tool.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "AUTO_DETECT_ENABLE=0" in out
    assert "CAN_CHANNEL=/dev/serial/by-id/canview" in out
    assert "MULTI_METER_FETCH_CMDS=:FETCh?,:FETC?" in out
    assert not (tmp_path / "roi.env").exists()


def test_env_hardcode_apply_writes_and_backs_up(monkeypatch, capsys, tmp_path):
    import roi.tools.env_hardcode as env_tool

    fake = env_tool.DetectedDevices(can_channel="/dev/serial/by-id/canview")
    monkeypatch.setattr(env_tool, "detect_devices", lambda **_k: fake)
    monkeypatch.setattr(env_tool, "_backup_stamp", lambda: "20260213-151700")

    out_path = tmp_path / "roi.env"
    out_path.write_text("OLD=1\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        ["roi-env-hardcode", "--apply", "--quiet", "--output", str(out_path)],
    )

    rc = env_tool.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert out_path.exists()
    assert "AUTO_DETECT_ENABLE=0" in out_path.read_text(encoding="utf-8")
    assert (tmp_path / "roi.env.20260213-151700.bak").exists()
    assert "Wrote" in out


def test_mmeter_diag_roi_cmds_success(monkeypatch, capsys):
    import roi.tools.mmeter_diag as mm_diag

    class FakeSerial:
        def __init__(self, *a, **k):
            pass

        def reset_input_buffer(self):
            return None

        def reset_output_buffer(self):
            return None

        def close(self):
            return None

    class FakeBK:
        def __init__(self, ser, log_fn=print):
            self.last_cmd = ""

        def query_line(self, cmd, delay_s=0.0, read_lines=6, clear_input=True):
            self.last_cmd = str(cmd)
            if str(cmd).strip() == "*IDN?":
                return "5491B  Multimeter,Ver1.4.14.06.18,124G21119"
            if str(cmd).strip().upper() == ":FUNCtion?".upper():
                return "CURR:DC"
            return "OK"

        def write(self, cmd, delay_s=0.0, clear_input=False):
            self.last_cmd = str(cmd)

        def fetch_values(self, cmd, delay_s=0.0, read_lines=6):
            return SimpleNamespace(primary=1.0, secondary=None, raw="1.0")

        def drain_errors(self, max_n=16, log=True):
            return ["0,No error"]

    monkeypatch.setattr(mm_diag.serial, "Serial", FakeSerial, raising=False)
    monkeypatch.setattr(mm_diag, "BK5491B", FakeBK)
    monkeypatch.setattr(
        sys,
        "argv",
        ["roi-mmter-diag", "--port", "/dev/ttyUSBX", "--roi-cmds", "--style", "func"],
    )

    rc = mm_diag.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "ROI Meter Command Matrix" in out
    assert "fail=0" in out


def test_mmeter_diag_roi_cmds_reports_failing_command(monkeypatch, capsys):
    import roi.tools.mmeter_diag as mm_diag

    class FakeSerial:
        def __init__(self, *a, **k):
            pass

        def reset_input_buffer(self):
            return None

        def reset_output_buffer(self):
            return None

        def close(self):
            return None

    class FakeBK:
        def __init__(self, ser, log_fn=print):
            self.last_cmd = ""

        def query_line(self, cmd, delay_s=0.0, read_lines=6, clear_input=True):
            self.last_cmd = str(cmd)
            if str(cmd).strip() == "*IDN?":
                return "5491B  Multimeter,Ver1.4.14.06.18,124G21119"
            if str(cmd).strip().upper() == ":FUNCtion?".upper():
                return "CURR:DC"
            return "OK"

        def write(self, cmd, delay_s=0.0, clear_input=False):
            self.last_cmd = str(cmd)

        def fetch_values(self, cmd, delay_s=0.0, read_lines=6):
            return SimpleNamespace(primary=1.0, secondary=None, raw="1.0")

        def drain_errors(self, max_n=16, log=True):
            if self.last_cmd.strip().upper() == ":TRIGGER:SOURCE BUS":
                return ["-113,BUS: BAD COMMAND"]
            return ["0,No error"]

    monkeypatch.setattr(mm_diag.serial, "Serial", FakeSerial, raising=False)
    monkeypatch.setattr(mm_diag, "BK5491B", FakeBK)
    monkeypatch.setattr(
        sys,
        "argv",
        ["roi-mmter-diag", "--port", "/dev/ttyUSBX", "--roi-cmds", "--style", "func"],
    )

    rc = mm_diag.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "Failing commands:" in out
    assert ":TRIGger:SOURce BUS" in out

