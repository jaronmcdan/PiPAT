# Instruments

ROI is designed to tolerate partial hardware. If a device is not connected, ROI should keep running and simply show it as unavailable.

## Multimeter (BK 2831E / 5491B style bench DMM)

Connection:
- USB-serial device (e.g., `/dev/ttyUSB0` or `/dev/serial/by-id/...`)
- Default baud: 38400

Config:
```bash
MULTI_METER_PATH=/dev/ttyUSB0
MULTI_METER_BAUD=38400
```

Notes:
- ROI supports a conservative startup to avoid “BUS” errors.
- If you see frequent errors, consider `AUTO_DETECT_BYID_ONLY=1` and use `/dev/serial/by-id/...`.

Diagnostics:
```bash
roi-mmter-diag
```

## Electronic Load (VISA / USBTMC)

Config:
```bash
VISA_BACKEND=@py
ELOAD_VISA_ID=USB0::...::INSTR
```

Permissions:
- On Raspberry Pi installs, `scripts/pi_install.sh --install-udev-rules` installs udev rules for common BK 8600-series VID/PID.
- Alternatively, run ROI as root.

Diagnostics:
```bash
roi-visa-diag
```

## AFG / Function Generator (VISA)

Config:
```bash
VISA_BACKEND=@py
AFG_VISA_ID=USB0::...::INSTR
# or ASRL/dev/ttyACM0::INSTR
```

If you use an ASRL resource:
- ROI’s auto-detect can probe ASRL resources; disable probing on unknown ports using `AUTO_DETECT_BYID_ONLY=1`.

## K1 relay (USB-serial relay controller)

Config:
```bash
K1_BACKEND=serial
K1_SERIAL_PORT=/dev/serial/by-id/<your-arduino>
K1_SERIAL_BAUD=9600
K1_SERIAL_RELAY_INDEX=1
```

## MrSignal / LANYI MR2.x Modbus PSU

Config:
```bash
MRSIGNAL_ENABLE=1
MRSIGNAL_PORT=/dev/ttyUSB1
MRSIGNAL_BAUD=9600
MRSIGNAL_SLAVE_ID=1
```

Notes:
- ROI applies safety clamps (`MRSIGNAL_MAX_V`, `MRSIGNAL_MAX_MA`).
- Float byteorder can be overridden if your device differs.
