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

Recommended for systemd/journald:

```bash
ROI_HEADLESS=1 roi
```

Headless mode still processes control traffic and publishes CAN readback frames.

## Web dashboard (read-only)

Enable via CLI:

```bash
roi --web --web-host 0.0.0.0 --web-port 8080
```

Or via environment variables (recommended for systemd):

```bash
ROI_WEB_ENABLE=1 ROI_WEB_HOST=0.0.0.0 ROI_WEB_PORT=8080 roi
```

Optional token:

```bash
ROI_WEB_ENABLE=1 ROI_WEB_TOKEN='your-secret' roi
```

If a token is set, clients must provide either:

- `Authorization: Bearer your-secret`
- `?token=your-secret`

Browse to:

- `http://<pi-hostname>:8080/`

## Useful CLI flags

- `--headless`
- `--no-can-setup`
- `--no-auto-detect`
- `--web`
- `--web-host`
- `--web-port`
- `--web-token`

## systemd service

```bash
sudo bash /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
sudo journalctl -u roi -f
```
