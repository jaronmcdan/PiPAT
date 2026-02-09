"""A tiny read-only web dashboard.

Goals
-----
- **Zero extra dependencies** (stdlib only).
- **Read-only**: this is intended for *observability* (status + diagnostics),
  not remote control.
- Safe to run alongside the existing Rich TUI and/or headless mode.

This module intentionally avoids touching hardware (no instrument I/O). All
data is obtained from existing in-process state snapshots.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class WebServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    # Optional token. If set, clients must provide it as either:
    #   - Authorization: Bearer <token>
    #   - ?token=<token>
    token: str = ""


_INDEX_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\"/>
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
  <title>ROI Dashboard</title>
  <style>
    :root { --fg:#111; --muted:#666; --bg:#fafafa; --card:#fff; --border:#ddd; --ok:#0a7; --warn:#d70; --bad:#c22; }
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; background:var(--bg); color:var(--fg); margin:0; }
    header { padding: 14px 18px; border-bottom: 1px solid var(--border); background:#fff; position: sticky; top: 0; z-index: 2; }
    header .title { font-weight: 700; }
    header .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
    main { padding: 16px 18px 40px; max-width: 1200px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 12px 10px; box-shadow: 0 1px 0 rgba(0,0,0,.03); }
    .card h2 { font-size: 14px; margin: 0 0 8px; display:flex; align-items:center; gap:8px; }
    .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
    .pill.ok { color: #fff; background: var(--ok); border-color: transparent; }
    .pill.warn { color: #fff; background: var(--warn); border-color: transparent; }
    .pill.bad { color: #fff; background: var(--bad); border-color: transparent; }
    table { width: 100%; border-collapse: collapse; }
    td { padding: 4px 0; vertical-align: top; font-size: 13px; }
    td.k { color: var(--muted); width: 42%; }
    td.v { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
    .log { margin-top: 14px; }
    .log pre { background:#0b1020; color:#e7e7e7; padding: 10px; border-radius: 10px; overflow:auto; font-size: 12px; line-height: 1.35; }
    .row { display:flex; flex-wrap:wrap; gap: 10px; align-items:center; }
    .row .hint { color: var(--muted); font-size: 12px; }
    .btn { background:#fff; border:1px solid var(--border); border-radius: 8px; padding: 6px 10px; cursor:pointer; font-size: 12px; }
    .btn:hover { background:#f2f2f2; }
    .tiny { font-size: 11px; color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <div class=\"title\">ROI — Web Dashboard</div>
    <div class=\"meta\" id=\"meta\">Loading…</div>
  </header>

  <main>
    <div class=\"row\">
      <button class=\"btn\" id=\"pauseBtn\">Pause</button>
      <button class=\"btn\" id=\"copyBtn\">Copy JSON</button>
      <span class=\"hint\" id=\"hint\"></span>
    </div>
    <div style=\"height:10px\"></div>

    <div class=\"grid\" id=\"cards\"></div>

    <div class=\"log\">
      <h2 style=\"font-size:14px;margin:14px 0 8px\">Recent events</h2>
      <div class=\"tiny\">This is an in-memory ring buffer (restarts clear it). Repeated identical errors are throttled.</div>
      <pre id=\"events\">Loading…</pre>
    </div>

    <div class=\"log\">
      <h2 style=\"font-size:14px;margin:14px 0 8px\">Raw snapshot</h2>
      <pre id=\"raw\">Loading…</pre>
    </div>
  </main>

<script>
  let paused = false;
  let inFlight = false;
  const pauseBtn = document.getElementById('pauseBtn');
  const copyBtn = document.getElementById('copyBtn');
  const meta = document.getElementById('meta');
  const hint = document.getElementById('hint');
  const cards = document.getElementById('cards');
  const eventsPre = document.getElementById('events');
  const rawPre = document.getElementById('raw');

  // If the server is running with ROI_WEB_TOKEN, open the dashboard as:
  //   http://host:8080/?token=...   (or /index.html?token=...)
  // We automatically propagate that token to API requests.
  const urlParams = new URLSearchParams(window.location.search);
  const token = urlParams.get('token');
  const statusUrl = token ? ('/api/status?token=' + encodeURIComponent(token)) : '/api/status';

  pauseBtn.onclick = () => {
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
  };

  copyBtn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(rawPre.textContent || '');
      hint.textContent = 'Copied JSON to clipboard.';
      setTimeout(() => hint.textContent = '', 1200);
    } catch (e) {
      hint.textContent = 'Copy failed (browser permissions).';
      setTimeout(() => hint.textContent = '', 1500);
    }
  };

  function pill(text, cls) {
    const s = document.createElement('span');
    s.className = 'pill ' + cls;
    s.textContent = text;
    return s;
  }

  function addRow(tbl, k, v) {
    const tr = document.createElement('tr');
    const tdK = document.createElement('td');
    tdK.className = 'k';
    tdK.textContent = k;
    const tdV = document.createElement('td');
    tdV.className = 'v';
    tdV.textContent = (v === null || v === undefined) ? '--' : String(v);
    tr.appendChild(tdK);
    tr.appendChild(tdV);
    tbl.appendChild(tr);
  }

  function devicePill(present, health) {
    // health keys are optional
    const okAge = health?.last_ok_age_s;
    const errAge = health?.last_error_age_s;
    if (!present) return pill('NOT DETECTED', 'bad');
    if (okAge !== undefined && okAge !== null) {
      if (okAge < 2.5) return pill('OK', 'ok');
      if (okAge < 8.0) return pill('STALE', 'warn');
      return pill('STUCK', 'bad');
    }
    if (errAge !== undefined && errAge !== null) return pill('ERROR', 'bad');
    return pill('UNKNOWN', 'warn');
  }

  function render(data) {
    const build = data?.build_tag || 'unknown';
    const host = data?.host || '--';
    const up = data?.uptime_s;
    meta.textContent = `host=${host} | build=${build} | uptime=${(up??0).toFixed(1)}s | updated=${new Date().toLocaleTimeString()}`;

    // Cards
    cards.innerHTML = '';
    const devices = data?.devices || {};
    const health = data?.diagnostics?.health || {};
    const wd = data?.watchdog || {};
    const telem = data?.telemetry || {};

    function makeCard(title, key, rows) {
      const card = document.createElement('div');
      card.className = 'card';
      const h2 = document.createElement('h2');
      h2.textContent = title;
      const pres = !!devices?.[key]?.present;
      h2.appendChild(devicePill(pres, health?.[key]));
      card.appendChild(h2);
      const tbl = document.createElement('table');
      rows(tbl);
      card.appendChild(tbl);
      cards.appendChild(card);
    }

    makeCard('CAN', 'can', (tbl) => {
      addRow(tbl, 'interface', devices?.can?.interface);
      addRow(tbl, 'channel', devices?.can?.channel);
      addRow(tbl, 'bitrate', devices?.can?.bitrate);
      addRow(tbl, 'bus_load', devices?.can?.bus_load_pct !== null && devices?.can?.bus_load_pct !== undefined ? devices?.can?.bus_load_pct.toFixed(1)+'%' : '--');
      addRow(tbl, 'rx_fps', devices?.can?.rx_fps);
      addRow(tbl, 'tx_fps', devices?.can?.tx_fps);
      addRow(tbl, 'wd', (wd?.states?.can || '--') + (wd?.ages?.can != null ? ` (${wd.ages.can.toFixed(1)}s)` : ''));
    });

    makeCard('K1 Relay', 'k1', (tbl) => {
      addRow(tbl, 'backend', devices?.k1?.backend);
      addRow(tbl, 'drive', devices?.k1?.drive);
      addRow(tbl, 'level', devices?.k1?.pin_level);
      addRow(tbl, 'wd', (wd?.states?.k1 || '--') + (wd?.ages?.k1 != null ? ` (${wd.ages.k1.toFixed(1)}s)` : ''));
      if (health?.k1?.last_error) addRow(tbl, 'last_error', health.k1.last_error);
    });

    makeCard('E-Load', 'eload', (tbl) => {
      addRow(tbl, 'id', devices?.eload?.id);
      addRow(tbl, 'resource', devices?.eload?.resource);
      addRow(tbl, 'meas_v', telem?.load_volts_mV != null ? (telem.load_volts_mV/1000).toFixed(3)+' V' : '--');
      addRow(tbl, 'meas_i', telem?.load_current_mA != null ? (telem.load_current_mA/1000).toFixed(3)+' A' : '--');
      addRow(tbl, 'mode', telem?.load_stat_func);
      addRow(tbl, 'enable', telem?.load_stat_imp);
      addRow(tbl, 'set_curr', telem?.load_stat_curr);
      addRow(tbl, 'set_res', telem?.load_stat_res);
      addRow(tbl, 'short', telem?.load_stat_short);
      addRow(tbl, 'wd', (wd?.states?.eload || '--') + (wd?.ages?.eload != null ? ` (${wd.ages.eload.toFixed(1)}s)` : ''));
      const h = health?.eload;
      if (h?.last_error) addRow(tbl, 'last_error', h.last_error);
    });

    makeCard('AFG', 'afg', (tbl) => {
      addRow(tbl, 'id', devices?.afg?.id);
      addRow(tbl, 'output', telem?.afg_out_str);
      addRow(tbl, 'freq', telem?.afg_freq_str);
      addRow(tbl, 'ampl', telem?.afg_ampl_str);
      addRow(tbl, 'offset', telem?.afg_offset_str);
      addRow(tbl, 'duty', telem?.afg_duty_str);
      addRow(tbl, 'shape', telem?.afg_shape_str);
      addRow(tbl, 'wd', (wd?.states?.afg || '--') + (wd?.ages?.afg != null ? ` (${wd.ages.afg.toFixed(1)}s)` : ''));
      const h = health?.afg;
      if (h?.last_error) addRow(tbl, 'last_error', h.last_error);
    });

    makeCard('Multimeter', 'mmeter', (tbl) => {
      addRow(tbl, 'id', devices?.mmeter?.id);
      addRow(tbl, 'scpi_style', devices?.mmeter?.scpi_style);
      addRow(tbl, 'fetch_cmd', devices?.mmeter?.fetch_cmd);
      addRow(tbl, 'func', devices?.mmeter?.func);
      addRow(tbl, 'val', telem?.mmeter_primary_str || '--');
      addRow(tbl, 'val2', telem?.mmeter_secondary_str || '--');
      addRow(tbl, 'wd', (wd?.states?.mmeter || '--') + (wd?.ages?.mmeter != null ? ` (${wd.ages.mmeter.toFixed(1)}s)` : ''));
      const h = health?.mmeter;
      if (h?.last_error) addRow(tbl, 'last_error', h.last_error);
    });

    makeCard('MrSignal', 'mrsignal', (tbl) => {
      addRow(tbl, 'id', devices?.mrsignal?.id);
      addRow(tbl, 'port', devices?.mrsignal?.port);
      addRow(tbl, 'output', telem?.mrs_out_str);
      addRow(tbl, 'mode', telem?.mrs_mode_str);
      addRow(tbl, 'set', telem?.mrs_set_str);
      addRow(tbl, 'input', telem?.mrs_in_str);
      addRow(tbl, 'float_bo', telem?.mrs_bo_str);
      addRow(tbl, 'wd', (wd?.states?.mrsignal || '--') + (wd?.ages?.mrsignal != null ? ` (${wd.ages.mrsignal.toFixed(1)}s)` : ''));
      const h = health?.mrsignal;
      if (h?.last_error) addRow(tbl, 'last_error', h.last_error);
    });

    // PAT matrix
    makeCard('PAT Matrix', 'pat', (tbl) => {
      const pm = data?.pat_matrix || {};
      for (let j=0; j<6; j++) {
        const k = 'J'+j;
        const e = pm?.[k];
        const age = e?.age;
        const vals = e?.vals;
        let active = '--';
        if (Array.isArray(vals)) {
          const xs = [];
          for (let i=0; i<Math.min(12, vals.length); i++) {
            const v = (vals[i]||0) & 3;
            if (v) xs.push(String(i+1)+'('+v+')');
          }
          active = xs.length ? xs.join(' ') : '--';
        }
        addRow(tbl, k, (age!=null?age.toFixed(2)+'s ':'') + active);
      }
    });

    // Events
    const ev = data?.diagnostics?.events || [];
    const lines = ev.slice(-80).map(e => {
      const t = new Date((e.ts_unix||0)*1000).toLocaleTimeString();
      const lvl = (e.level||'info').toUpperCase().padEnd(5);
      const src = (e.source||'').padEnd(10);
      return `[${t}] ${lvl} ${src} ${e.message}`;
    });
    eventsPre.textContent = lines.join('\n') || '(no events yet)';

    rawPre.textContent = JSON.stringify(data, null, 2);
  }

  function showError(e) {
    const msg = String(e);
    meta.textContent = 'Disconnected (' + msg + ')';
    eventsPre.textContent = 'Disconnected: ' + msg;
    // Keep whatever was last rendered in rawPre if available.
    if (!rawPre.textContent || rawPre.textContent.trim() === 'Loading…' || rawPre.textContent.trim() === 'Loading...') {
      rawPre.textContent = 'Disconnected: ' + msg;
    }
  }

  async function fetchWithTimeout(url, timeoutMs) {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { signal: controller.signal });
    } finally {
      clearTimeout(t);
    }
  }

  async function pollOnce() {
    if (paused || inFlight) return;
    inFlight = true;
    try {
      const res = await fetchWithTimeout(statusUrl, 1200);
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.text()) || ''; } catch (_) { detail = ''; }
        detail = detail ? (' ' + detail.slice(0, 200)) : '';
        throw new Error('HTTP ' + res.status + detail);
      }
      const data = await res.json();
      render(data);
    } catch (e) {
      showError(e);
    } finally {
      inFlight = false;
    }
  }

  pollOnce();
  setInterval(pollOnce, 1000);
</script>
</body>
</html>
"""


