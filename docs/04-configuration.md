# Configuration

ROI configuration is defined in `roi.config` as sensible defaults, and can be overridden by environment variables.

On Raspberry Pi installs, the canonical override file is:

- `/etc/roi/roi.env`

The systemd unit loads that file automatically.

## Most important settings

### Build tag

Use this to label a build in logs:

```bash
ROI_BUILD_TAG=lab-pi-01
```

### CAN backend selection

SocketCAN (default):

```bash
CAN_INTERFACE=socketcan
CAN_CHANNEL=can0
CAN_BITRATE=250000
CAN_SETUP=1
```

CANview (serial gateway):

```bash
CAN_INTERFACE=rmcanview
CAN_CHANNEL=/dev/serial/by-id/<your-canview>
CAN_SERIAL_BAUD=115200
CAN_BITRATE=250000
CAN_SETUP=1
```

See [CAN backends](05-can-backends.md).

### Auto-detection

Auto-detection helps when `/dev/ttyUSB*` order changes.

Enable/disable:

```bash
AUTO_DETECT_ENABLE=1
AUTO_DETECT_VERBOSE=1
AUTO_DETECT_PREFER_BY_ID=1
```

Lock down probing (avoid talking to unknown serial ports):

```bash
AUTO_DETECT_BYID_ONLY=1
```

Provide “by-id” name hints (matched against `/dev/serial/by-id/*` symlink names):

```bash
AUTO_DETECT_MMETER_BYID_HINTS=5491b,multimeter
AUTO_DETECT_MRSIGNAL_BYID_HINTS=mr.signal,lanyi
AUTO_DETECT_K1_BYID_HINTS=arduino,relay
AUTO_DETECT_CANVIEW_BYID_HINTS=canview,proemion
```

### VISA backend

On Raspberry Pi, `pyvisa-py` is usually the easiest:

```bash
VISA_BACKEND=@py
VISA_TIMEOUT_MS=500
```

### Device IDs / ports

If you do not rely on auto-detect, set explicit paths:

```bash
MULTI_METER_PATH=/dev/ttyUSB0
MRSIGNAL_PORT=/dev/ttyUSB1
K1_SERIAL_PORT=/dev/ttyACM0
ELOAD_VISA_ID=USB0::...
AFG_VISA_ID=USB0::...   # or ASRL/...::INSTR
```

## Notes on defaults

- Defaults in `roi.config` are safe-ish, but **every lab setup differs**.
- It’s normal to configure at least `CAN_*` and any connected instruments.
