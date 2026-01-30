# ROI Instrument Bridge (Raspberry Pi)

This project runs on a Raspberry Pi and bridges **SocketCAN** control messages to lab instruments and local GPIO:

- **E-load** via PyVISA (SCPI)
- **Multimeter** (e.g., Keysight 5491B) via USB-serial
- **AFG** via PyVISA (SCPI)
- **MrSignal / LANYI MR2.0** via Modbus RTU (USB-serial)
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

# GPIO backends for gpiozero (recommended on Pi OS Bookworm)
sudo apt-get install -y python3-lgpio

# Optional (older stacks / alternates)
# sudo apt-get install -y python3-rpi.gpio python3-pigpio

# Ensure your user can access GPIO without sudo
sudo usermod -aG gpio $USER
# Log out/in (or reboot) for group membership to take effect
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
- `DASH_FPS` (Rich TUI render rate; default 15)
- `MEAS_POLL_PERIOD`, `STATUS_POLL_PERIOD` (instrument poll cadence; defaults 0.2s/1.0s)
- `K1_PIN_BCM`, `K1_ACTIVE_LOW`, `K1_CAN_INVERT`, `K1_IDLE_DRIVE`
- `K1_TIMEOUT_SEC` (watchdog timeout for K1)
- `CONTROL_TIMEOUT_SEC` (or per-device timeouts)
- `MULTI_METER_PATH`, `MULTI_METER_BAUD`
- `ELOAD_VISA_ID`, `AFG_VISA_ID`
- `MRSIGNAL_ENABLE`, `MRSIGNAL_PORT`, `MRSIGNAL_BAUD`, `MRSIGNAL_SLAVE_ID`, `MRSIGNAL_PARITY`, `MRSIGNAL_STOPBITS`

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

If GPIO is unavailable (or `K1_ENABLE=0`), PiPAT falls back to a **no-op/mock relay driver** so the rest of the bridge can run.
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

### `gpiozero.exc.BadPinFactory: Unable to load any default pin factory!`

This means gpiozero cannot find a working GPIO backend on this host (common when:
running off-Pi, inside a container without GPIO access, missing `python3-lgpio`,
or lacking permissions for `/dev/gpiomem`/`/dev/gpiochip*`).

Options:

1) **Real GPIO on a Pi:** install `python3-lgpio` and ensure you are in the `gpio` group:

```bash
sudo apt-get install -y python3-lgpio
sudo usermod -aG gpio $USER
```

**Virtualenv note:** apt-installed GPIO backends (like `python3-lgpio`) live in system site-packages. If you are running inside a venv and still see this error, recreate it with system site packages enabled:

```bash
rm -rf .venv
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```


2) **Dev / no-GPIO mode:** disable K1 entirely:

```bash
export K1_ENABLE=0
python3 main.py
```

3) **Dev / simulated GPIO:** run with gpiozero's mock pin factory:

```bash
export GPIOZERO_PIN_FACTORY=mock
python3 main.py
```

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


## CAN bus load (dashboard)

PiPAT shows an estimated CAN **bus load %** on the dashboard status line.

Notes:

- This is an **estimator**, not a physical-layer measurement.
- It uses the configured `CAN_BITRATE` and observed frame sizes to approximate on-wire bits.
- TX frames sent by PiPAT are counted in software; RX frames are counted from SocketCAN.

Tuning (optional):

- `CAN_BUS_LOAD_ENABLE=0` to hide/disable load calculation
- `CAN_BUS_LOAD_WINDOW_SEC=1.0` sliding window (seconds)
- `CAN_BUS_LOAD_STUFFING_FACTOR=1.2` heuristic stuffing multiplier
- `CAN_BUS_LOAD_OVERHEAD_BITS=48` approximate overhead bits per classic CAN frame

---

## MrSignal (MR2.0) support

PiPAT can control a **MrSignal / LANYI MR2.0** over its USB virtual COM port using **Modbus RTU** (via `minimalmodbus`).

### Configure (env vars)

Add these to `roi.env` (see `roi.env.example`):

- `MRSIGNAL_ENABLE=1`
- `MRSIGNAL_PORT=/dev/ttyUSB1` (or `COM7` on Windows for dev)
- `MRSIGNAL_BAUD=9600`
- `MRSIGNAL_SLAVE_ID=1`
- `MRSIGNAL_PARITY=N`  (`N`/`E`/`O`)
- `MRSIGNAL_STOPBITS=1`
- `MRSIGNAL_TIMEOUT=0.5`
- Optional float byteorder overrides:
  - `MRSIGNAL_FLOAT_BYTEORDER=` (e.g. `BYTEORDER_BIG_SWAP`)
  - `MRSIGNAL_FLOAT_BYTEORDER_AUTO=1`
- Safety clamps:
  - `MRSIGNAL_MAX_V=24.0`
  - `MRSIGNAL_MAX_MA=24.0`

### CAN control frame

**Arbitration ID:** `MRSIGNAL_CTRL_ID` (default `0x0CFF0800`, extended)

Payload (8 bytes):

- `byte0`: bit0 = enable (1=ON, 0=OFF)
- `byte1`: output select (matches MR2.0 register 40022)
  - `0` = mA
  - `1` = V
  - `4` = mV
  - `6` = 24V
- `bytes2..5`: `float32` little-endian setpoint
  - units depend on mode: **mA** when mode=0, **V** when mode=1/6, **mV** when mode=4
- `bytes6..7`: reserved

PiPAT applies safety clamps (`MRSIGNAL_MAX_V`, `MRSIGNAL_MAX_MA`) before writing to the instrument.

### Optional CAN readback frames

If `CAN_TX_ENABLE=1` (default), PiPAT also publishes:

- `MRSIGNAL_READ_STATUS_ID` (default `0x0CFF0007`):
  - `byte0` enable (0/1)
  - `byte1` mode
  - `bytes2..5` float32 output value (same units as mode)
- `MRSIGNAL_READ_INPUT_ID` (default `0x0CFF0008`):
  - `bytes0..3` float32 input value (same units as mode)

---

