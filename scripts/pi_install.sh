#!/usr/bin/env bash
set -euo pipefail

PREFIX="/opt/roi"
INSTALL_OS_DEPS="0"
INSTALL_UDEV_RULES="0"
ADD_USER_GROUPS="0"
VENV_SYSTEM_SITE_PACKAGES="0"

EASY="0"

usage() {
  cat <<EOF
Usage: sudo $0 [--prefix /opt/roi]

Optional:
  --easy                  Do the "make it work" path (os deps + udev + user groups)
  --install-os-deps        Install recommended apt packages (python3-venv, can-utils, libusb, usbutils, lgpio)
  --install-udev-rules     Install udev rules for USBTMC instruments (E-load)
  --add-user-groups        Add the invoking user to dialout/plugdev/gpio (for interactive runs)
  --venv-system-site-packages  Create the venv with --system-site-packages (needed to use apt-installed lgpio)

Installs this project onto a Raspberry Pi:
- Copies files into PREFIX
- Creates venv at PREFIX/.venv
- Installs requirements
- Leaves systemd service install to scripts/service_install.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --easy)
      EASY="1"; shift;;
    --prefix)
      PREFIX="$2"; shift 2;;
    --install-os-deps)
      INSTALL_OS_DEPS="1"; shift;;
    --install-udev-rules)
      INSTALL_UDEV_RULES="1"; shift;;
    --add-user-groups)
      ADD_USER_GROUPS="1"; shift;;
    --venv-system-site-packages)
      VENV_SYSTEM_SITE_PACKAGES="1"; shift;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage; exit 2;;
  esac
done

if [[ "$EASY" == "1" ]]; then
  INSTALL_OS_DEPS="1"
  INSTALL_UDEV_RULES="1"
  ADD_USER_GROUPS="1"
  VENV_SYSTEM_SITE_PACKAGES="1"
fi

if [[ "$(id -u)" != "0" ]]; then
  echo "Please run as root (use sudo)." >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$INSTALL_OS_DEPS" == "1" ]]; then
  echo "[ROI] Installing OS dependencies via apt"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y \
      python3 python3-venv python3-pip python3-dev \
      can-utils \
      libusb-1.0-0 \
      usbutils \
      python3-lgpio \
      rsync
  else
    echo "[ROI] WARNING: apt-get not found; skipping OS deps." >&2
  fi
fi

if [[ "$INSTALL_UDEV_RULES" == "1" ]]; then
  echo "[ROI] Installing udev rules for USBTMC instruments (E-load)"
  mkdir -p /etc/udev/rules.d

  # BK Precision 8600 series (VID:PID 2ec7:8800) - allow both libusb (pyvisa-py)
  # and /dev/usbtmc* kernel driver access.
  cat >/etc/udev/rules.d/99-roi-usbtmc.rules <<'EOF'
# ROI / PiPAT instrument access

# BK Precision 8600-series Electronic Load (USBTMC)
# - "usb" rule covers libusb access (/dev/bus/usb/..)
# - "usbtmc" rule covers kernel driver node (/dev/usbtmc*)
SUBSYSTEM=="usb", ATTR{idVendor}=="2ec7", ATTR{idProduct}=="8800", MODE:="0666"
SUBSYSTEM=="usbtmc", ATTRS{idVendor}=="2ec7", ATTRS{idProduct}=="8800", MODE:="0666", GROUP:="plugdev"
EOF

  udevadm control --reload-rules || true
  udevadm trigger || true

  # Best-effort: ensure the kernel driver exists (enables /dev/usbtmc* fallback)
  modprobe usbtmc 2>/dev/null || true
  mkdir -p /etc/modules-load.d
  if [[ ! -f /etc/modules-load.d/usbtmc.conf ]]; then
    echo usbtmc >/etc/modules-load.d/usbtmc.conf
  fi

  echo "[ROI] NOTE: If the E-load was already plugged in, unplug/replug the USB cable now."
fi

if [[ "$ADD_USER_GROUPS" == "1" ]]; then
  # When invoked via sudo, SUDO_USER is the original user.
  TARGET_USER="${SUDO_USER:-}"
  if [[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]]; then
    echo "[ROI] Adding $TARGET_USER to groups: dialout plugdev gpio"
    usermod -aG dialout,plugdev,gpio "$TARGET_USER" || true
    echo "[ROI] NOTE: You may need to log out/in for group changes to take effect."
  else
    echo "[ROI] Skipping user group changes (no SUDO_USER)"
  fi
fi

echo "[ROI] Installing to: $PREFIX"
mkdir -p "$PREFIX"
rsync -a --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "venv" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".pytest_cache" \
  "$SRC_DIR/" "$PREFIX/"

echo "[ROI] Ensuring venv at $PREFIX/.venv"
if [[ "$VENV_SYSTEM_SITE_PACKAGES" == "1" ]]; then
  python3 -m venv --system-site-packages "$PREFIX/.venv"
else
  python3 -m venv "$PREFIX/.venv"
fi
"$PREFIX/.venv/bin/pip" install -U pip

if [[ -f "$PREFIX/requirements.txt" ]]; then
  "$PREFIX/.venv/bin/pip" install -r "$PREFIX/requirements.txt"
else
  echo "[ROI] WARNING: requirements.txt missing; installing minimal deps"
  "$PREFIX/.venv/bin/pip" install python-can pyserial pyvisa pyvisa-py pyusb gpiozero rich
fi

# Env dir
mkdir -p /etc/roi
if [[ ! -f /etc/roi/roi.env ]]; then
  echo "[ROI] Writing /etc/roi/roi.env (edit for per-Pi overrides)"
  cp -n "$PREFIX/roi.env.example" /etc/roi/roi.env || true
fi

echo
echo "[ROI] Done."
echo "Edit /etc/roi/roi.env for per-Pi overrides (config.py provides defaults)."
echo "Run: sudo $PREFIX/.venv/bin/python $PREFIX/main.py"
echo "(Optional service) sudo ./scripts/service_install.sh --prefix $PREFIX --enable --start"
