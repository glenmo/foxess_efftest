#!/usr/bin/env bash
#
# Install the SMA WebBox live Modbus probe as a systemd service.
#
# Usage:
#   sudo bash install_sma_webbox_probe.sh
#   sudo bash install_sma_webbox_probe.sh --port 8910
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="sma-webbox-probe.service"
SERVICE_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

HOST="0.0.0.0"
PORT="8910"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">> App dir: $APP_DIR"
echo ">> Listen:  $HOST:$PORT"

if [[ ! -d "$APP_DIR/venv" ]]; then
  echo ">> Creating venv"
  "$PYTHON_BIN" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R "$SERVICE_USER" "$APP_DIR/venv" || true

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
echo ">> Writing $UNIT_PATH"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=SMA WebBox live Modbus probe
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/sma_webbox_probe.py \\
    --web-host ${HOST} \\
    --web-port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager status "$SERVICE_NAME" | head -10 || true

HN="$(hostname -f 2>/dev/null || hostname)"
if [[ "$HN" == *.* ]]; then
  URL="http://${HN}:${PORT}/"
else
  URL="http://${HN}.local:${PORT}/"
fi
echo ">> Open: $URL"
