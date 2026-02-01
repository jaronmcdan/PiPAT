# hardware.py

from __future__ import annotations

import fnmatch
import os
import errno
import math
import threading
import time
from typing import Optional

import pyvisa
import serial

try:
    from gpiozero import LED  # type: ignore
except Exception:
    LED = None  # type: ignore

import config
from bk5491b import BK5491B, MmeterFunc
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



def _is_raspberry_pi() -> bool:
    """Best-effort host detection to decide whether to attempt GPIO."""
    # Most Raspberry Pi OS / Debian-based images expose this.
    try:
        with open("/proc/device-tree/model", "rb") as f:
            model = f.read().decode("ascii", errors="ignore")
        if "Raspberry Pi" in model:
            return True
    except Exception:
        pass

    # Fallback: /proc/cpuinfo usually contains this string on Pi.
    try:
        with open("/proc/cpuinfo", "r", encoding="ascii", errors="ignore") as f:
            txt = f.read()
        if "Raspberry Pi" in txt:
            return True
    except Exception:
        pass

    return False


def _is_permission_denied(exc: Exception) -> bool:
    """Return True if an exception is (or wraps) an EACCES/EPERM."""
    try:
        if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
            return True
    except Exception:
        pass

    # pyserial / pyvisa sometimes wrap the underlying OSError in args
    try:
        for a in getattr(exc, "args", ()) or ():
            if isinstance(a, PermissionError):
                return True
            if isinstance(a, OSError) and getattr(a, "errno", None) in (errno.EACCES, errno.EPERM):
                return True
    except Exception:
        pass

    s = str(exc).lower()
    return ("permission denied" in s) or ("errno 13" in s) or ("eacces" in s)


def _asrl_devnode(resource_id: str) -> Optional[str]:
    """Extract /dev/... from an ASRL VISA resource string when present."""
    rid = str(resource_id or "")
    if not rid.startswith("ASRL") or "/dev/" not in rid:
        return None
    start = rid.find("/dev/")
    end = rid.find("::", start)
    if end == -1:
        end = len(rid)
    out = rid[start:end]
    return out or None


