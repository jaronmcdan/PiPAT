# hardware.py
import serial
import pyvisa
import threading
import fnmatch
import time
from gpiozero import LED
import config  # Imports settings from config.py

class HardwareManager:
    """Manages communication and state for the e-load, multimeter, and AFG."""
    def __init__(self):
        # --- State Variables ---
        # e-load
        self.e_load_remote: int = 0
        self.e_load_enabled: int = 0
        self.e_load_mode: int = 0
        self.e_load_short: int = 0
        self.e_load_csetting: int = 0
        self.e_load_rsetting: int = 0
        self.pload_volts: float = 0.0
        self.pload_current: float = 0.0
        
        # multimeter
        self.multi_meter = None
        self.multi_meter_mode: int = 0
        self.multi_meter_range: int = 0
        self.mmeter_id = None

        # AFG
        self.afg = None
        self.afg_id = None
        self.afg_output: bool = False
        self.afg_shape: int = 0  # 0=SIN, 1=SQU, 2=TRI
        self.afg_freq: int = 1000
        self.afg_ampl: int = 1000 # mVpp
        self.afg_offset: int = 0   # mV
        self.afg_duty: int = 50    # %

        # VISA Shared Manager
        self.resource_manager = None
        self.e_load = None

        # Locks (Thread Safety)
        self.eload_lock = threading.Lock()
        self.mmeter_lock = threading.Lock()
        self.afg_lock = threading.Lock()

        # GPIO Relay
        # We treat `self.relay` as the *relay coil control* (energized / de-energized).
        # The DUT power state may be inverted depending on whether you're wired through NO or NC.
        self.relay = LED(
            config.K1_PIN_BCM,
            # active_high=True means: .on() drives GPIO HIGH.
            # For active-low relay inputs we invert this so .on() energizes the coil.
            active_high=not config.RELAY_ACTIVE_LOW,
            # initial_value is the *logical* state of the LED device, i.e. whether the coil
            # should start energized.
            initial_value=self._coil_should_be_energized(config.RELAY_START_POWER_ON),
        )

        # Track the intended DUT power state so the UI can show something meaningful.
        self.dut_power_on: bool = bool(config.RELAY_START_POWER_ON)

    def _coil_should_be_energized(self, dut_power_on: bool) -> bool:
        """Translate desired DUT power state to coil energized/de-energized."""
        # If wired through NO: energize coil to power DUT.
        # If wired through NC: de-energize coil to power DUT.
        return dut_power_on if config.RELAY_POWER_ON_WHEN_COIL_ENERGIZED else (not dut_power_on)

    def set_dut_power(self, dut_power_on: bool) -> None:
        """Set DUT power consistently across relay hats/wiring."""
        coil_energize = self._coil_should_be_energized(dut_power_on)
        if coil_energize:
            self.relay.on()
        else:
            self.relay.off()
        self.dut_power_on = bool(dut_power_on)

    def initialize_devices(self) -> None:
        """Initializes the multi-meter, e-load, and AFG."""
        self._initialize_multimeter()
        self._initialize_visa_devices()

    def _initialize_multimeter(self) -> None:
        try:
            mmeter = serial.Serial(config.MULTI_METER_PATH, config.MULTI_METER_BAUD, timeout=1)
            mmeter.write(b'*IDN?\n')
            self.mmeter_id = mmeter.readline().decode().strip()
            print(f"MULTI-METER ID: {self.mmeter_id}")
            self.multi_meter = mmeter
        except (serial.SerialException, IOError) as e:
            print(f"Failed to communicate with multi-meter: {e}")
            self.multi_meter = None

    def _initialize_visa_devices(self) -> None:
        """Initializes both E-Load and AFG via PyVISA."""
        try:
            self.resource_manager = pyvisa.ResourceManager()
            
            # --- 1. E-LOAD (Scan for USBTMC) ---
            try:
                available_resources = list(self.resource_manager.list_resources())
                print(f"Scanning for E-Load in: {available_resources}")
                
                for resource_id in available_resources:
                    if fnmatch.fnmatch(resource_id, config.ELOAD_VISA_ID):
                        try:
                            dev = self.resource_manager.open_resource(resource_id)
                            dev_id = dev.query('*IDN?').strip()
                            print(f"E-LOAD FOUND: {dev_id}")
                            dev.write('*RST')
                            dev.write('SYST:CLE')
                            self.e_load = dev
                            break
                        except Exception as e:
                            print(f"Skip E-LOAD ({resource_id}): {e}")
            except Exception as e:
                print(f"E-Load Scan Error: {e}")

            # --- 2. AFG-2125 (Direct Connect) ---
            try:
                print(f"Attempting AFG connection at {config.AFG_VISA_ID}...")
                afg_dev = self.resource_manager.open_resource(config.AFG_VISA_ID)
                afg_dev.baud_rate = 115200 
                afg_dev.read_termination = '\n'
                afg_dev.write_termination = '\n'
                
                dev_id = afg_dev.query('*IDN?').strip()
                print(f"AFG FOUND: {dev_id}")
                self.afg = afg_dev
                self.afg_id = dev_id

            except Exception as e:
                print(f"AFG Connection Failed ({config.AFG_VISA_ID}): {e}")

            if not self.e_load: print("WARNING: E-LOAD not found.")
            if not self.afg:    print("WARNING: AFG not found.")

        except Exception as e:
            print(f"Critical VISA Error: {e}")

    def close_devices(self) -> None:
        if self.multi_meter:
            self.multi_meter.close()
        if self.e_load:
            self.e_load.close()
        if self.afg:
            self.afg.close()
        if self.resource_manager:
            self.resource_manager.close()