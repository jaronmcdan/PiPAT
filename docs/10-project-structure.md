# Project structure

ROI uses a “src layout” (recommended for Python packages) and separates deployment artifacts from source.

```
.
├─ src/roi/                 # Python package
│  ├─ app.py                # main application (entrypoint `roi`)
│  ├─ config.py             # env-driven configuration
│  ├─ can/                  # CAN backends + TX/RX helpers
│  ├─ core/                 # hardware manager, discovery, PAT helpers
│  ├─ devices/              # instrument drivers (SCPI, Modbus, USBTMC)
│  ├─ tools/                # small diagnostic CLIs
│  └─ assets/               # packaged data (PAT.dbc)
├─ deploy/
│  ├─ env/roi.env.example   # example /etc/roi/roi.env
│  └─ systemd/roi.service   # systemd unit template
├─ scripts/                 # install/build helpers (Pi)
├─ docs/                    # documentation (in setup order)
└─ tests/                   # unit tests
```

## Why this layout?

- **Avoids import confusion**: src-layout prevents accidentally importing the local checkout when you think you’re importing the installed package.
- **Cleaner deployment**: `deploy/` is explicitly “things we copy to /etc or /etc/systemd”.
- **Docs are ordered**: `docs/` starts at overview and ends at troubleshooting/development.
