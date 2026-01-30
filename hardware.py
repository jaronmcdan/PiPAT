# hardware.py

from __future__ import annotations

import fnmatch
import math
import threading
import time
from typing import Optional

import pyvisa
import serial
from gpiozero import LED

import config
from mrsignal import MrSignalClient, MrSignalStatus


class _NullRelay:
    """Fallback relay implementation used when GPIO is unavailable.

    Provides a minimal gpiozero-like interface (on/off/is_lit/pin) so the rest
    of the application can run in dev environments, containers, or hosts that
    lack Raspberry Pi GPIO support.
    """

    def __init__(self, initial_drive: bool = False):
        self._is_lit = bool(initial_drive)

    @property
    def is_lit(self) -> bool:
        return bool(self._is_lit)

    def on(self) -> None:
        self._is_lit = True

    def off(self) -> None:
        self._is_lit = False

    @property
    def pin(self):
        return None


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

        # MrSignal (MR2.0) via Modbus RTU over USB-serial
        self.mrsignal: MrSignalClient | None = None
        self.mrsignal_id: Optional[int] = None
        self.mrsignal_output_on: bool = False
        self.mrsignal_output_select: int = 1  # default V
        self.mrsignal_output_value: float = 0.0
        self.mrsignal_input_value: float = 0.0
        self.mrsignal_float_byteorder: str = "DEFAULT"

        # Last commanded values (to suppress redundant Modbus writes)
        self._mrs_last_enable: Optional[bool] = None
        self._mrs_last_select: Optional[int] = None
        self._mrs_last_value: Optional[float] = None

        # VISA
        self.resource_manager = None
        self.e_load = None

        # Locks (Thread Safety)
        self.eload_lock = threading.Lock()
        self.mmeter_lock = threading.Lock()
        self.afg_lock = threading.Lock()

        self.mrsignal_lock = threading.Lock()

        # --- GPIO Relay (K1) ---
        # K1 is treated as a direct drive output. We intentionally do not infer "DUT power"
        # from contact wiring (NC/NO). If you need true DUT power status, measure it.
        initial_drive = bool(getattr(config, "K1_IDLE_DRIVE", False))

        self.relay_backend: str = "disabled"
        if bool(getattr(config, "K1_ENABLE", True)):
            try:
                self.relay = LED(
                    config.K1_PIN_BCM,
                    # active_high is the *electrical* polarity of the relay input.
                    # If K1_ACTIVE_LOW=True, then LED.on() drives the pin LOW.
                    active_high=not bool(getattr(config, "K1_ACTIVE_LOW", False)),
                    # initial_value is whether LED is "on" (drive asserted).
                    initial_value=bool(initial_drive),
                )
                self.relay_backend = "gpio"
            except Exception as e:
                # Typical causes: running off-Pi, missing pin factory backends
                # (lgpio/RPi.GPIO/pigpio), or insufficient permissions for GPIO.
                print(f"WARNING: K1 GPIO unavailable; running with a mock relay. ({e})")
                self.relay = _NullRelay(initial_drive)
                self.relay_backend = "mock"
        else:
            self.relay = _NullRelay(initial_drive)
            self.relay_backend = "disabled"

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
        self._initialize_mrsignal()

    def _initialize_multimeter(self) -> None:
        try:
            mmeter = serial.Serial(
                config.MULTI_METER_PATH,
                int(config.MULTI_METER_BAUD),
                timeout=float(getattr(config, 'MULTI_METER_TIMEOUT', 1.0)),
                write_timeout=float(getattr(config, 'MULTI_METER_WRITE_TIMEOUT', 1.0)),
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

    def _initialize_mrsignal(self) -> None:
        """Initialize MrSignal (LANYI MR2.0) Modbus RTU device if enabled.

        MrSignal is controlled via Modbus RTU over a USB-serial adapter and
        driven by CAN control frames handled by the receiver thread.
        """

        if not bool(getattr(config, "MRSIGNAL_ENABLE", False)):
            self.mrsignal = None
            return

        port = str(getattr(config, "MRSIGNAL_PORT", "") or "").strip()
        if not port:
            print("MrSignal disabled: MRSIGNAL_PORT is empty")
            self.mrsignal = None
            return

        try:
            client = MrSignalClient(
                port=port,
                slave_id=int(getattr(config, "MRSIGNAL_SLAVE_ID", 1)),
                baud=int(getattr(config, "MRSIGNAL_BAUD", 9600)),
                parity=str(getattr(config, "MRSIGNAL_PARITY", "N")),
                stopbits=int(getattr(config, "MRSIGNAL_STOPBITS", 1)),
                timeout_s=float(getattr(config, "MRSIGNAL_TIMEOUT", 0.5)),
                float_byteorder=(str(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER", "") or "").strip() or None),
                float_byteorder_auto=bool(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER_AUTO", True)),
            )
            client.connect()

            # Best-effort initial read so we can surface status immediately.
            st = client.read_status()

            self.mrsignal = client
            self.mrsignal_id = st.device_id
            self.mrsignal_output_on = bool(st.output_on) if st.output_on is not None else False
            self.mrsignal_output_select = int(st.output_select or 0)
            if st.output_value is not None:
                self.mrsignal_output_value = float(st.output_value)
            if st.input_value is not None:
                self.mrsignal_input_value = float(st.input_value)
            self.mrsignal_float_byteorder = str(st.float_byteorder or "DEFAULT")

            print(
                f"MrSignal FOUND: port={port} slave={getattr(client, 'slave_id', '?')} "
                f"id={self.mrsignal_id} mode={st.mode_label} bo={self.mrsignal_float_byteorder}"
            )

        except Exception as e:
            print(f"MrSignal connection failed ({port}): {e}")
            try:
                if self.mrsignal:
                    self.mrsignal.close()
            except Exception:
                pass
            self.mrsignal = None

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


    def apply_idle_mrsignal(self) -> None:
        if not self.mrsignal:
            return
        try:
            with self.mrsignal_lock:
                # Output enable is the safety-critical part.
                self.mrsignal.set_enable(bool(getattr(config, "MRSIGNAL_IDLE_OUTPUT_ON", False)))
            self.mrsignal_output_on = bool(getattr(config, "MRSIGNAL_IDLE_OUTPUT_ON", False))
        except Exception:
            pass


    def set_mrsignal(self, *, enable: bool, output_select: int, value: float,
                     max_v: float | None = None, max_ma: float | None = None) -> None:
        """Apply MrSignal control with safety clamps and redundant-write suppression."""
        if not self.mrsignal:
            return

        # Clamp setpoint based on mode (0=mA, 1=V, 4=mV, 6=24V)
        v = float(value)
        sel = int(output_select)

        if sel == 0:  # mA
            lim = float(max_ma if max_ma is not None else getattr(config, "MRSIGNAL_MAX_MA", 24.0))
            if v < 0.0:
                v = 0.0
            if v > lim:
                v = lim
        elif sel in (1, 6):  # V / 24V
            lim = float(max_v if max_v is not None else getattr(config, "MRSIGNAL_MAX_V", 24.0))
            if v < 0.0:
                v = 0.0
            if v > lim:
                v = lim
        elif sel == 4:  # mV
            lim = float(max_v if max_v is not None else getattr(config, "MRSIGNAL_MAX_V", 24.0)) * 1000.0
            if v < 0.0:
                v = 0.0
            if v > lim:
                v = lim
        # else: unknown mode, do minimal clamping
        if not math.isfinite(v):
            v = 0.0

        # Redundant suppression
        if (self._mrs_last_enable is not None and self._mrs_last_select is not None and self._mrs_last_value is not None):
            if (bool(enable) == bool(self._mrs_last_enable)) and (int(sel) == int(self._mrs_last_select)) and (abs(float(v) - float(self._mrs_last_value)) < 1e-6):
                return

        with self.mrsignal_lock:
            self.mrsignal.set_output(enable=bool(enable), output_select=int(sel), value=float(v))

        self._mrs_last_enable = bool(enable)
        self._mrs_last_select = int(sel)
        self._mrs_last_value = float(v)

        # Update last-known commanded state (dashboard uses polled values too)
        self.mrsignal_output_on = bool(enable)
        self.mrsignal_output_select = int(sel)
        self.mrsignal_output_value = float(v)

    def apply_idle_all(self) -> None:
        # Relay is always present
        try:
            self.set_k1_idle()
        except Exception:
            pass
        self.apply_idle_eload()
        self.apply_idle_afg()
        self.apply_idle_mrsignal()

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
        if self.mrsignal:
            try:
                self.mrsignal.close()
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
