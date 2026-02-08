# Overview

ROI (**R**emote **O**perational **I**quipment) is a Raspberry Pi–focused bridge between a CAN bus and lab / test instruments.

It does three main things:

1. **Receives control frames on CAN** (setpoints, mode changes, on/off, relay control).
2. **Applies those commands to instruments** (multimeter, electronic load, AFG, MrSignal PSU, K1 relay).
3. **Publishes readback frames back on CAN** at a fixed rate (measurements and status).

## Architecture

At a high level:

- A CAN RX thread reads CAN frames and updates “last seen” timestamps.
- A device command loop consumes only the control frames it cares about and calls into instrument drivers.
- A polling loop periodically queries instruments for measurements/status.
- A CAN TX thread publishes readback frames at `CAN_TX_PERIOD_MS`.

```
CAN bus  --->  ROI (Pi)
            ├─ RX thread: read control frames
            ├─ Command loop: write to instruments
            ├─ Poll loop: read instruments
            └─ TX thread: publish readback frames
```

## What ROI is not

- Not a general SCADA framework.
- Not a PLC replacement.
- Not a GUI-first application (the Rich dashboard is primarily a debug/status console).

## Naming note

Older internal drafts referenced a different project name. This repository is **ROI** and the docs/scripts now use ROI consistently.
