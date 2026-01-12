# config.py

# --- Hardware Identifiers ---
MULTI_METER_PATH = '/dev/ttyUSB0'
MULTI_METER_BAUD = 38400

# VISA Resource IDs
ELOAD_VISA_ID = "USB0::11975::34816::*::0::INSTR"
AFG_VISA_ID   = "ASRL/dev/ttyACM0::INSTR" 

# --- GPIO Configuration ---
K1_PIN_BCM = 26
RELAY_ACTIVE_LOW = False

# Relay contact wiring:
# - True  => DUT is wired through NC (Normally Closed): coil energized CUTS power
# - False => DUT is wired through NO (Normally Open):  coil energized APPLIES power
RELAY_WIRING_NC = True

# CAN relay semantics:
# - True  => CAN bit0==1 requests DUT POWER OFF (kill)
# - False => CAN bit0==1 requests DUT POWER ON
RELAY_CAN_BIT1_IS_POWER_OFF = True

# --- CAN Bus Configuration ---
CAN_CHANNEL = "can1"
CAN_BITRATE = 250000

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

