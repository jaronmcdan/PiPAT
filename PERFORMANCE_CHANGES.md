# Performance / Responsiveness changes (2026-02-05)

This build focuses on:
1) **CAN & Device control response** (highest priority)
2) **UI lag**
3) **CPU load** (not a focus)

## Key changes

### CAN RX queue overflow policy
- **`can_comm.py`**: When the bounded `CAN_CMD_QUEUE_MAX` command queue fills, the CAN RX thread now **drops the oldest queued command** to make room for the newest.
  - This improves responsiveness for "knob/slider" style controls where only the latest state matters.

### rmcanview adapter RX buffering
- **`rmcanview.py`**: Internal adapter RX queue is now **bounded** (`CAN_RMCANVIEW_RX_MAX`, default 2048) and uses a **drop-oldest** policy under backpressure.
  - Prevents unbounded latency growth on busy CAN buses.

### E-load control batching + fewer writes
- **`device_comm.py`**: E-load writes are now **batched under a single lock** per frame.
- Only the setpoint relevant to the active mode is written:
  - **CURR mode** -> write `CURR` only
  - **RES mode**  -> write `RES` only
  - On mode change, the new mode's setpoint is written once.

### Polling lock hold time reductions
- **`main.py`**:
  - E-load measurement polling now acquires/releases the lock per query.
  - E-load status polling queries only the active setpoint and clears the inactive setpoint so the dashboard doesn't show stale values.
  - AFG status polling now acquires/releases the lock per query.
  - MrSignal status polling now reads status in **small chunks** (one Modbus transaction per lock acquisition) so control writes are only blocked for a single transaction at a time.

### MrSignal "OFF" fast path + float byteorder reuse
- **`mrsignal.py`**:
  - When disabling output, the driver writes `OUTPUT_ON=0` **first** (fast safety action), then applies mode/value.
  - When auto-detecting float byteorder, the driver tries the **previously successful byteorder first** to reduce repeated probing overhead during polling.

### Dashboard "optimistic" fallbacks
- **`dashboard.py`**:
  - When polled status fields are missing (because polling skipped to avoid blocking control), the dashboard falls back to the **last commanded state** cached in `HardwareManager`.

## New/updated config
- `CAN_RMCANVIEW_RX_MAX` (default 2048): bounds rmcanview adapter receive buffering.
- `BUILD_TAG` updated to: `2026-02-05-perf-ctrl-ui-v1`
