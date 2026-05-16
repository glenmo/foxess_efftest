#!/usr/bin/env bash
#
# Install the Fox H3 efficiency-test dashboard as a systemd service.
#
# Usage:
#   sudo bash install.sh                                  # defaults — upstream localhost:5000, port 8900
#   sudo bash install.sh --battery-capacity-ah 120        # enable Coulombic SOC
#   sudo bash install.sh --upstream http://desky.local:5000 --port 8900 --battery-capacity-ah 120
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="foxess-efftest.service"
SERVICE_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Defaults
HOST="0.0.0.0"
PORT="8900"
UPSTREAM="http://localhost:5000"
CSV_DIR="${APP_DIR}/data"
SITE_NAME="Fox H3 Efficiency Test"
BATTERY_CAPACITY_AH="0"          # 0 = disable Coulombic SOC; set per battery

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)                  HOST="$2";                 shift 2 ;;
    --port)                  PORT="$2";                 shift 2 ;;
    --upstream)              UPSTREAM="$2";             shift 2 ;;
    --csv-dir)               CSV_DIR="$2";              shift 2 ;;
    --site-name)             SITE_NAME="$2";            shift 2 ;;
    --battery-capacity-ah)   BATTERY_CAPACITY_AH="$2";  shift 2 ;;
    --user)                  SERVICE_USER="$2";         shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">> App dir:               $APP_DIR"
echo ">> Upstream:              $UPSTREAM"
echo ">> Listen:                $HOST:$PORT"
echo ">> CSV dir:               $CSV_DIR"
echo ">> Battery capacity (Ah): $BATTERY_CAPACITY_AH (0 = Coulombic SOC disabled)"

if [[ ! -d "$APP_DIR/venv" ]]; then
  echo ">> Creating venv"
  "$PYTHON_BIN" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p "$CSV_DIR"
chown -R "$SERVICE_USER" "$APP_DIR/venv" "$CSV_DIR" || true

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
echo ">> Writing $UNIT_PATH"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Fox H3 AC-AC efficiency test dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py \\
    --host ${HOST} \\
    --port ${PORT} \\
    --upstream ${UPSTREAM} \\
    --csv-dir ${CSV_DIR} \\
    --site-name "${SITE_NAME}" \\
    --battery-capacity-ah ${BATTERY_CAPACITY_AH}
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
