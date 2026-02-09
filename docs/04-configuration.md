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


### CAN TX / traffic tuning

ROI publishes **readback/status** frames on CAN from a dedicated TX thread.

Baseline settings:

```bash
CAN_TX_ENABLE=1
CAN_TX_PERIOD_MS=50
```

Fine-grained per-frame periods (milliseconds). These default to `CAN_TX_PERIOD_MS`, so you only need to set the ones you want slower:

```bash
CAN_TX_PERIOD_MMETER_LEGACY_MS=200
CAN_TX_PERIOD_MMETER_EXT_MS=200
CAN_TX_PERIOD_MMETER_STATUS_MS=200
CAN_TX_PERIOD_ELOAD_MS=200

CAN_TX_PERIOD_AFG_EXT_MS=1000
CAN_TX_PERIOD_MRS_STATUS_MS=1000
CAN_TX_PERIOD_MRS_INPUT_MS=1000
```

Optional: send a frame immediately when its payload changes (still rate-limited):

```bash
CAN_TX_SEND_ON_CHANGE=1
CAN_TX_SEND_ON_CHANGE_MIN_MS=50
```

### CAN RX filtering (optional CPU optimization)

On very busy CAN buses, you can ask ROI to apply driver/kernel-level CAN ID filters so the Python process only receives frames it actually cares about.

```bash
CAN_RX_KERNEL_FILTER_MODE=control
# or:
CAN_RX_KERNEL_FILTER_MODE=control+pat
```

Note: filtering reduces CPU load, but it also makes the bus-load estimator less accurate (because ROI no longer sees all traffic).

### CANview serial tuning (rmcanview)

For RM/Proemion CANview gateways, you can disable `pyserial.flush()` on every send. This can improve throughput and reduce CPU usage.

```bash
CAN_RMCANVIEW_FLUSH_EVERY_SEND=0
```

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
