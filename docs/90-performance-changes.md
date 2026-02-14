# Performance and Responsiveness Notes

This appendix summarizes intentional behavior that affects responsiveness and
CAN traffic on busy systems.

## CAN RX queue overflow policy

File: `src/roi/can/comm.py`

ROI uses a bounded queue between CAN RX and device command handling. If the
queue is full (`CAN_CMD_QUEUE_MAX`), ROI drops the oldest queued command to
prefer the newest control state.

## CAN TX traffic shaping

Files: `src/roi/can/comm.py`, `src/roi/config.py`

Per-frame TX period variables allow lower CAN traffic without disabling periodic
readback frames:

- `CAN_TX_PERIOD_MMETER_LEGACY_MS`
- `CAN_TX_PERIOD_MMETER_EXT_MS`
- `CAN_TX_PERIOD_MMETER_STATUS_MS`
- `CAN_TX_PERIOD_ELOAD_MS`
- `CAN_TX_PERIOD_AFG_EXT_MS`
- `CAN_TX_PERIOD_MRS_STATUS_MS`
- `CAN_TX_PERIOD_MRS_INPUT_MS`

Each defaults to `CAN_TX_PERIOD_MS`.

Optional: `CAN_TX_SEND_ON_CHANGE=1` sends immediately on payload change (still
rate-limited).

## CAN RX kernel/driver filtering

File: `src/roi/can/comm.py`

`CAN_RX_KERNEL_FILTER_MODE` can reduce CPU usage by limiting delivered CAN IDs.
Tradeoff: bus-load estimation becomes less accurate when ROI sees only filtered
traffic.

## rmcanview RX buffering

File: `src/roi/can/rmcanview.py`

The rmcanview backend uses a bounded internal RX buffer controlled by
`CAN_RMCANVIEW_RX_MAX` and applies drop-oldest behavior under backpressure.

## Polling lock strategy

Files:

- `src/roi/app.py`
- `src/roi/core/device_comm.py`

ROI separates fast measurement polling from slower status polling and keeps lock
hold time short so control writes remain responsive.

## MrSignal output-off fast path

File: `src/roi/devices/mrsignal.py`

When disabling output, ROI writes `OUTPUT_ON=0` first as a safety-first step.

## Dashboard fallback behavior

File: `src/roi/ui/dashboard.py`

If a polled field is temporarily unavailable, the dashboard may display the last
known commanded state.

## Build tag

Use `ROI_BUILD_TAG` to label deployments in logs.
