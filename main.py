#!/home/j/roi/bin/python
# main.py

import can
import time
import threading
import struct
import subprocess
import sys

# --- Local Modules ---
import config
from hardware import HardwareManager
from dashboard import build_dashboard, console, HAVE_RICH
from rich.live import Live

# --- CAN Helper Functions ---
def setup_can_interface(channel: str, bitrate: int):
    try:
        subprocess.run(["sudo", "ip", "link", "set", channel, "up", "type", "can", f"bitrate", f"{bitrate}"], check=True)
        return can.interface.Bus(interface='socketcan', channel=channel, bitrate=bitrate)
    except Exception:
        return None

def shutdown_can_interface(channel: str):
    subprocess.run(["sudo", "ip", "link", "set", channel, "down"], check=False)

def receive_can_messages(cbus, hardware: HardwareManager, stop_event: threading.Event):
    """Background thread to process incoming CAN commands."""
    (console.log if HAVE_RICH else print)("Receiver thread started.")
    
    SHAPE_MAP = {0: "SIN", 1: "SQU", 2: "RAMP"}

    while not stop_event.is_set():
        message = cbus.recv(timeout=1.0)
        if not message:
            continue

        # Relay control
        if message.arbitration_id == config.RLY_CTRL_ID:
            bit0 = (message.data[0] & 0x01)
            # Protocol semantics can vary: some send 1=ON, others send 0=ON.
            dut_power_on = (bit0 == 0x01) if config.RELAY_CAN_ON_IS_1 else (bit0 == 0x00)
            hardware.set_dut_power(dut_power_on)

            # OPTION 2: Force Reset (Use this if '1' should always reboot the device)
            # if should_be_on:
            #     hardware.relay.off()
            #     time.sleep(1.0) # Wait 1 second for power to drain
            #     hardware.relay.on()
            
            continue

        # AFG Control (Primary)
        if message.arbitration_id == config.AFG_CTRL_ID and hardware.afg:
            enable = (message.data[0] != 0)
            shape_idx = message.data[1]
            freq = struct.unpack('<I', bytes(message.data[2:6]))[0]
            ampl_mV = struct.unpack('<H', bytes(message.data[6:8]))[0]
            ampl_V = ampl_mV / 1000.0

            try:
                with hardware.afg_lock:
                    if hardware.afg_output != enable:
                        hardware.afg.write(f"SOUR1:OUTP {'ON' if enable else 'OFF'}")
                        hardware.afg_output = enable
                    if hardware.afg_shape != shape_idx:
                        shape_str = SHAPE_MAP.get(shape_idx, "SIN")
                        hardware.afg.write(f"SOUR1:FUNC {shape_str}")
                        hardware.afg_shape = shape_idx
                    if hardware.afg_freq != freq:
                        hardware.afg.write(f"SOUR1:FREQ {freq}")
                        hardware.afg_freq = freq
                    if hardware.afg_ampl != ampl_mV:
                        hardware.afg.write(f"SOUR1:AMPL {ampl_V}")
                        hardware.afg_ampl = ampl_mV
            except Exception as e:
                (console.log if HAVE_RICH else print)(f"AFG Control Error: {e}")
            continue

        # AFG Control (Extended)
        if message.arbitration_id == config.AFG_CTRL_EXT_ID and hardware.afg:
            offset_mV = struct.unpack('<h', bytes(message.data[0:2]))[0]
            offset_V = offset_mV / 1000.0
            duty_cycle = message.data[2] if message.dlc > 2 else 50
            
            if duty_cycle < 1: duty_cycle = 1
            if duty_cycle > 99: duty_cycle = 99

            try:
                with hardware.afg_lock:
                    if hardware.afg_offset != offset_mV:
                        hardware.afg.write(f"SOUR1:VOLT:OFFS {offset_V}")
                        hardware.afg_offset = offset_mV
                    if hardware.afg_duty != duty_cycle:
                        hardware.afg.write(f"SOUR1:SQU:DCYC {duty_cycle}")
                        hardware.afg_duty = duty_cycle
            except Exception as e:
                (console.log if HAVE_RICH else print)(f"AFG Ext Error: {e}")
            continue

        # Multimeter control
        # ... inside receive_can_messages ...
        if message.arbitration_id == config.MMETER_CTRL_ID:
            print(f"DEBUG: Received Meter CMD. Mode: {message.data[0]}") # <--- ADD THIS
            meter_mode = message.data[0]
            meter_range = message.data[1]
            
            # Check why it might be skipping
            if not hardware.multi_meter:
                print("DEBUG: ERROR - Multimeter not connected!") # <--- ADD THIS
            if hardware.multi_meter and (hardware.multi_meter_mode != meter_mode):
                with hardware.mmeter_lock:
                    if meter_mode == 0:
                        hardware.multi_meter.write(b'FUNC VOLT:DC\n')
                    elif meter_mode == 1:
                        hardware.multi_meter.write(b'FUNC CURR:DC\n')
                        time.sleep(0.5)
                        hardware.multi_meter.write(b'CURR:DC:RANG 5\n')
                    hardware.multi_meter_mode = meter_mode
            hardware.multi_meter_range = meter_range
            continue

        # E-load control
        if message.arbitration_id == config.LOAD_CTRL_ID and hardware.e_load:
            first_byte = message.data[0]
            new_enable = 1 if first_byte & 0x0C == 0x04 else 0
            new_mode = 1 if first_byte & 0x30 == 0x10 else 0
            new_short = 1 if first_byte & 0xC0 == 0x40 else 0
            
            if hardware.e_load_enabled != new_enable:
                hardware.e_load_enabled = new_enable
                with hardware.eload_lock:
                    hardware.e_load.write("INP ON" if new_enable else "INP OFF")
            
            if hardware.e_load_mode != new_mode:
                hardware.e_load_mode = new_mode
                with hardware.eload_lock:
                    hardware.e_load.write("FUNC RES" if new_mode else "FUNC CURR")

            if hardware.e_load_short != new_short:
                hardware.e_load_short = new_short
                with hardware.eload_lock:
                    hardware.e_load.write("INP:SHOR ON" if new_short else "INP:SHOR OFF")

            val_c = (message.data[3] << 8) | message.data[2]
            if hardware.e_load_csetting != val_c:
                hardware.e_load_csetting = val_c
                with hardware.eload_lock:
                    hardware.e_load.write(f"CURR {val_c/1000}")
            
            val_r = (message.data[5] << 8) | message.data[4]
            if hardware.e_load_rsetting != val_r:
                hardware.e_load_rsetting = val_r
                with hardware.eload_lock:
                    hardware.e_load.write(f"RES {val_r/1000}")

