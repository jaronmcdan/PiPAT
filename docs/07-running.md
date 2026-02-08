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

## systemd service

On the Pi install:

```bash
sudo /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
```

Logs:

```bash
sudo journalctl -u roi -f
```
