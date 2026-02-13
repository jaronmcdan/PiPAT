# Overview

ROI (Remote Operational Equipment) is a Raspberry Pi focused bridge between
CAN control traffic and lab/test instruments.

ROI does three things:

1. Receives control frames on CAN.
2. Applies commands to connected instruments.
3. Publishes readback/status frames back on CAN.

## Runtime Model

ROI separates responsibilities across loops/threads:

- CAN RX thread reads frames and updates freshness state.
- Device command loop applies control frames to hardware.
- Polling loop reads instrument measurements/status.
- CAN TX thread publishes readback frames at configured cadence.

```text
CAN bus --> ROI (Pi)
          |- RX thread: read control frames
          |- command loop: write to instruments
          |- poll loop: read instruments
          `- TX thread: publish readback frames
```

## Scope

ROI is not:

- a general SCADA framework
- a PLC replacement
- a GUI-first product (dashboard is operational diagnostics)
