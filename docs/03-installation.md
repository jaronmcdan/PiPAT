# Installation

ROI supports two install styles:

- Raspberry Pi appliance install (`/opt/roi` + optional systemd)
- Developer install in a checkout (`pip install -e .`)

## Option A: Raspberry Pi appliance install (recommended)

From a fresh clone on the Pi:

```bash
git clone <your-repo-url> roi
cd roi
sudo bash scripts/pi_install.sh --easy
```

`--easy` does:

- installs OS deps with apt
- installs USBTMC udev rules
- adds invoking user to `dialout` and `plugdev`
- creates venv at `/opt/roi/.venv`
- installs ROI into that venv

### Configure

```bash
sudo nano /etc/roi/roi.env
```

See [Configuration](04-configuration.md).

### Run interactively

```bash
sudo /opt/roi/.venv/bin/roi
```

### Install as a service

```bash
sudo bash /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
```

## Option B: Developer install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
roi
```

## Build a distributable tarball

```bash
./scripts/make_pi_dist.sh
# produces dist/roi-<sha-or-timestamp>.tar.gz
```

Copy the tarball to the Pi, extract it, then run:

```bash
sudo bash scripts/pi_install.sh --easy
```
