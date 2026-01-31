"""Runtime configuration for ROI Instrument Bridge.

All values in this file can be overridden via environment variables.
This is useful when running as a systemd service, where /etc/roi/roi.env
can hold per-Pi settings.

Parsing rules:
- booleans: 1/0, true/false, yes/no, on/off
- integers: decimal by default; "0x" prefix is allowed for hex
- floats: standard Python float format
"""

from __future__ import annotations

import os
from typing import Optional


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    s = v.strip().lower()
    try:
        # allow hex like 0x1a
        return int(s, 0)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v.strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    s = v.strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


# --- Hardware Identifiers ---
MULTI_METER_PATH = _env_str("MULTI_METER_PATH", "/dev/ttyUSB0")
MULTI_METER_BAUD = _env_int("MULTI_METER_BAUD", 38400)
MULTI_METER_TIMEOUT = _env_float("MULTI_METER_TIMEOUT", 1.0)
MULTI_METER_WRITE_TIMEOUT = _env_float("MULTI_METER_WRITE_TIMEOUT", 1.0)

# If True, HardwareManager will send "*IDN?" again on startup to verify the
# meter is responsive. If False (default), we avoid sending extra commands on
# boot because some 5491B units will beep/throw a bus error if *anything* else
# touches the port during early init (e.g., VISA ASRL probing).
MULTI_METER_VERIFY_ON_STARTUP = _env_bool("MULTI_METER_VERIFY_ON_STARTUP", False)

# Optional cached IDN string (patched at runtime by device_discovery).
MULTI_METER_IDN = _env_str("MULTI_METER_IDN", "")

# Many USB-serial instruments echo commands and/or respond a moment later.
# These settings make IDN probing more robust.
MULTI_METER_IDN_DELAY = _env_float("MULTI_METER_IDN_DELAY", 0.05)
MULTI_METER_IDN_READ_LINES = _env_int("MULTI_METER_IDN_READ_LINES", 4)

# --- Optional USB auto-detection (Raspberry Pi) ---
# When enabled, main.py will scan /dev/serial/by-id + PyVISA resources at startup
# and patch these config values at runtime:
#   - MULTI_METER_PATH
#   - MRSIGNAL_PORT
#   - AFG_VISA_ID
#   - ELOAD_VISA_ID
AUTO_DETECT_ENABLE = _env_bool("AUTO_DETECT_ENABLE", True)
AUTO_DETECT_VERBOSE = _env_bool("AUTO_DETECT_VERBOSE", True)

# Sub-features
AUTO_DETECT_MMETER = _env_bool("AUTO_DETECT_MMETER", True)
AUTO_DETECT_MRSIGNAL = _env_bool("AUTO_DETECT_MRSIGNAL", True)
AUTO_DETECT_VISA = _env_bool("AUTO_DETECT_VISA", True)
AUTO_DETECT_AFG = _env_bool("AUTO_DETECT_AFG", True)
AUTO_DETECT_ELOAD = _env_bool("AUTO_DETECT_ELOAD", True)

# VISA/serial probing safety:
# - Probing ASRL resources sends *IDN? over a serial port. If the baud is wrong
#   for some attached device, that device may show an error. We therefore:
#     - allow ASRL probing to be disabled
#     - use a configurable baud
#     - skip known serial ports already claimed by other devices
AUTO_DETECT_VISA_PROBE_ASRL = _env_bool("AUTO_DETECT_VISA_PROBE_ASRL", True)
AUTO_DETECT_ASRL_BAUD = _env_int("AUTO_DETECT_ASRL_BAUD", 115200)

# Comma-separated device-node prefixes to exclude from ASRL probing.
# (e.g. the Pi's onboard UARTs or console serial ports)
AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES = _env_str(
    "AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES",
    # NOTE: many USB-serial instruments (like the 5491B DMM) are /dev/ttyUSB*.
    # Probing those as VISA ASRL at the wrong baud can make them beep and/or
    # enter an error state. Default: exclude USB-serial ports.
    "/dev/ttyAMA,/dev/ttyS,/dev/ttyUSB",
)

# Comma-separated device-node prefixes to *allow* for ASRL probing.
# If set, only these serial devices will be probed via VISA. This is the
# safest way to avoid poking non-SCPI USB-serial devices.
# Default: only probe CDC-ACM devices (many bench instruments show up as ttyACM*).
AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES = _env_str(
    "AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES",
    "/dev/ttyACM",
)

# Prefer stable symlinks (when present)
AUTO_DETECT_PREFER_BY_ID = _env_bool("AUTO_DETECT_PREFER_BY_ID", True)

# Optional: force a PyVISA backend ("@py" for pyvisa-py). Empty => default.
AUTO_DETECT_VISA_BACKEND = _env_str("AUTO_DETECT_VISA_BACKEND", "")

