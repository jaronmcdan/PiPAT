# Troubleshooting

## ROI starts but no controls take effect

- Verify CAN traffic is arriving:
  - SocketCAN: `candump can0`
  - CANview: verify `CAN_CHANNEL` points to the expected `/dev/serial/by-id/...`
- Verify CAN interface state:

```bash
ip -details link show can0
```

ROI-aware CAN diagnostic:

```bash
roi-can-diag --duration 5
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
roi-mmter-diag --roi-cmds --style func
```

Note: `--roi-cmds` is a superset probe. It can report unsupported commands that
your bench never uses in normal PAT flows.

Try:

- use `/dev/serial/by-id/...` instead of `/dev/ttyUSB*`
- set `AUTO_DETECT_BYID_ONLY=1`
- ensure no competing service opens the same serial device
- if startup errors persist, disable startup error draining to isolate:
  `MMETER_CLEAR_ERRORS_ON_STARTUP=0`
- if your firmware rejects some commands but ROI control still works, gate
  unsupported command classes in `/etc/roi/roi.env`:
  - `MMETER_LEGACY_MODE0_ENABLE=0` (skip legacy mode0->VDC mapping)
  - `MMETER_EXT_SET_RANGE_ENABLE=0` (skip EXT `:RANGe <value>` writes)
  - `MMETER_EXT_SECONDARY_ENABLE=0` (skip EXT `:FUNCtion2...` writes)
  - `MMETER_EXT_CTRL_ENABLE=0` (disable all EXT opcodes)

## MrSignal read/write diagnostics

```bash
roi-mrsignal-diag --read-count 3
```

Optional explicit write test:

```bash
roi-mrsignal-diag --enable 1 --set-mode 1 --set-value 5.0
```

## CANview errors or missing frames

- confirm the correct `/dev/serial/by-id/...` device
- confirm `CAN_SERIAL_BAUD`
- set `CAN_CLEAR_ERRORS_ON_INIT=1`

## Auto-detect behavior looks wrong

```bash
roi-autodetect-diag
```

## Module import errors from a checkout

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
roi
```
