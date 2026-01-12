# config.py

from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var.

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
AFG_VISA_ID   = os.getenv("AFG_VISA_ID", "ASRL/dev/ttyACM0::INSTR")

# --- GPIO Configuration ---
K1_PIN_BCM = int(os.getenv("K1_PIN_BCM", "26"))

# Electrical polarity of the relay input:
# - True:  GPIO LOW energizes the relay coil (active-low input)
# - False: GPIO HIGH energizes the relay coil (active-high input)
RELAY_ACTIVE_LOW = _env_bool("RELAY_ACTIVE_LOW", False)

# Wiring semantics:
# - True  => coil energized powers the DUT (wired through NO)
# - False => coil energized cuts power to the DUT (wired through NC)
RELAY_POWER_ON_WHEN_COIL_ENERGIZED = _env_bool("RELAY_POWER_ON_WHEN_COIL_ENERGIZED", True)

# CAN protocol semantics:
# - True  => CAN bit 0 value 1 means "DUT POWER ON"
# - False => CAN bit 0 value 0 means "DUT POWER ON"
RELAY_CAN_ON_IS_1 = _env_bool("RELAY_CAN_ON_IS_1", True)

# Desired DUT power state when the script starts.
# Set this so starting the script doesn't unexpectedly kill power.
RELAY_START_POWER_ON = _env_bool("RELAY_START_POWER_ON", True)


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