# IDN matching hints (comma-separated, case-insensitive)
AUTO_DETECT_MMETER_IDN_HINTS = _env_str("AUTO_DETECT_MMETER_IDN_HINTS", "multimeter,5491b")
AUTO_DETECT_AFG_IDN_HINTS = _env_str("AUTO_DETECT_AFG_IDN_HINTS", "afg,function,generator,arb")
AUTO_DETECT_ELOAD_IDN_HINTS = _env_str("AUTO_DETECT_ELOAD_IDN_HINTS", "load,eload,electronic load,dl,it,bk")

# VISA Resource IDs (PyVISA)
ELOAD_VISA_ID = _env_str("ELOAD_VISA_ID", "USB0::11975::34816::*::0::INSTR")
AFG_VISA_ID = _env_str("AFG_VISA_ID", "ASRL/dev/ttyACM1::INSTR")

# PyVISA I/O timeout (milliseconds). Lower values reduce "sluggish" feel when a
# device is disconnected or slow to respond, at the cost of more timeouts.
VISA_TIMEOUT_MS = _env_int("VISA_TIMEOUT_MS", 500)

# --- GPIO / K1 relay drive ---
K1_ENABLE = _env_bool("K1_ENABLE", True)
K1_PIN_BCM = _env_int("K1_PIN_BCM", 26)

# Relay input polarity:
# - True  => relay input is active-low (GPIO LOW energizes coil)
# - False => relay input is active-high
K1_ACTIVE_LOW = _env_bool("K1_ACTIVE_LOW", False)

# If True, invert the incoming CAN bit0 before driving K1.
K1_CAN_INVERT = _env_bool("K1_CAN_INVERT", False)

# Idle/default drive state for K1 when control is missing (watchdog timeout)
# and (optionally) on program startup.
K1_IDLE_DRIVE = _env_bool("K1_IDLE_DRIVE", False)

# --- CAN Bus ---
CAN_CHANNEL = _env_str("CAN_CHANNEL", "can1")
CAN_BITRATE = _env_int("CAN_BITRATE", 250000)

# If True, main.py will try to bring the SocketCAN interface up.
CAN_SETUP = _env_bool("CAN_SETUP", True)

# Max number of incoming CAN control frames buffered between the CAN RX thread
# and the device command worker. Keeping this bounded ensures the CAN RX loop
# never blocks on slow instrument I/O.
CAN_CMD_QUEUE_MAX = _env_int("CAN_CMD_QUEUE_MAX", 256)

# --- Control watchdog ---
# If a given device doesn't receive its control message within the timeout,
# we drive that device back to its configured idle state.
CONTROL_TIMEOUT_SEC = _env_float("CONTROL_TIMEOUT_SEC", 2.0)

# Extra grace before declaring a *hard* timeout (beyond CONTROL_TIMEOUT_SEC).
# This eliminates most UI flicker caused by borderline jitter when control
# frames arrive near the threshold.
WATCHDOG_GRACE_SEC = _env_float("WATCHDOG_GRACE_SEC", 0.25)

