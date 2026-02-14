# Project Structure

ROI uses a src-layout Python package and keeps deploy artifacts separate from
runtime code.

```text
.
|- src/roi/                 # application package
|  |- app.py                # main entrypoint (`roi`)
|  |- config.py             # env-driven settings
|  |- can/                  # CAN backends and TX/RX logic
|  |- core/                 # hardware manager, discovery, diagnostics helpers
|  |- devices/              # instrument drivers (SCPI/Modbus/USBTMC)
|  |- tools/                # diagnostic CLIs
|  `- assets/               # packaged data
|- deploy/
|  |- env/roi.env.example   # sample /etc/roi/roi.env
|  `- systemd/roi.service   # service template
|- scripts/                 # Pi install/build helpers
|- docs/                    # setup + operations docs
`- tests/                   # unit tests
```

## Why this layout

- reduces accidental import confusion in local checkouts
- cleanly separates deploy templates from application code
- keeps setup and ops docs in one ordered place
