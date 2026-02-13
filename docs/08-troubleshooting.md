# Troubleshooting

## ROI starts but no controls take effect

- Verify CAN traffic is arriving:
  - SocketCAN: `candump can0`
  - CANview: verify `CAN_CHANNEL` points to the expected `/dev/serial/by-id/...`
- Verify CAN interface state:

```bash
ip -details link show can0
```

## Permission errors

- USB serial: user must be in `dialout` (or run as root)
- USBTMC/VISA: install udev rules or run as root
- SocketCAN bring-up requires privileges

## VISA cannot find instruments

Run:

```bash
roi-visa-diag
```

Common causes:

- backend mismatch (`VISA_BACKEND=@py` is typical on Pi)
- missing USB permissions
- unsupported resource path

## Multimeter BUS errors / beeps

Run:

```bash
roi-mmter-diag
```

Try:

- use `/dev/serial/by-id/...` instead of `/dev/ttyUSB*`
- set `AUTO_DETECT_BYID_ONLY=1`
- ensure no competing service opens the same serial device

## CANview errors or missing frames

- confirm the correct `/dev/serial/by-id/...` device
- confirm `CAN_SERIAL_BAUD`
- set `CAN_CLEAR_ERRORS_ON_INIT=1`

## Module import errors from a checkout

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
roi
```
