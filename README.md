# ROI (Remote Operational Equipment)

ROI is a Raspberry Pi–focused bridge between a CAN bus and lab / test instruments (multimeter, electronic load, AFG, relay, MrSignal PSU).

It listens for **control frames** on CAN, applies those commands to instruments, and publishes **readback/status frames** back on CAN.

## Documentation (start here)

- **Docs index:** [`docs/README.md`](docs/README.md)

The docs are written in setup order (overview → install → config → run).

## Quick start (developer)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
roi
```

Run unit tests:

```bash
python -m pytest
```

## Raspberry Pi install (appliance style)

```bash
git clone <your-repo-url> roi
cd roi
sudo ./scripts/pi_install.sh --easy
sudo /opt/roi/.venv/bin/roi
```

Install as a service:

```bash
sudo /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
sudo journalctl -u roi -f
```

## Handy diagnostics

- VISA/USBTMC discovery:
  ```bash
  roi-visa-diag
  ```
- Multimeter serial diagnostics:
  ```bash
  roi-mmter-diag
  ```

## Optional web dashboard

ROI can run a lightweight read-only web UI for device status and failure diagnostics:

```bash
ROI_WEB_ENABLE=1 ROI_WEB_PORT=8080 roi
```

Then browse to `http://<pi-hostname>:8080/`.

## Project name note

Some earlier internal drafts referenced a different name. This repository is **ROI** (Remote Operational Equipment). Any legacy references have been removed.
