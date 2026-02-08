# Prerequisites

## Supported platforms

- **Raspberry Pi / Debian-based Linux** is the primary target.
- Windows/macOS can run unit tests and some logic, but hardware backends are Linux-oriented.

## Python

- Python **3.10+** is required (ROI uses modern typing syntax).

On Raspberry Pi OS / Debian, check:

```bash
python3 --version
```

## Hardware

ROI is meant to sit between your CAN network and lab instruments.

Typical setup:

- Raspberry Pi 4/5 (or similar)
- CAN interface:
  - SocketCAN capable interface (e.g., PiCAN, MCP2515, USB-CAN), **or**
  - RM/Proemion CANview gateway (serial “Byte Command Protocol”)
- Instruments (optional; ROI works with any subset):
  - B&K Precision bench multimeter (USB-serial)
  - Electronic load (VISA USBTMC, e.g., BK 8600 series)
  - AFG / function generator (VISA USB or VISA serial)
  - Arduino/USB relay controller for K1
  - MrSignal / LANYI MR2.x Modbus PSU

## OS packages (recommended)

The installer can install these for you:

- `python3-venv`, `python3-pip`, `python3-dev`
- `can-utils` (useful for debugging SocketCAN)
- `libusb-1.0-0`, `usbutils` (helpful for VISA/USBTMC)
- `rsync` (install scripts use it)
