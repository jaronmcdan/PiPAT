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

# VISA Resource IDs (PyVISA)
ELOAD_VISA_ID = _env_str("ELOAD_VISA_ID", "USB0::11975::34816::*::0::INSTR")
AFG_VISA_ID = _env_str("AFG_VISA_ID", "ASRL/dev/ttyACM0::INSTR")

# --- GPIO / Relay ---
K1_PIN_BCM = _env_int("K1_PIN_BCM", 26)

# Relay input polarity:
# - True  => relay input is active-low (GPIO LOW energizes coil)
# - False => relay input is active-high
RELAY_ACTIVE_LOW = _env_bool("RELAY_ACTIVE_LOW", False)

# Relay contact wiring:
# - True  => DUT is wired through NC (Normally Closed): coil energized CUTS power
# - False => DUT is wired through NO (Normally Open):  coil energized APPLIES power
RELAY_WIRING_NC = _env_bool("RELAY_WIRING_NC", True)

# CAN relay semantics:
# - True  => CAN bit0==1 requests DUT POWER OFF (kill)
# - False => CAN bit0==1 requests DUT POWER ON
RELAY_CAN_BIT1_IS_POWER_OFF = _env_bool("RELAY_CAN_BIT1_IS_POWER_OFF", True)

# Desired idle/default DUT power state when control is missing (watchdog timeout)
# and (optionally) on program startup.
RELAY_IDLE_DUT_POWER = _env_bool("RELAY_IDLE_DUT_POWER", False)


# --- CAN Bus ---
CAN_CHANNEL = _env_str("CAN_CHANNEL", "can1")
CAN_BITRATE = _env_int("CAN_BITRATE", 250000)

# If True, main.py will try to bring the SocketCAN interface up.
CAN_SETUP = _env_bool("CAN_SETUP", True)

# --- Control watchdog ---
# If a given device doesn't receive its control message within the timeout,
# we drive that device back to its configured idle state.
CONTROL_TIMEOUT_SEC = _env_float("CONTROL_TIMEOUT_SEC", 2.0)
RELAY_TIMEOUT_SEC = _env_float("RELAY_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
ELOAD_TIMEOUT_SEC = _env_float("ELOAD_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
AFG_TIMEOUT_SEC = _env_float("AFG_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
MMETER_TIMEOUT_SEC = _env_float("MMETER_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)

# If True, apply idle states immediately on startup before processing controls.
APPLY_IDLE_ON_STARTUP = _env_bool("APPLY_IDLE_ON_STARTUP", True)

# Headless mode disables the Rich TUI (useful for systemd/journald).
ROI_HEADLESS = _env_bool("ROI_HEADLESS", False)

# --- Optional idle behavior for instruments ---
# E-load: idle means input off and short off.
ELOAD_IDLE_INPUT_ON = _env_bool("ELOAD_IDLE_INPUT_ON", False)
ELOAD_IDLE_SHORT_ON = _env_bool("ELOAD_IDLE_SHORT_ON", False)

# AFG: idle means output off.
AFG_IDLE_OUTPUT_ON = _env_bool("AFG_IDLE_OUTPUT_ON", False)


# --- CAN IDs (Control) ---
LOAD_CTRL_ID = 0x0CFF0400
RLY_CTRL_ID = 0x0CFF0500
MMETER_CTRL_ID = 0x0CFF0600
AFG_CTRL_ID = 0x0CFF0700  # Enable, Shape, Freq, Ampl
AFG_CTRL_EXT_ID = 0x0CFF0701  # Offset, Duty Cycle

# --- CAN IDs (Readback) ---
ELOAD_READ_ID = 0x0CFF0003
MMETER_READ_ID = 0x0CFF0004
AFG_READ_ID = 0x0CFF0005  # Status: Enable, Freq, Ampl
AFG_READ_EXT_ID = 0x0CFF0006  # Status: Offset, Duty Cycle


# --- CAN transmit behavior ---
# Regulate outgoing readback frames (ELOAD/MMETER/AFG status) to a fixed rate.
CAN_TX_ENABLE = _env_bool("CAN_TX_ENABLE", True)
CAN_TX_PERIOD_MS = _env_int("CAN_TX_PERIOD_MS", 50)
