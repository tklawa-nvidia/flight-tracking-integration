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

# Free the port from any previous instance BEFORE binding. A reinstall's
# remote kill can miss an app that a prior session launched (different exec
# channel / PID view), and uvicorn would then exit with "address already in
# use" while the STALE build keeps serving — making a fresh install look
# like it "didn't take". Best-effort, multiple methods, never fatal.
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
fi
for pd in /proc/[0-9]*; do
  [ -r "$pd/cmdline" ] || continue
  _c=$(tr '\0' ' ' < "$pd/cmdline" 2>/dev/null)
  case "$_c" in
    *uvicorn*server:app*)
      # Only reap our OWN port: the sibling demos (boat, satellite) run the
      # same "uvicorn server:app" command line and differ only by --port, so an
      # unscoped kill here tears them down too.
      case "$_c" in *"--port $PORT "*) ;; *) continue ;; esac
      _p=$(basename "$pd")
      [ "$_p" = "$$" ] || kill -9 "$_p" 2>/dev/null || true ;;
  esac
done
sleep 1

# Access logs are ON: every /api/* request the OpenClaw agent issues lands in
# $LOG, so when a skill call returns 4xx/5xx you can grep the log to see the
# exact method+path+status without having to instrument the agent.
exec python -m uvicorn server:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --log-level info \
  --access-log
