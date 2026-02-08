# Internal notes (maintainers)

These are historical engineering notes. They are **not** meant as end-user docs.

> Note: older entries referenced root-level filenames (e.g., `main.py`). The repo has since been refactored to a src-layout; updated paths are shown below.

## 2026-01-28 — Dashboard stability fixes

- **Bus load display smoothing**:
  - `src/roi/can/metrics.py` (`can_metrics.BusLoadMeter`) supports EMA smoothing (`smooth_alpha`)
  - `src/roi/app.py` wires it to `roi.config.CAN_BUS_LOAD_SMOOTH_ALPHA`

- **Control watchdog reliability**:
  - Timestamps use `time.monotonic()` (resists NTP/system clock adjustments)
  - Soft vs hard timeout via `roi.config.WATCHDOG_GRACE_SEC`
  - Separate “CAN liveness” timeout (`roi.config.CAN_TIMEOUT_SEC`) independent of any specific control frame

Files touched:
- `src/roi/app.py`
- `src/roi/ui/dashboard.py`
- `src/roi/can/metrics.py`
- `src/roi/config.py`

## 2026-01-30 — MrSignal (MR2.0) Modbus RTU support

- Added `src/roi/devices/mrsignal.py` (minimalmodbus client with byteorder compatibility helpers)
- Wired MrSignal into `HardwareManager` for init/idle/shutdown and redundant-write suppression
- Added `MRSIGNAL_*` config/env options + CAN IDs + optional CAN readback frames
- Dashboard displays MrSignal status (ID, output enable, mode, setpoint, input, float byteorder)

## 2026-01-30 — USB auto-detect: prevent 5491B “bus command error” during VISA scanning

Root cause:
- PyVISA discovery can probe many `ASRL/...` resources with a forced baud and `*IDN?`.
- If that hits the 5491B at the wrong baud, the meter may display a bus/command error.

Mitigation:
- Auto-detect excludes ASRL resources that map to already-discovered serial ports (multimeter + MrSignal)
- Excludes onboard/console ports (ttyAMA*, ttyS*; configurable)
- Allows disabling ASRL probing entirely

Config knobs:
- `AUTO_DETECT_VISA_PROBE_ASRL`
- `AUTO_DETECT_ASRL_BAUD`
- `AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES`
- `AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES`

Files touched:
- `src/roi/core/device_discovery.py`
- `src/roi/config.py`
