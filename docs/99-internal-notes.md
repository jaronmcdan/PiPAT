# Internal Notes (Maintainers)

Historical engineering notes for maintainers. Not end-user documentation.

Note: older notes referenced pre-refactor root-level paths. Current paths in this
file use the src-layout.

## 2026-01-28: Dashboard stability fixes

Changes:

- Bus-load smoothing in `src/roi/can/metrics.py` (`BusLoadMeter` EMA)
- App wiring in `src/roi/app.py` to `CAN_BUS_LOAD_SMOOTH_ALPHA`
- Watchdog timing hardening using `time.monotonic()`
- Soft/hard timeout behavior via `WATCHDOG_GRACE_SEC`
- Separate CAN liveness timeout via `CAN_TIMEOUT_SEC`

Files touched:

- `src/roi/app.py`
- `src/roi/ui/dashboard.py`
- `src/roi/can/metrics.py`
- `src/roi/config.py`

## 2026-01-30: MrSignal (MR2.0) Modbus RTU support

Changes:

- Added `src/roi/devices/mrsignal.py` (minimalmodbus client + byteorder helpers)
- Wired MrSignal into `HardwareManager` init/idle/shutdown paths
- Added `MRSIGNAL_*` config options, CAN IDs, and optional readback frames
- Added dashboard status fields (ID, output enable, mode, setpoint, input, byteorder)

## 2026-01-30: USB auto-detect mitigation for 5491B bus errors during VISA scanning

Root cause:

- PyVISA probing may query many `ASRL/...` resources with forced baud and `*IDN?`
- Probing a 5491B at incorrect serial settings can trigger meter bus/command errors

Mitigation:

- Exclude ASRL resources that map to already-known serial ports (meter/MrSignal)
- Exclude onboard console ports by default (`ttyAMA*`, `ttyS*`)
- Allow full ASRL probe disable

Relevant config:

- `AUTO_DETECT_VISA_PROBE_ASRL`
- `AUTO_DETECT_ASRL_BAUD`
- `AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES`
- `AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES`

Files touched:

- `src/roi/core/device_discovery.py`
- `src/roi/config.py`
