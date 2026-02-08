# Troubleshooting

## ROI starts but nothing happens

- Confirm you are receiving CAN frames:
  - SocketCAN: `candump can0`
  - CANview: confirm `CAN_CHANNEL` points to the correct `/dev/serial/by-id/...` symlink

- Confirm the CAN interface is up:
  ```bash
  ip -details link show can0
  ```

## Permission errors

- USB serial devices: add your user to `dialout`, or run ROI as root.
- USBTMC / VISA: install udev rules or run as root.
- SocketCAN bring-up: requires privileges.

## VISA can’t see your instrument

Run:
```bash
roi-visa-diag
```

Common causes:
- Wrong backend (try `VISA_BACKEND=@py`)
- Missing permissions
- Device enumerated as `/dev/usbtmc*` but VISA backend doesn’t support it (ROI includes a `/dev/usbtmc*` fallback path)

## Multimeter shows BUS errors / beeps

Run:
```bash
roi-mmter-diag
```

Try:
- Use `/dev/serial/by-id/...` paths rather than `/dev/ttyUSB*`
- Set `AUTO_DETECT_BYID_ONLY=1` to avoid probing unknown serial devices
- Ensure no other service is opening the port (ModemManager, brltty, etc.)

## CANview errors / no frames

- Confirm the USB serial device is correct (`/dev/serial/by-id/...`).
- Confirm `CAN_SERIAL_BAUD` matches what the CANview expects.
- Try setting `CAN_CLEAR_ERRORS_ON_INIT=1`.

## “Module not found” errors

If running from a git checkout, install in a venv:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
roi
```
