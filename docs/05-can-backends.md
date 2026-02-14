# CAN Backends

ROI supports two CAN backends:

1. SocketCAN (`CAN_INTERFACE=socketcan`)
2. RM/Proemion CANview (`CAN_INTERFACE=rmcanview`)

## SocketCAN

If `CAN_SETUP=1`, ROI tries to run:

```bash
ip link set can0 up type can bitrate 250000
```

If CAN is managed externally:

```bash
CAN_SETUP=0
```

Useful debug commands (`can-utils`):

```bash
ip -details link show can0
candump can0
cansend can0 123#DEADBEEF
```

## CANview (rmcanview)

```bash
CAN_INTERFACE=rmcanview
CAN_CHANNEL=/dev/serial/by-id/<your-canview>
CAN_SERIAL_BAUD=115200
CAN_BITRATE=250000
CAN_SETUP=1
CAN_CLEAR_ERRORS_ON_INIT=1
```

Notes:

- `CAN_SERIAL_BAUD` is the USB-serial link speed to the gateway.
- `CAN_BITRATE` is the actual CAN bus bitrate.
- `CAN_CLEAR_ERRORS_ON_INIT=1` resets gateway CAN controller state on startup.

## Frame IDs

Control/readback CAN IDs are defined in `src/roi/config.py` (`*_CTRL_ID`,
`*_READ_ID`).
