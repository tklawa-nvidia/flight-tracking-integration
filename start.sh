#!/usr/bin/env bash
# Sandbox-side launcher. install.sh uploads this to
# /sandbox/.openclaw-data/flight-tracking/start.sh and runs it under nohup
# inside the sandbox.

set -euo pipefail

APP_DIR="/sandbox/.openclaw-data/flight-tracking/app"
VENV="/sandbox/.openclaw-data/flight-tracking/venv"
LOG="/sandbox/.openclaw-data/flight-tracking/server.log"
PORT="${FLIGHT_APP_PORT:-18890}"

# Pull the env file (created by install.sh) into the current shell.
ENV_FILE="/sandbox/.openclaw-data/flight-tracking/flight.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# Activate the venv install.sh built and exec uvicorn directly so signals
# propagate cleanly when systemd-style supervisors restart us.
# shellcheck disable=SC1091
. "$VENV/bin/activate"

cd "$APP_DIR"
exec python -m uvicorn server:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --log-level info \
  --no-access-log
