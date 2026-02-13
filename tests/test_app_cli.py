from __future__ import annotations

import pytest


def test_parse_args_version_prints_version(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    import roi.app as app
    import roi.build_info as build_info

    monkeypatch.setattr(build_info, "get_version_with_revision", lambda: "9.9.9+gabcdef0")

    with pytest.raises(SystemExit) as ex:
        app.parse_args(["--version"])

    assert ex.value.code == 0
    out = capsys.readouterr().out
    assert "ROI 9.9.9+gabcdef0" in out

