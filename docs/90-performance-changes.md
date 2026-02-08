# Performance and responsiveness notes

This appendix summarizes a few intentional design choices in ROI that affect responsiveness on a busy CAN bus.

## CAN RX queue overflow policy

File: `src/roi/can/comm.py`

ROI uses a bounded `queue.Queue` between the CAN RX thread and the device command worker.

When `CAN_CMD_QUEUE_MAX` is full, ROI prefers to **drop the oldest queued command** to make room for the newest. This improves responsiveness for “knob/slider” controls where only the latest state matters.


## CAN TX traffic shaping

File: `src/roi/can/comm.py`, `src/roi/config.py`

ROI’s TX loop supports per-frame publish periods so you can reduce CAN traffic without turning off periodic transmissions.

Environment variables (milliseconds):

- `CAN_TX_PERIOD_MMETER_LEGACY_MS`
- `CAN_TX_PERIOD_MMETER_EXT_MS`
- `CAN_TX_PERIOD_MMETER_STATUS_MS`
- `CAN_TX_PERIOD_ELOAD_MS`
- `CAN_TX_PERIOD_AFG_EXT_MS`
- `CAN_TX_PERIOD_MRS_STATUS_MS`
- `CAN_TX_PERIOD_MRS_INPUT_MS`

Each defaults to `CAN_TX_PERIOD_MS` (legacy behavior).

Optional: `CAN_TX_SEND_ON_CHANGE=1` sends a frame immediately when its payload changes (still rate-limited).

## CAN RX kernel/driver filtering (optional)

File: `src/roi/can/comm.py`

When `CAN_RX_KERNEL_FILTER_MODE` is set, ROI attempts to apply driver/kernel-level CAN ID filters (`cbus.set_filters`) so only relevant frames are delivered to Python.

This can significantly reduce CPU usage on a busy bus, but it also makes the bus-load estimator less accurate (because ROI can’t count what it can’t see).

## rmcanview adapter RX buffering

File: `src/roi/can/rmcanview.py`

The rmcanview serial backend maintains its own internal RX buffer. ROI bounds that buffer with `CAN_RMCANVIEW_RX_MAX` (default 2048) and uses a “drop-oldest” policy under backpressure to prevent unbounded latency growth.

## Polling lock strategy

Files:
- `src/roi/app.py`
- `src/roi/core/device_comm.py`

ROI uses short lock hold times for instrument I/O:
- measurement polling (fast) is separated from status polling (slow)
- control writes are prioritized over long polling sequences

This avoids the “UI is responsive but controls lag” failure mode when a slow VISA/serial query blocks a shared lock for too long.

## MrSignal “OFF” fast path

File: `src/roi/devices/mrsignal.py`

When disabling output, ROI writes `OUTPUT_ON=0` first as a fast safety action before applying other mode/value changes.

## Dashboard fallbacks

File: `src/roi/ui/dashboard.py`

When a polled field is missing (because polling is intentionally throttled to avoid blocking control), the dashboard can fall back to the last commanded state cached in the HardwareManager.

## Build tag

Use `ROI_BUILD_TAG` (env var) to label a deployment build in logs.
