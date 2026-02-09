# Running ROI

## Interactive mode (Rich dashboard)

If `rich` is installed and `ROI_HEADLESS=0`, running `roi` opens a live dashboard.

Developer install:
```bash
roi
```

Pi install:
```bash
sudo /opt/roi/.venv/bin/roi
```

## Headless mode

Headless mode is recommended under systemd:

```bash
ROI_HEADLESS=1 roi
```

In headless mode, ROI logs status periodically and publishes CAN frames normally, but skips the Rich UI.

## Web dashboard (read-only)

ROI can optionally start a tiny, dependency-free web dashboard to view:

- Device detect state (AFG / E-load / Multimeter / MrSignal / K1 / CAN)
- Live measurements (from the same telemetry used by the Rich dashboard)
- Recent events + per-device last-error diagnostics

Enable it with either CLI flags:

```bash
roi --web --web-port 8080
```

…or environment variables (recommended for systemd):

```bash
ROI_WEB_ENABLE=1 ROI_WEB_HOST=0.0.0.0 ROI_WEB_PORT=8080 roi
```

Optional access control (very basic):

```bash
ROI_WEB_ENABLE=1 ROI_WEB_TOKEN='your-secret' roi
```

Then browse:

- http://<pi-hostname>:8080/

If a token is set, supply either:

- `Authorization: Bearer your-secret` (header)
- `?token=your-secret` (query param)

## systemd service

On the Pi install:

```bash
sudo /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
```

Logs:

```bash
sudo journalctl -u roi -f
```
