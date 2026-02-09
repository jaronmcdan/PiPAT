from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def _wait_for_port(srv, timeout_s: float = 2.0) -> int:
    """Wait until the underlying HTTPServer is bound and return the port."""

    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        httpd = getattr(srv, "_server", None)
        if httpd is not None:
            try:
                return int(httpd.server_address[1])
            except Exception:
                pass
        time.sleep(0.01)
    raise AssertionError("web server did not start")


def test_diagnostics_ring_and_health_dedupe():
    from roi.core.diagnostics import Diagnostics

    d = Diagnostics(max_events=10, dedupe_window_s=10.0)

    # Dedupe same message+source within the window.
    d.log("hello", source="x")
    d.log("hello", source="x")
    ev = d.events_snapshot()
    assert len(ev) == 1

    d.mark_ok("mmeter")
    d.mark_error("mmeter", RuntimeError("boom"), where="poll")
    snap = d.snapshot()
    assert "events" in snap and "health" in snap
    assert "mmeter" in snap["health"]
    assert snap["health"]["mmeter"]["error_count"] == 1
    assert "boom" in snap["health"]["mmeter"]["last_error"]
    # Age fields are best-effort (can be absent if monotonic math failed)
    assert "last_error_unix" in snap["health"]["mmeter"]


def test_web_dashboard_basic_routes():
    from roi.web import WebDashboardServer, WebServerConfig

    logs: list[str] = []

    def snap():
        return {"ok": True}

    srv = WebDashboardServer(cfg=WebServerConfig(host="127.0.0.1", port=0, token=""), get_snapshot=snap, log_fn=logs.append)
    srv.start()
    port = _wait_for_port(srv)

    # Index
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
        body = r.read().decode("utf-8", errors="replace")
        assert "ROI Dashboard" in body

    # Ping
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping") as r:
        assert r.read() == b"pong"

    # Status
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status") as r:
        data = json.loads(r.read().decode("utf-8"))
        assert data["ok"] is True

    # 404
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404

    srv.stop()

    # Start log should have been emitted.
    assert any("[web]" in s for s in logs)


def test_web_dashboard_token_and_error_path():
    from roi.web import WebDashboardServer, WebServerConfig

    def boom():
        raise RuntimeError("snap")

    srv = WebDashboardServer(cfg=WebServerConfig(host="127.0.0.1", port=0, token="sekrit"), get_snapshot=boom)
    srv.start()
    port = _wait_for_port(srv)

    # Unauthorized without token
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping")
        raise AssertionError("expected 401")
    except urllib.error.HTTPError as e:
        assert e.code == 401

    # Authorized with query token
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status?token=sekrit")
        raise AssertionError("expected 500")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        data = json.loads(e.read().decode("utf-8"))
        assert "error" in data

    # Authorized with bearer token
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping")
    req.add_header("Authorization", "Bearer sekrit")
    with urllib.request.urlopen(req) as r:
        assert r.read() == b"pong"

    srv.stop()