# Timeout used for the "CAN" freshness indicator (any CAN message received).
CAN_TIMEOUT_SEC = _env_float("CAN_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
K1_TIMEOUT_SEC = _env_float("K1_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
ELOAD_TIMEOUT_SEC = _env_float("ELOAD_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
AFG_TIMEOUT_SEC = _env_float("AFG_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
MMETER_TIMEOUT_SEC = _env_float("MMETER_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)

# If True, apply idle states immediately on startup before processing controls.
APPLY_IDLE_ON_STARTUP = _env_bool("APPLY_IDLE_ON_STARTUP", True)

# Headless mode disables the Rich TUI (useful for systemd/journald).
ROI_HEADLESS = _env_bool("ROI_HEADLESS", False)


# --- Dashboard / polling ---
# DASH_FPS controls only the Rich TUI render rate (it does NOT affect CAN).
DASH_FPS = _env_int("DASH_FPS", 15)

# Instrument polling cadence (seconds). These govern how often values update on the dashboard
# and how frequently outgoing readback frames can change.
MEAS_POLL_PERIOD = _env_float("MEAS_POLL_PERIOD", 0.2)      # fast measurements (V/I, meter)
STATUS_POLL_PERIOD = _env_float("STATUS_POLL_PERIOD", 1.0)  # slow status (setpoints/mode)


# --- Optional idle behavior for instruments ---
# E-load: idle means input off and short off.
ELOAD_IDLE_INPUT_ON = _env_bool("ELOAD_IDLE_INPUT_ON", False)
ELOAD_IDLE_SHORT_ON = _env_bool("ELOAD_IDLE_SHORT_ON", False)

# AFG: idle means output off.
AFG_IDLE_OUTPUT_ON = _env_bool("AFG_IDLE_OUTPUT_ON", False)

# MrSignal / LANYI MR2.0 (Modbus RTU over USB-serial)
MRSIGNAL_ENABLE = _env_bool("MRSIGNAL_ENABLE", True)
# Default is /dev/ttyUSB1 to avoid colliding with the multimeter default (/dev/ttyUSB0).
MRSIGNAL_PORT = _env_str("MRSIGNAL_PORT", "/dev/ttyACM0")
MRSIGNAL_BAUD = _env_int("MRSIGNAL_BAUD", 9600)
MRSIGNAL_SLAVE_ID = _env_int("MRSIGNAL_SLAVE_ID", 1)
MRSIGNAL_PARITY = _env_str("MRSIGNAL_PARITY", "N")  # N/E/O
MRSIGNAL_STOPBITS = _env_int("MRSIGNAL_STOPBITS", 1)  # 1 or 2
MRSIGNAL_TIMEOUT = _env_float("MRSIGNAL_TIMEOUT", 0.5)

# Float byteorder handling (minimalmodbus varies between versions/devices)
# Examples: BYTEORDER_BIG, BYTEORDER_LITTLE, BYTEORDER_BIG_SWAP, BYTEORDER_LITTLE_SWAP
MRSIGNAL_FLOAT_BYTEORDER = _env_str("MRSIGNAL_FLOAT_BYTEORDER", "")
MRSIGNAL_FLOAT_BYTEORDER_AUTO = _env_bool("MRSIGNAL_FLOAT_BYTEORDER_AUTO", True)

# Safety clamps (applied to incoming CAN setpoints)
MRSIGNAL_MAX_V = _env_float("MRSIGNAL_MAX_V", 24.0)
MRSIGNAL_MAX_MA = _env_float("MRSIGNAL_MAX_MA", 24.0)

# Idle behavior: output OFF by default (safety)
MRSIGNAL_IDLE_OUTPUT_ON = _env_bool("MRSIGNAL_IDLE_OUTPUT_ON", False)

# Watchdog timeout (seconds)
MRSIGNAL_TIMEOUT_SEC = _env_float("MRSIGNAL_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)

# Poll cadence for status/input reads (seconds)
MRSIGNAL_POLL_PERIOD = _env_float("MRSIGNAL_POLL_PERIOD", STATUS_POLL_PERIOD)


# --- CAN IDs (Control) ---
LOAD_CTRL_ID = 0x0CFF0400
RLY_CTRL_ID = 0x0CFF0500
MMETER_CTRL_ID = 0x0CFF0600
AFG_CTRL_ID = 0x0CFF0700  # Enable, Shape, Freq, Ampl
AFG_CTRL_EXT_ID = 0x0CFF0701  # Offset, Duty Cycle

# MrSignal control (enable/mode/value float)
MRSIGNAL_CTRL_ID = 0x0CFF0800

# --- CAN IDs (Readback) ---
ELOAD_READ_ID = 0x0CFF0003
MMETER_READ_ID = 0x0CFF0004
AFG_READ_ID = 0x0CFF0005  # Status: Enable, Freq, Ampl
AFG_READ_EXT_ID = 0x0CFF0006  # Status: Offset, Duty Cycle

# MrSignal readback (optional)
MRSIGNAL_READ_STATUS_ID = 0x0CFF0007
MRSIGNAL_READ_INPUT_ID = 0x0CFF0008


# --- CAN bus load estimator (dashboard) ---
# Enabled by default; set to 0 to hide/disable bus load calculation.
CAN_BUS_LOAD_ENABLE = _env_bool("CAN_BUS_LOAD_ENABLE", True)

# Sliding window for the estimator (seconds).
CAN_BUS_LOAD_WINDOW_SEC = _env_float("CAN_BUS_LOAD_WINDOW_SEC", 1.0)

# Physical-layer bit stuffing increases actual bits on-wire; 1.2 is a reasonable heuristic.
CAN_BUS_LOAD_STUFFING_FACTOR = _env_float("CAN_BUS_LOAD_STUFFING_FACTOR", 1.2)

# Exponential smoothing for the displayed bus load percent. 0.0 disables.
CAN_BUS_LOAD_SMOOTH_ALPHA = _env_float("CAN_BUS_LOAD_SMOOTH_ALPHA", 0.25)

# Approximate overhead bits per classic CAN frame excluding data (SOF..IFS). This is an estimate.
CAN_BUS_LOAD_OVERHEAD_BITS = _env_int("CAN_BUS_LOAD_OVERHEAD_BITS", 48)


# --- CAN transmit behavior ---
# Regulate outgoing readback frames (ELOAD/MMETER/AFG status) to a fixed rate.
CAN_TX_ENABLE = _env_bool("CAN_TX_ENABLE", True)
CAN_TX_PERIOD_MS = _env_int("CAN_TX_PERIOD_MS", 50)