def main():
    hardware = HardwareManager()
    stop_event = threading.Event()
    
    # measurement vars
    meter_current_mA = 0
    load_volts_mV = 0
    load_current_mA = 0

    # status vars
    load_stat_func, load_stat_curr, load_stat_imp, load_stat_res, load_stat_short = "","","","",""
    afg_freq_str, afg_ampl_str, afg_out_str, afg_shape_str = "","","",""
    afg_offset_str, afg_duty_str = "0", "50"

    last_status_poll = 0.0
    STATUS_POLL_PERIOD = 1.0

    try:
        hardware.initialize_devices()
        cbus = setup_can_interface(config.CAN_CHANNEL, config.CAN_BITRATE)
        if not cbus:
            print("CAN Init Failed.") 
            return

        receiver_thread = threading.Thread(target=receive_can_messages, args=(cbus, hardware, stop_event), daemon=True)
        receiver_thread.start()

        # Enter Dashboard Loop
        with Live(console=console, screen=True, refresh_per_second=10) as live:
            while True:
                now = time.time()
                
                # 1. Multimeter Read
                if hardware.multi_meter:
                    try:
                        with hardware.mmeter_lock:
                            hardware.multi_meter.write(b'FETC?\n')
                            resp = hardware.multi_meter.readline().decode().strip()
                        if resp:
                            val = float(resp)
                            meter_current_mA = int(round(val * 1000))
                            msg = can.Message(arbitration_id=config.MMETER_READ_ID, 
                                              data=list(meter_current_mA.to_bytes(2, 'little')) + [0]*6, 
                                              is_extended_id=True)
                            cbus.send(msg)
                    except Exception: pass

                # 2. E-Load Meas
                if hardware.e_load:
                    try:
                        with hardware.eload_lock:
                            v_str = hardware.e_load.query("MEAS:VOLT?").strip()
                            i_str = hardware.e_load.query("MEAS:CURR?").strip()
                        if v_str and i_str:
                            load_volts_mV = int(float(v_str)*1000)
                            load_current_mA = int(float(i_str)*1000)
                            data = list(load_volts_mV.to_bytes(2, 'little')) + \
                                   list(load_current_mA.to_bytes(2, 'little')) + [0]*4
                            msg = can.Message(arbitration_id=config.ELOAD_READ_ID, data=data, is_extended_id=True)
                            cbus.send(msg)
                    except Exception: pass

                # 3. Status Poll (Low Priority)
                if now - last_status_poll >= STATUS_POLL_PERIOD:
                    last_status_poll = now
                    
                    if hardware.e_load:
                        try:
                            with hardware.eload_lock:
                                load_stat_func = hardware.e_load.query("FUNC?").strip()
                                load_stat_curr = hardware.e_load.query("CURR?").strip()
                                load_stat_imp = hardware.e_load.query("INP?").strip()
                                load_stat_res = hardware.e_load.query("RES?").strip()
                        except Exception: pass

                    if hardware.afg:
                        try:
                            with hardware.afg_lock:
                                afg_freq_str = hardware.afg.query("SOUR1:FREQ?").strip()
                                afg_ampl_str = hardware.afg.query("SOUR1:AMPL?").strip()
                                afg_out_str = hardware.afg.query("SOUR1:OUTP?").strip()
                                
                                is_actually_on = (afg_out_str.strip().upper() in ['ON', '1'])
                                if hardware.afg_output != is_actually_on:
                                    hardware.afg_output = is_actually_on

                                afg_shape_str = hardware.afg.query("SOUR1:FUNC?").strip()
                                afg_offset_str = hardware.afg.query("SOUR1:VOLT:OFFS?").strip()
                                afg_duty_str = hardware.afg.query("SOUR1:SQU:DCYC?").strip()
                                
                                if afg_offset_str and afg_duty_str:
                                    off_mv = int(float(afg_offset_str) * 1000)
                                    duty_pct = int(float(afg_duty_str))
                                    payload = bytearray(struct.pack('<h', off_mv))
                                    payload.append(duty_pct & 0xFF)
                                    payload.extend([0]*5)
                                    msg = can.Message(arbitration_id=config.AFG_READ_EXT_ID, data=payload, is_extended_id=True)
                                    cbus.send(msg)
                        except Exception: pass

                # 4. Update UI
                renderable = build_dashboard(
                    hardware,
                    meter_current_mA=meter_current_mA,
                    load_volts_mV=load_volts_mV,
                    load_current_mA=load_current_mA,
                    load_stat_func=load_stat_func,
                    load_stat_curr=load_stat_curr,
                    load_stat_res=load_stat_res,
                    load_stat_imp=load_stat_imp,
                    load_stat_short=load_stat_short,
                    afg_freq_read=afg_freq_str,
                    afg_ampl_read=afg_ampl_str,
                    afg_offset_read=afg_offset_str,
                    afg_duty_read=afg_duty_str,
                    afg_out_read=afg_out_str,
                    afg_shape_read=afg_shape_str,
                    can_channel=config.CAN_CHANNEL,
                    can_bitrate=config.CAN_BITRATE,
                    status_poll_period=STATUS_POLL_PERIOD,
                )
                live.update(renderable)
                time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        # Clean up
        try:
             if 'receiver_thread' in locals(): receiver_thread.join()
             if 'cbus' in locals() and cbus: cbus.shutdown()
             shutdown_can_interface(config.CAN_CHANNEL)
             hardware.close_devices()
        except Exception:
             pass

if __name__ == "__main__":
    main()