class _ServerWithContext(ThreadingHTTPServer):
    """ThreadingHTTPServer with an attached context."""

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, *, context: "_Context"):
        super().__init__(server_address, RequestHandlerClass)
        self.context = context


class _Context:
    def __init__(
        self,
        *,
        cfg: WebServerConfig,
        get_snapshot: Callable[[], Dict[str, Any]],
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.get_snapshot = get_snapshot
        self.log = log_fn or (lambda _m: None)


class _Handler(BaseHTTPRequestHandler):
    server: _ServerWithContext  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Silence the default per-request logging. ROI already has its own logs.
        return

    def _send(self, status: int, *, content_type: str, body: bytes) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        body = b"Unauthorized"
        self.send_response(int(HTTPStatus.UNAUTHORIZED))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("WWW-Authenticate", 'Bearer realm="ROI"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self) -> bool:
        token = str(getattr(self.server.context.cfg, "token", "") or "")
        if not token:
            return True

        # 1) Authorization header
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth.split(" ", 1)[1].strip() == token:
            return True

        # 2) Query parameter
        try:
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            if params.get("token", [""])[0] == token:
                return True
        except Exception:
            pass
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._is_authorized():
            self._unauthorized()
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"

        if path in ("/", "/index.html"):
            self._send(
                int(HTTPStatus.OK),
                content_type="text/html; charset=utf-8",
                body=_INDEX_HTML.encode("utf-8"),
            )
            return

        if path == "/api/status":
            try:
                snap = self.server.context.get_snapshot()
                body = json.dumps(snap, sort_keys=False).encode("utf-8")
                self._send(int(HTTPStatus.OK), content_type="application/json", body=body)
            except Exception as e:
                self._send(
                    int(HTTPStatus.INTERNAL_SERVER_ERROR),
                    content_type="application/json",
                    body=json.dumps({"error": str(e)}).encode("utf-8"),
                )
            return

        if path == "/api/ping":
            self._send(int(HTTPStatus.OK), content_type="text/plain; charset=utf-8", body=b"pong")
            return

        self._send(
            int(HTTPStatus.NOT_FOUND),
            content_type="text/plain; charset=utf-8",
            body=b"Not found",
        )


class WebDashboardServer:
    """Background thread that serves a read-only HTML dashboard + JSON API."""

    def __init__(
        self,
        *,
        cfg: WebServerConfig,
        get_snapshot: Callable[[], Dict[str, Any]],
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._context = _Context(cfg=cfg, get_snapshot=get_snapshot, log_fn=log_fn)
        self._server: Optional[_ServerWithContext] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.is_running:
            return

        # Bind early so we fail fast with a useful error message.
        addr = (str(self.cfg.host), int(self.cfg.port))
        self._server = _ServerWithContext(addr, _Handler, context=self._context)

        def _run() -> None:
            assert self._server is not None
            host, port = self._server.server_address[:2]
            self._context.log(f"[web] dashboard: http://{host}:{port}")
            try:
                self._server.serve_forever(poll_interval=0.5)
            finally:
                try:
                    self._server.server_close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, name="roi-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        srv = self._server
        if srv is None:
            return
        try:
            srv.shutdown()
        except Exception:
            pass

    @staticmethod
    def default_host() -> str:
        # Prefer a stable hostname if possible, but fall back to 0.0.0.0.
        try:
            _ = socket.gethostname()
        except Exception:
            return "0.0.0.0"
        return "0.0.0.0"
