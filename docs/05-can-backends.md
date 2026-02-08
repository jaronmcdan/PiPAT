# CAN backends

ROI supports two CAN backends:

1. **SocketCAN** (`CAN_INTERFACE=socketcan`) — recommended if you have a Linux CAN netdev (can0/can1).
2. **RM/Proemion CANview** (`CAN_INTERFACE=rmcanview`) — for CANview gateways connected via serial.

## SocketCAN

### Bring-up

If `CAN_SETUP=1`, ROI will attempt to run:

```bash
ip link set can0 up type can bitrate 250000
```

If you prefer to manage CAN outside ROI, set:

```bash
CAN_SETUP=0
```

### Debugging tools

Install `can-utils` and use:

```bash
ip -details link show can0
candump can0
cansend can0 123#DEADBEEF
```

## CANview (rmcanview)

Set:

```bash
CAN_INTERFACE=rmcanview
CAN_CHANNEL=/dev/serial/by-id/<your-canview>
CAN_SERIAL_BAUD=115200
CAN_BITRATE=250000
CAN_SETUP=1
```

Notes:

- `CAN_SERIAL_BAUD` is the **USB-serial baud** between Pi and CANview.
- `CAN_BITRATE` is the **actual CAN bus bitrate**.
- If `CAN_CLEAR_ERRORS_ON_INIT=1`, ROI will send a gateway reset sequence at startup.

## Frame formats and IDs

CAN control/readback IDs are defined in `roi.config` (see the `*_CTRL_ID` and `*_READ_ID` constants).
