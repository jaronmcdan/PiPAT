\
#!/usr/bin/env bash
set -euo pipefail

PREFIX="/opt/roi"
ENABLE_SERVICE="0"

usage() {
  cat <<EOF
Usage: sudo $0 [--prefix /opt/roi] [--enable-service]

Installs this project onto a Raspberry Pi:
- Copies files into PREFIX
- Creates venv at PREFIX/.venv
- Installs requirements
- Optionally installs and enables systemd service 'roi'
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      PREFIX="$2"; shift 2;;
    --enable-service)
      ENABLE_SERVICE="1"; shift;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage; exit 2;;
  esac
done

if [[ "$(id -u)" != "0" ]]; then
  echo "Please run as root (use sudo)." >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
python3 -m venv "$PREFIX/.venv"
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

# systemd unit
if [[ -f "$PREFIX/systemd/roi.service" ]]; then
  echo "[ROI] Installing systemd unit"
  cp "$PREFIX/systemd/roi.service" /etc/systemd/system/roi.service
  systemctl daemon-reload
fi

if [[ "$ENABLE_SERVICE" == "1" ]]; then
  echo "[ROI] Enabling and starting service"
  systemctl enable roi
  systemctl restart roi
  systemctl status roi --no-pager || true
else
  echo "[ROI] Service not enabled. To enable later:"
  echo "  sudo systemctl enable roi"
  echo "  sudo systemctl start roi"
fi

echo
echo "[ROI] Done."
echo "Edit config.py (or /etc/roi/roi.env if you later adopt env-based config)."
echo "Logs: sudo journalctl -u roi -f"
