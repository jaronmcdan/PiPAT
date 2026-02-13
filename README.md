# ROI (Remote Operational Equipment)

ROI is a Raspberry Pi focused bridge between a CAN bus and lab/test instruments.
It receives CAN control frames, applies them to connected devices, and publishes
readback/status frames back onto CAN.

## Start Here

- Documentation index: [`docs/README.md`](docs/README.md)
- Recommended path: overview -> install -> config -> run

## Developer Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
roi
```

Run tests:

```bash
python -m pytest
```

## Raspberry Pi Install

```bash
git clone <your-repo-url> roi
cd roi
sudo bash scripts/pi_install.sh --easy
sudo /opt/roi/.venv/bin/roi
```

Install and enable service:

```bash
sudo bash /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
sudo journalctl -u roi -f
```

## Diagnostics

```bash
roi-visa-diag
roi-mmter-diag
```

## Optional Web Dashboard

```bash
ROI_WEB_ENABLE=1 ROI_WEB_PORT=8080 roi
```

Browse to `http://<pi-hostname>:8080/`.
