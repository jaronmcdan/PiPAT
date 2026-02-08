# Installation

ROI supports two installation styles:

- “**Appliance**” style on a Raspberry Pi (`/opt/roi` + systemd)
- “**Developer**” style in a checkout (`pip install -e .`)

## Option A: Raspberry Pi appliance install (recommended)

From a fresh clone on the Pi:

```bash
git clone <your-repo-url> roi
cd roi
sudo ./scripts/pi_install.sh --easy
```

What `--easy` does:

- installs OS deps (apt)
- installs USBTMC udev rules (for E-load access)
- adds your user to `dialout` / `plugdev` groups
- creates a venv under `/opt/roi/.venv`
- installs ROI into that venv

### Configure

Edit the env file:

```bash
sudo nano /etc/roi/roi.env
```

(See [Configuration](04-configuration.md).)

### Run interactively

```bash
sudo /opt/roi/.venv/bin/roi
```

### Install as a service

```bash
sudo /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
```

## Option B: Developer install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
roi
```

## Building a distributable tarball

For copying to multiple Pis without git:

```bash
./scripts/make_pi_dist.sh
# produces dist/roi-<sha-or-timestamp>.tar.gz
```

Copy the tarball to the Pi, extract, then run `sudo ./scripts/pi_install.sh --easy`.
