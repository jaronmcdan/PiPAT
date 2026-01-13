# ROI Instrument Bridge (Raspberry Pi)

This project runs on a Raspberry Pi and bridges **SocketCAN** control messages to lab instruments and local GPIO:

- **E-load** via PyVISA (SCPI)
- **Multimeter** (e.g., Keysight 5491B) via USB-serial
- **AFG** via PyVISA (SCPI)
- **K1 Relay** (GPIO) for DUT power control
- Optional terminal dashboard (Rich TUI)

> Key files: `main.py`, `hardware.py`, `dashboard.py`, `config.py`

---

## Quick start (dev / interactive)

### 1) OS packages (Raspberry Pi OS / Debian)

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip python3-dev \
  can-utils \
  libusb-1.0-0
```

### 2) Create a virtualenv and install Python deps

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 3) Configure

You can configure in **either** of these ways:

1) **Edit `config.py`** (good for dev)
2) **Environment variables** (recommended for systemd / per-Pi overrides)
   - see `roi.env.example`

The most common settings:

- `CAN_CHANNEL`, `CAN_BITRATE`, `CAN_SETUP`
- `CAN_TX_ENABLE`, `CAN_TX_PERIOD_MS` (regulate outgoing readback frames; default 50ms)
- `K1_PIN_BCM`, `K1_ACTIVE_LOW`, `K1_CAN_INVERT`, `K1_IDLE_DRIVE`
- `K1_TIMEOUT_SEC` (watchdog timeout for K1)
- `CONTROL_TIMEOUT_SEC` (or per-device timeouts)
- `MULTI_METER_PATH`, `MULTI_METER_BAUD`
- `ELOAD_VISA_ID`, `AFG_VISA_ID`

### 4) Run

```bash
python3 main.py
```

---

## SocketCAN notes

By default (`CAN_SETUP=1`), `main.py` attempts to bring the CAN interface up using:

```bash
ip link set <channel> up type can bitrate <bitrate>
```

If you prefer to configure CAN at boot, set `CAN_SETUP=0` (in `config.py` or `/etc/roi/roi.env`).

Manual test:

```bash
sudo ip link set can1 up type can bitrate 250000
ip -details link show can1
```

---

## K1 relay semantics (no inference)

PiPAT treats the relay as a **direct K1 drive output** controlled by CAN bit0 in `RLY_CTRL_ID`.
The UI reports only what the software is commanding and what the GPIO pin is doing.

- There is **no NC/NO “DUT power” inference**.
- If you need actual DUT power status, measure it (e.g., voltage sense, current sense, an auxiliary input).

Relevant settings:
- `K1_ACTIVE_LOW` — electrical polarity of the relay input (module dependent)
- `K1_CAN_INVERT` — invert CAN bit0 before driving K1 (optional)
- `K1_IDLE_DRIVE` — drive state to apply on watchdog timeout and (optionally) on startup
- `K1_TIMEOUT_SEC` — watchdog timeout for K1 controls


## Control watchdog / timeouts (auto-idle)

If a control message stops arriving, this app will drive that device back to an **idle state**.

Defaults:
- Timeout: `CONTROL_TIMEOUT_SEC=2.0`
- E-load idle: input OFF
- AFG idle: output OFF

You can override per device:
- `K1_TIMEOUT_SEC`
- `ELOAD_TIMEOUT_SEC`
- `AFG_TIMEOUT_SEC`
- `MMETER_TIMEOUT_SEC`

The dashboard’s bottom bar shows watchdog ages / timeouts when the TUI is enabled.

---

## Running as a service (always-on)

This bundle includes:
- `scripts/pi_install.sh` – installs into `/opt/roi`, creates a venv, installs deps, and optionally enables a systemd service
- `systemd/roi.service` – systemd unit
- `roi.env.example` – environment override template

### Install on the Pi

1) Copy a release tarball to the Pi and extract it (see dist build below)
2) Run:

```bash
cd <extracted-folder>
sudo ./scripts/pi_install.sh --install-os-deps --enable-service
```

### Check logs

```bash
sudo systemctl status roi
sudo journalctl -u roi -f
```

Notes:
- The service defaults to **headless mode** (`ROI_HEADLESS=1`) which disables the Rich TUI in journald.
- Edit `/etc/roi/roi.env` for per-Pi overrides.

---

## Building a Raspberry Pi release tarball

Run on your dev machine (where the repo is):

```bash
./scripts/make_pi_dist.sh
```

Output: `dist/roi-<version>.tar.gz`

---

## Troubleshooting

### Multimeter `UnicodeDecodeError`

If the serial port returns non-ASCII garbage (e.g. noise at boot), this code now decodes with `errors='replace'` and flushes buffers before `*IDN?`.
If you still see issues, confirm:
- Correct `MULTI_METER_PATH` (`/dev/ttyUSB0`, `/dev/ttyUSB1`, etc.)
- Correct baud (`MULTI_METER_BAUD`)

### Relay does “the opposite”

Double-check:
- `K1_ACTIVE_LOW`

---

## Safety

This code can control power to external equipment.
Verify relay wiring (NC/NO), polarity, and idle defaults before running unattended.
