# ROI Instrument Bridge (Raspberry Pi)

This project runs on a Raspberry Pi and bridges **SocketCAN** control messages to lab instruments and local GPIO:

- **E-load** via PyVISA (SCPI)
- **Multimeter** (e.g., Keysight 5491B) via USB-serial
- **AFG** via PyVISA
- **K1 Relay** (GPIO) for DUT power control
- A terminal dashboard (Rich TUI)

> Files: `main.py`, `hardware.py`, `dashboard.py`, `config.py`

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

Edit `config.py` (or use an env file if you adopt the optional service install below).

Key settings in `config.py`:

- `CAN_CHANNEL`, `CAN_BITRATE`
- `K1_PIN_BCM`
- `MULTI_METER_PATH`, `MULTI_METER_BAUD`
- `ELOAD_VISA_ID`, `AFG_VISA_ID`

### 4) Run

```bash
python main.py
```

---

## SocketCAN notes

`main.py` attempts to bring the CAN interface up using:

```bash
sudo ip link set <channel> up type can bitrate <bitrate>
```

That means:
- Running as **root** is easiest (especially as a service).
- Or you can pre-configure CAN at boot and run as a normal user.

To test manually:

```bash
sudo ip link set can1 up type can bitrate 250000
ip -details link show can1
```

---

## Relay hats, inversion, and NC/NO wiring

Relay behavior can look “inverted” for three independent reasons:

1) **Relay input polarity** (active-low vs active-high)
2) **Wiring** (NC vs NO contact)
3) **Protocol meaning** (whether a CAN bit means “power on”)

### What the current code does
- Uses `gpiozero.LED(... active_high=True, initial_value=False)` in `hardware.py`
- Interprets relay CAN command in `main.py` as:

```python
should_be_on = (message.data[0] & 0x01) == 0   # bit0==0 => ON
if should_be_on: hardware.relay.on()
else: hardware.relay.off()
```

So:
- CAN bit0 **0** drives `relay.on()`
- CAN bit0 **1** drives `relay.off()`

If your DUT powers up even when the Pi has no power, your relay path is very likely wired through **NC**:
- **coil de-energized** => NC closed => DUT powered
- **coil energized** => NC open => DUT unpowered

### Making it portable (recommended change)
`config.py` contains `RELAY_ACTIVE_LOW`, but it is not used in the current `hardware.py`. The simplest portable update is:

```python
# hardware.py
self.relay = LED(
    config.K1_PIN_BCM,
    active_high=not config.RELAY_ACTIVE_LOW,
    initial_value=False,
)
```

Then set `RELAY_ACTIVE_LOW=True` on active-low boards.

If you also need “DUT power semantics” (NC vs NO) to be portable, the best practice is to implement a helper like
`set_dut_power(True/False)` that translates desired DUT power into coil energized/de-energized based on wiring.

---

## Running as a service (always-on)

This bundle includes:
- `scripts/pi_install.sh` – installs into `/opt/roi`, sets up a venv, and (optionally) a systemd service
- `systemd/roi.service` – service template
- `roi.env.example` – environment file template

### Install on the Pi

1) Copy a release tarball to the Pi and extract it (see the dist script below)
2) Run:

```bash
cd <extracted-folder>
sudo ./scripts/pi_install.sh --enable-service
```

### Check logs

```bash
sudo systemctl status roi
sudo journalctl -u roi -f
```

---

## Building a Raspberry Pi release tarball

Run on your dev machine (where the repo is):

```bash
./scripts/make_pi_dist.sh
```

Output: `dist/roi-<version>.tar.gz`

---

## License / Safety

This code can control power to external equipment. Verify relay wiring (NC/NO), polarity, and safe defaults before
running unattended.
