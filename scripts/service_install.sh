#!/usr/bin/env bash
set -euo pipefail

# Install/enable the systemd service that runs ROI.
# This is intentionally split out from pi_install.sh so you can:
#   - prep the Pi image without turning on an always-on service yet
#   - or install the service on a different host pointing at an existing PREFIX

PREFIX="/opt/roi"
SERVICE_NAME="roi"
ENABLE="0"
START="0"

usage() {
  cat <<EOF
Usage: sudo $0 [--prefix /opt/roi] [--service-name roi] [--enable] [--start]

Installs a systemd unit that runs:
  <prefix>/.venv/bin/roi

Options:
  --prefix <path>         Install prefix used by pi_install.sh (default: /opt/roi)
  --service-name <name>   systemd unit name (default: roi)
  --enable                Enable at boot (systemctl enable)
  --start                 Start now (systemctl start)

Examples:
  sudo $0 --prefix /opt/roi --enable --start
  sudo $0 --service-name roi-test --start
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      PREFIX="$2"; shift 2;;
    --service-name)
      SERVICE_NAME="$2"; shift 2;;
    --enable)
      ENABLE="1"; shift;;
    --start)
      START="1"; shift;;
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

if [[ ! -d "$PREFIX" ]]; then
  echo "[ROI] ERROR: PREFIX does not exist: $PREFIX" >&2
  echo "[ROI] Run scripts/pi_install.sh first (or point --prefix to the installed location)." >&2
  exit 1
fi

if [[ ! -x "$PREFIX/.venv/bin/roi" ]]; then
  echo "[ROI] WARNING: roi entrypoint not found at $PREFIX/.venv/bin/roi" >&2
  echo "[ROI] The service may fail to start until you run scripts/pi_install.sh." >&2
fi

TEMPLATE=""
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$PREFIX/deploy/systemd/roi.service" ]]; then
  TEMPLATE="$PREFIX/deploy/systemd/roi.service"
elif [[ -f "$SRC_DIR/deploy/systemd/roi.service" ]]; then
  TEMPLATE="$SRC_DIR/deploy/systemd/roi.service"
else
  echo "[ROI] ERROR: cannot find deploy/systemd/roi.service template." >&2
  exit 1
fi

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

echo "[ROI] Installing systemd unit: $UNIT_PATH"

# Replace hard-coded /opt/roi with the requested PREFIX.
REPL="$PREFIX"
REPL_ESCAPED="$(printf '%s' "$REPL" | sed -e 's/[\\/&|]/\\&/g')"
sed -e "s|/opt/roi|${REPL_ESCAPED}|g" "$TEMPLATE" >"$UNIT_PATH"

systemctl daemon-reload

if [[ "$ENABLE" == "1" ]]; then
  echo "[ROI] Enabling service: $SERVICE_NAME"
  systemctl enable "$SERVICE_NAME"
fi

if [[ "$START" == "1" ]]; then
  echo "[ROI] Starting service: $SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  systemctl status "$SERVICE_NAME" --no-pager || true
else
  echo "[ROI] Service installed but not started. Start it with:"
  echo "  sudo systemctl start $SERVICE_NAME"
fi

echo
echo "[ROI] Done. Logs: sudo journalctl -u $SERVICE_NAME -f"