def _print_serial_permission_hint(devnode: str) -> None:
    devnode = str(devnode or "").strip()
    if not devnode:
        return
    # Debian/Ubuntu convention for /dev/tty* access.
    print(f"HINT: Permission denied opening {devnode}.")
    print("  Fix: add your user to the dialout group, then log out/in:")
    print("    sudo usermod -aG dialout $USER")
    print("  (You can also test quickly with: sudo chmod a+rw <device>, but udev will reset it)")

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
        # Higher-level helper (preferred)
        self.mmeter: BK5491B | None = None
        self.multi_meter_mode: int = 0
        self.multi_meter_range: int = 0
        self.mmeter_id: Optional[str] = None
        # Determined at runtime by the polling thread (see main.py).
        self.mmeter_fetch_cmd: Optional[str] = None

        # Expanded DMM state (used for CAN status/readback)
        self.mmeter_func: int = int(MmeterFunc.VDC)
        self.mmeter_autorange: bool = True
        self.mmeter_range_value: float = 0.0
        self.mmeter_nplc: float = 1.0
        self.mmeter_func2: int = int(MmeterFunc.VDC)
        self.mmeter_func2_enabled: bool = False
        self.mmeter_rel_enabled: bool = False
        self.mmeter_trig_source: int = 0  # 0=IMM,1=BUS,2=MAN
        # Poll holdoff after configuration changes (monotonic time)
        self.mmeter_pause_until: float = 0.0


        # SCPI command dialect for the multimeter.
        #
        # For the 2831E/5491B family, the documented / recommended dialect is
        # the classic tree rooted at :FUNCtion (plus :FUNCtion2 for secondary
        # display on newer firmware). We default to "func".
        try:
            self.mmeter_scpi_style: str = str(getattr(config, "MMETER_SCPI_STYLE", "func") or "func").strip().lower()
        except Exception:
            self.mmeter_scpi_style = "func"

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
        if not bool(getattr(config, "K1_ENABLE", True)):
            self.relay = _NullRelay(initial_drive)
            self.relay_backend = "disabled"
        elif not _is_raspberry_pi():
            # Avoid gpiozero pin-factory fallback noise on non-Pi hosts.
            print("WARNING: K1 GPIO unavailable (not a Raspberry Pi); running with a mock relay.")
            self.relay = _NullRelay(initial_drive)
            self.relay_backend = "mock"
        elif LED is None:
            print("WARNING: gpiozero unavailable; running with a mock relay.")
            self.relay = _NullRelay(initial_drive)
            self.relay_backend = "mock"
        else:
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
                # Typical causes: missing pin factories, insufficient permissions, etc.
                print(f"WARNING: K1 GPIO unavailable; running with a mock relay. ({e})")
                self.relay = _NullRelay(initial_drive)
                self.relay_backend = "mock"

    def _maybe_detect_mmeter_scpi_style(self) -> None:
        """One-time SCPI dialect detection for the 5491B.

        Some 5491/5492 command sets use :CONFigure (CONF:...) while others use
        :FUNCtion + per-subsystem trees. Sending the wrong style can make the
        meter display "BUS: BAD COMMAND".
        """

        style = str(getattr(self, "mmeter_scpi_style", "func") or "func").strip().lower()
        if style not in ("conf", "func", "auto"):
            style = "func"

        # If explicitly configured, honor it.
        if style != "auto":
            self.mmeter_scpi_style = style
            print(f"MMETER SCPI style: {self.mmeter_scpi_style}")
            return

        # Auto-detect
        helper = getattr(self, "mmeter", None)
        if helper is None:
            self.mmeter_scpi_style = "func"
            print(f"MMETER SCPI style: {self.mmeter_scpi_style} (default)")
            return

        # 1) Try FUNC-style query (classic SCPI tree) first.
        resp2 = ""
        try:
            resp2 = helper.query_line(":FUNCtion?", delay_s=0.05, read_lines=6)
        except Exception:
            resp2 = ""
        r2 = (resp2 or "").upper()
        if any(tok in r2 for tok in ("VOLT", "CURR", "RES", "FREQ", "PER", "DIO", "CONT")):
            self.mmeter_scpi_style = "func"
            print(f"MMETER SCPI style: {self.mmeter_scpi_style} (auto)")
            return

        # 2) As a fallback, try CONF-style query.
        # WARNING: Some firmware variants will complain on the front panel when
        # they receive an unknown command. Only attempt this when the user
        # explicitly requested auto detection.
        resp = ""
        try:
            resp = helper.query_line(":CONFigure:FUNCtion?", delay_s=0.05, read_lines=6)
        except Exception:
            resp = ""
        r = (resp or "").upper()
        if any(tok in r for tok in ("DCV", "ACV", "DCA", "ACA", "HZ", "RES", "DIOC", "NONE")):
            self.mmeter_scpi_style = "conf"
            print(f"MMETER SCPI style: {self.mmeter_scpi_style} (auto)")
            return

        # Fallback
        self.mmeter_scpi_style = "func"
        print(f"MMETER SCPI style: {self.mmeter_scpi_style} (auto default)")

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
        """Initialize the serial multimeter.

        Many USB-serial instruments will *echo* the command you send before
        replying with the actual IDN string. Also, some respond a beat later.
        We therefore read a few lines and skip obvious echoes.
        """
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

            # If device_discovery already identified the meter, avoid sending
            # another *IDN? immediately on boot unless verification is enabled.
            cached_idn = str(getattr(config, 'MULTI_METER_IDN', '') or '').strip()
            verify = bool(getattr(config, 'MULTI_METER_VERIFY_ON_STARTUP', False))
            if cached_idn and not verify:
                self.mmeter_id = cached_idn
                print(f"MULTI-METER ID: {self.mmeter_id}")
                self.multi_meter = mmeter
                try:
                    self.mmeter = BK5491B(mmeter, log_fn=print)
                except Exception:
                    self.mmeter = None

                # Clear any stale error queue entries so the front panel doesn't
                # keep showing a persistent BUS error after experimentation.
                try:
                    if bool(getattr(config, "MMETER_CLEAR_ERRORS_ON_STARTUP", True)) and self.mmeter is not None:
                        self.mmeter.drain_errors(log=True)
                except Exception:
                    pass

                # Optional SCPI dialect detection (one-time) to avoid sending
                # commands the meter doesn't understand (prevents "BUS: BAD COMMAND").
                self._maybe_detect_mmeter_scpi_style()
                return

            # Query IDN and tolerate command echo.
            try:
                mmeter.write(b"*IDN?\n")
                mmeter.flush()
            except Exception:
                pass

            # Give the device a moment; many respond ~10-100ms later.
            time.sleep(float(getattr(config, 'MULTI_METER_IDN_DELAY', 0.05)))

            idn: Optional[str] = None
            for _ in range(int(getattr(config, 'MULTI_METER_IDN_READ_LINES', 4))):
                raw = mmeter.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                # Ignore the common echo patterns.
                if line.upper().startswith("*IDN?"):
                    continue
                # Some devices include stray prompts; prefer lines that look like an IDN.
                if ("," in line) or ("multimeter" in line.lower()) or ("5491" in line.lower()):
                    idn = line
                    break
                # Fallback: accept the first non-empty non-echo line.
                if idn is None:
                    idn = line

            self.mmeter_id = idn
            print(f"MULTI-METER ID: {self.mmeter_id or 'Unknown'}")
            self.multi_meter = mmeter
            try:
                self.mmeter = BK5491B(mmeter, log_fn=print)
            except Exception:
                self.mmeter = None

            try:
                if bool(getattr(config, "MMETER_CLEAR_ERRORS_ON_STARTUP", True)) and self.mmeter is not None:
                    self.mmeter.drain_errors(log=True)
            except Exception:
                pass

            self._maybe_detect_mmeter_scpi_style()
        except (serial.SerialException, IOError, OSError) as e:
            print(f"Failed to communicate with multi-meter: {e}")
            if _is_permission_denied(e):
                _print_serial_permission_hint(str(getattr(config, "MULTI_METER_PATH", "") or ""))
            self.multi_meter = None
            self.mmeter = None

    def _initialize_visa_devices(self) -> None:
        """Initializes both E-Load and AFG via PyVISA."""
        try:
            self.resource_manager = pyvisa.ResourceManager()

            # --- 1. E-LOAD (Scan for USBTMC / match pattern) ---
            try:
                available_resources = list(self.resource_manager.list_resources())

                # CRITICAL SAFETY: Never probe ASRL resources when looking for an E-load.
                # Many unrelated USB-serial devices show up as ASRL/dev/ttyUSB*::INSTR
                # (including the 5491B DMM). Touching them via VISA can make them beep
                # "bus command error" and stop responding. The E-load is USBTMC and
                # should appear as a USB* resource.
                usb_resources = [r for r in available_resources if str(r).startswith("USB")]
                print(f"Scanning for E-Load in (USB only): {usb_resources}")

                eload_pat = str(getattr(config, "ELOAD_VISA_ID", "") or "").strip()
                eload_hints = [
                    t.strip().lower()
                    for t in str(getattr(config, "AUTO_DETECT_ELOAD_IDN_HINTS", "") or "").split(",")
                    if t.strip()
                ]

                def _is_specific_resource(pat: str) -> bool:
                    if not pat:
                        return False
                    # If there are any glob metacharacters, treat as a pattern.
                    return not any(ch in pat for ch in ("*", "?", "["))

                eload_specific = _is_specific_resource(eload_pat)

                if eload_specific:
                    # If config points at a specific VISA resource (e.g. from autodetect),
                    # trust it and connect directly. Do NOT require IDN hints here.
                    # Try the specific resource first, even if list_resources() is empty.
                    candidates = [eload_pat] if eload_pat else []
                    # Then fall back to scanning any other USB resources we can see.
                    if usb_resources:
                        candidates += [r for r in usb_resources if r != eload_pat]
                    print(f"E-Load target (direct): {eload_pat}")
                else:
                    # Pattern scan across all USB resources.
                    candidates = [r for r in usb_resources if (not eload_pat or fnmatch.fnmatch(r, eload_pat))]
                    print(f"E-Load scan: pattern={eload_pat or '*'} hints={eload_hints or 'none'}")

                for resource_id in candidates:
                    try:
                        dev = self.resource_manager.open_resource(resource_id)
                        # Bound I/O time so a slow/missing instrument doesn't stall controls.
                        try:
                            dev.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                        except Exception:
                            pass
                        # USBTMC devices usually speak SCPI with newline termination.
                        try:
                            dev.read_termination = "\n"
                            dev.write_termination = "\n"
                        except Exception:
                            pass

                        dev_id = str(dev.query("*IDN?")).strip()

                        # Only enforce hints when doing a broad scan.
                        if (not eload_specific) and eload_hints:
                            low = (dev_id or "").lower()
                            if not any(h in low for h in eload_hints):
                                print(f"E-Load candidate rejected: {resource_id} -> {dev_id}")
                                try:
                                    dev.close()
                                except Exception:
                                    pass
                                continue

                        print(f"E-LOAD FOUND: {dev_id} @ {resource_id}")
                        # Keep these best-effort; some loads dislike *RST.
                        try:
                            dev.write("SYST:CLE")
                        except Exception:
                            pass
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
                # Bound I/O time so polling doesn't block control writes for long.
                try:
                    afg_dev.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                except Exception:
                    pass
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
                if _is_permission_denied(e):
                    dev = _asrl_devnode(str(getattr(config, "AFG_VISA_ID", "") or ""))
                    if dev:
                        _print_serial_permission_hint(dev)

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
            if _is_permission_denied(e):
                _print_serial_permission_hint(port)
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
