# hardware.py

from __future__ import annotations

import fnmatch
import threading
import time
from typing import Optional

import pyvisa
import serial
from gpiozero import LED

import config


def _clamp_i16(x: int) -> int:
    if x < -32768:
        return -32768
    if x > 32767:
        return 32767
    return x


class HardwareManager:
    """Manages communication and state for the e-load, multimeter, AFG, and relay."""

    def __init__(self):
        # --- State Variables ---
        # e-load
        self.e_load_enabled: int = 0
        self.e_load_mode: int = 0
        self.e_load_short: int = 0
        self.e_load_csetting: int = 0
        self.e_load_rsetting: int = 0

        # multimeter
        self.multi_meter: Optional[serial.Serial] = None
        self.multi_meter_mode: int = 0
        self.multi_meter_range: int = 0
        self.mmeter_id: Optional[str] = None

        # AFG
        self.afg = None
        self.afg_id: Optional[str] = None
        self.afg_output: bool = False
        self.afg_shape: int = 0  # 0=SIN, 1=SQU, 2=RAMP
        self.afg_freq: int = 1000
        self.afg_ampl: int = 1000  # mVpp
        self.afg_offset: int = 0  # mV
        self.afg_duty: int = 50  # %

        # VISA
        self.resource_manager = None
        self.e_load = None

        # Locks (Thread Safety)
        self.eload_lock = threading.Lock()
        self.mmeter_lock = threading.Lock()
        self.afg_lock = threading.Lock()

        # --- GPIO Relay (K1) ---
        # K1 is treated as a direct drive output. We intentionally do not infer "DUT power"
        # from contact wiring (NC/NO). If you need true DUT power status, measure it.
        initial_drive = bool(getattr(config, "K1_IDLE_DRIVE", False))

        self.relay = LED(
            config.K1_PIN_BCM,
            # active_high is the *electrical* polarity of the relay input.
            # If K1_ACTIVE_LOW=True, then LED.on() drives the pin LOW.
            active_high=not bool(getattr(config, "K1_ACTIVE_LOW", False)),
            # initial_value is whether LED is "on" (drive asserted).
            initial_value=bool(initial_drive),
        )

    # --- Relay helpers ---
    def get_k1_drive(self) -> bool:
        "Return the logical drive state we are commanding via gpiozero (ON/OFF)."
        return bool(self.relay.is_lit)

    def get_k1_pin_level(self):
        "Return the raw GPIO level (True=HIGH, False=LOW) if available."
        try:
            pin = getattr(self.relay, 'pin', None)
            if pin is None:
                return None
            if hasattr(pin, 'state'):
                return bool(pin.state)
            if hasattr(pin, 'value'):
                return bool(pin.value)
        except Exception:
            return None
        return None

    def set_k1_drive(self, drive_on: bool) -> None:
        "Set K1 drive directly (no DUT inference)."
        if drive_on:
            self.relay.on()
        else:
            self.relay.off()

    def set_k1_idle(self) -> None:
        "Apply K1 idle drive state."
        self.set_k1_drive(bool(getattr(config, 'K1_IDLE_DRIVE', False)))





    def initialize_devices(self) -> None:
        """Initializes the multi-meter, e-load, and AFG."""
        self._initialize_multimeter()
        self._initialize_visa_devices()

    def _initialize_multimeter(self) -> None:
        try:
            mmeter = serial.Serial(
                config.MULTI_METER_PATH,
                int(config.MULTI_METER_BAUD),
                timeout=1,
                write_timeout=1,
            )
            # Clear any garbage that could cause decode issues.
            try:
                mmeter.reset_input_buffer()
                mmeter.reset_output_buffer()
            except Exception:
                pass

            mmeter.write(b"*IDN?\n")
            mmeter.flush()
            raw = mmeter.readline()
            self.mmeter_id = raw.decode("ascii", errors="replace").strip() or None
            print(f"MULTI-METER ID: {self.mmeter_id or 'Unknown'}")
            self.multi_meter = mmeter
        except (serial.SerialException, IOError) as e:
            print(f"Failed to communicate with multi-meter: {e}")
            self.multi_meter = None

    def _initialize_visa_devices(self) -> None:
        """Initializes both E-Load and AFG via PyVISA."""
        try:
            self.resource_manager = pyvisa.ResourceManager()

            # --- 1. E-LOAD (Scan for USBTMC / match pattern) ---
            try:
                available_resources = list(self.resource_manager.list_resources())
                print(f"Scanning for E-Load in: {available_resources}")

                for resource_id in available_resources:
                    if fnmatch.fnmatch(resource_id, config.ELOAD_VISA_ID):
                        try:
                            dev = self.resource_manager.open_resource(resource_id)
                            dev_id = dev.query("*IDN?").strip()
                            print(f"E-LOAD FOUND: {dev_id}")
                            dev.write("*RST")
                            dev.write("SYST:CLE")
                            self.e_load = dev
                            break
                        except Exception as e:
                            print(f"Skip E-LOAD ({resource_id}): {e}")
            except Exception as e:
                print(f"E-Load Scan Error: {e}")

            # --- 2. AFG (Direct Connect) ---
            try:
                print(f"Attempting AFG connection at {config.AFG_VISA_ID}...")
                afg_dev = self.resource_manager.open_resource(config.AFG_VISA_ID)
                # Some VISA backends expose serial config fields
                try:
                    afg_dev.baud_rate = 115200
                except Exception:
                    pass
                afg_dev.read_termination = "\n"
                afg_dev.write_termination = "\n"

                dev_id = afg_dev.query("*IDN?").strip()
                print(f"AFG FOUND: {dev_id}")
                self.afg = afg_dev
                self.afg_id = dev_id

            except Exception as e:
                print(f"AFG Connection Failed ({config.AFG_VISA_ID}): {e}")

            if not self.e_load:
                print("WARNING: E-LOAD not found.")
            if not self.afg:
                print("WARNING: AFG not found.")

        except Exception as e:
            print(f"Critical VISA Error: {e}")

    # --- Idle / shutdown helpers (used by watchdog) ---
    def apply_idle_eload(self) -> None:
        if not self.e_load:
            return
        try:
            with self.eload_lock:
                # Input off is the safety-critical part.
                self.e_load.write("INP ON" if config.ELOAD_IDLE_INPUT_ON else "INP OFF")
                self.e_load.write("INP:SHOR ON" if config.ELOAD_IDLE_SHORT_ON else "INP:SHOR OFF")
            self.e_load_enabled = 1 if config.ELOAD_IDLE_INPUT_ON else 0
            self.e_load_short = 1 if config.ELOAD_IDLE_SHORT_ON else 0
        except Exception:
            pass

    def apply_idle_afg(self) -> None:
        if not self.afg:
            return
        try:
            with self.afg_lock:
                self.afg.write(f"SOUR1:OUTP {'ON' if config.AFG_IDLE_OUTPUT_ON else 'OFF'}")
            self.afg_output = bool(config.AFG_IDLE_OUTPUT_ON)
        except Exception:
            pass

    def apply_idle_all(self) -> None:
        # Relay is always present
        try:
            self.set_k1_idle()
        except Exception:
            pass
        self.apply_idle_eload()
        self.apply_idle_afg()

    def close_devices(self) -> None:
        # Best-effort safety shutdown
        try:
            self.apply_idle_all()
        except Exception:
            pass

        if self.multi_meter:
            try:
                self.multi_meter.close()
            except Exception:
                pass
        if self.e_load:
            try:
                self.e_load.close()
            except Exception:
                pass
        if self.afg:
            try:
                self.afg.close()
            except Exception:
                pass
        if self.resource_manager:
            try:
                self.resource_manager.close()
            except Exception:
                pass
