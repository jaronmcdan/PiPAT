# config.py

import os

def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from environment variables.

    Accepts: 1/0, true/false, yes/no, on/off (case-insensitive).
    """
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}

# --- Hardware Identifiers ---
MULTI_METER_PATH = os.getenv("MULTI_METER_PATH", "/dev/ttyUSB0")
MULTI_METER_BAUD = int(os.getenv("MULTI_METER_BAUD", "38400"))

# VISA Resource IDs
ELOAD_VISA_ID = os.getenv("ELOAD_VISA_ID", "USB0::11975::34816::*::0::INSTR")
AFG_VISA_ID   = os.getenv("AFG_VISA_ID",   "ASRL/dev/ttyACM0::INSTR")

# --- GPIO Configuration ---
K1_PIN_BCM = int(os.getenv("K1_PIN_BCM", "26"))

# If True, the relay board is active-low (GPIO LOW energizes the relay).
RELAY_ACTIVE_LOW = _env_bool("RELAY_ACTIVE_LOW", False)

# Logical relay state to apply on startup (True = relay energized / "CLOSED").
# Override per-Pi with env var RELAY_INITIAL_ON=0/1 if you swap between hats.
RELAY_INITIAL_ON = _env_bool("RELAY_INITIAL_ON", True)

# --- CAN Bus Configuration ---
CAN_CHANNEL = os.getenv("CAN_CHANNEL", "can1")
CAN_BITRATE = int(os.getenv("CAN_BITRATE", "250000"))

# CAN IDs (Control)
LOAD_CTRL_ID    = 0x0CFF0400
RLY_CTRL_ID     = 0x0CFF0500
MMETER_CTRL_ID  = 0x0CFF0600
AFG_CTRL_ID     = 0x0CFF0700  # Enable, Shape, Freq, Ampl
AFG_CTRL_EXT_ID = 0x0CFF0701  # Offset, Duty Cycle

# CAN IDs (Readback)
ELOAD_READ_ID   = 0x0CFF0003
MMETER_READ_ID  = 0x0CFF0004
AFG_READ_ID     = 0x0CFF0005  # Status: Enable, Freq, Ampl
AFG_READ_EXT_ID = 0x0CFF0006  # Status: Offset, Duty Cycle
