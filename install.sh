#!/usr/bin/env bash
# Flight Tracking Integration — sandbox installer.
#
# Sandbox-side install. Per the demo brief, no host-side proxy is used:
# OpenSky has no auth secret in anonymous mode, and the inference key is
# read at server startup from the env file we drop into
# /sandbox/.openclaw-data/flight-tracking/flight.env. If you want to harden
# this later, swap inference traffic onto a host proxy and remove the key
# from the env file — the server already reads INFERENCE_BASE_URL/MODEL/
# INFERENCE_API_KEY straight from the environment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SANDBOX_NAME="${1:-${OPENSHELL_SANDBOX:-}}"
PORT="${FLIGHT_APP_PORT:-18890}"

CREDS_PATH="$HOME/.nemoclaw/credentials.json"
ONBOARD_PATH="$HOME/.nemoclaw/onboard-session.json"
SANDBOX_BASE="/sandbox/.openclaw-data/flight-tracking"
SKILLS_BASE="/sandbox/.openclaw-data/skills"
SESSIONS_PATH="/sandbox/.openclaw-data/agents/main/sessions/sessions.json"

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { printf "${CYAN}  ▸ %s${NC}\n" "$1"; }
ok()    { printf "${GREEN}  ✓ %s${NC}\n" "$1"; }
warn()  { printf "${YELLOW}  ⚠ %s${NC}\n" "$1"; }
fail()  { printf "${RED}  ✗ %s${NC}\n" "$1"; exit 1; }

ssh_sandbox() {
  ssh -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR \
      -o ProxyCommand="openshell ssh-proxy --gateway-name nemoclaw --name $SANDBOX_NAME" \
      "sandbox@openshell-$SANDBOX_NAME" "$@"
}

cat <<EOF

  ╔════════════════════════════════════════════════════════════╗
  ║  FlightOps — Flight Tracking Integration installer        ║
  ║  Live aircraft on a deck.gl + MapLibre console           ║
  ╚════════════════════════════════════════════════════════════╝

EOF

# ── 0. Sandbox name ─────────────────────────────────────────────────────
if [ -z "$SANDBOX_NAME" ]; then
  SANDBOX_NAME=$(python3 -c "
import json, os
try:
    p = os.path.expanduser('~/.nemoclaw/sandboxes.json')
    print(json.load(open(p)).get('defaultSandbox',''))
except Exception:
    pass
" 2>/dev/null || true)
fi
if [ -z "$SANDBOX_NAME" ]; then
  printf "  Sandbox name: "
  read -r SANDBOX_NAME
fi
[ -z "$SANDBOX_NAME" ] && fail "No sandbox name provided. Usage: ./install.sh <sandbox-name>"
info "Target sandbox: $SANDBOX_NAME"

# ── 1. Prerequisites ────────────────────────────────────────────────────
command -v openshell >/dev/null 2>&1 || fail "openshell CLI not found"
command -v python3   >/dev/null 2>&1 || fail "python3 not found on host"
openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME" \
  || fail "Sandbox '$SANDBOX_NAME' not found. Run 'nemoclaw onboard' first."
ok "Prerequisites OK"

# ── 2. Resolve inference + secrets from existing nemoclaw state ─────────
INFERENCE_API_KEY=""
INFERENCE_BASE_URL="https://inference-api.nvidia.com/v1"
INFERENCE_MODEL="nvidia/nvidia/nemotron-3-super-v3"

if [ -f "$ONBOARD_PATH" ]; then
  INFERENCE_BASE_URL=$(python3 -c "
import json
try:
    d = json.load(open('$ONBOARD_PATH'))
    print(d.get('endpointUrl','https://inference-api.nvidia.com/v1'))
except Exception:
    print('https://inference-api.nvidia.com/v1')
")
  INFERENCE_MODEL=$(python3 -c "
import json
try:
    d = json.load(open('$ONBOARD_PATH'))
    print(d.get('model','nvidia/nvidia/nemotron-3-super-v3'))
except Exception:
    print('nvidia/nvidia/nemotron-3-super-v3')
")
  CRED_ENV=$(python3 -c "
import json
try:
    d = json.load(open('$ONBOARD_PATH'))
    print(d.get('credentialEnv','COMPATIBLE_API_KEY'))
except Exception:
    print('COMPATIBLE_API_KEY')
")
else
  CRED_ENV="COMPATIBLE_API_KEY"
fi

if [ -f "$CREDS_PATH" ]; then
  INFERENCE_API_KEY=$(python3 -c "
import json
try:
    print(json.load(open('$CREDS_PATH')).get('$CRED_ENV',''))
except Exception:
    pass
" 2>/dev/null || true)
fi

if [ -z "$INFERENCE_API_KEY" ]; then
  warn "No inference API key found in $CREDS_PATH ($CRED_ENV)."
  printf "  Paste the inference API key (or leave blank to install without copilot): "
  read -r INFERENCE_API_KEY
fi

if [ -n "$INFERENCE_API_KEY" ]; then
  ok "Inference: $INFERENCE_MODEL via $INFERENCE_BASE_URL"
else
  warn "Copilot will be disabled until you set INFERENCE_API_KEY in flight.env."
fi

# Optional OpenSky basic auth (lifts rate limit from 10s -> 5s).
OPENSKY_USERNAME="${OPENSKY_USERNAME:-}"
OPENSKY_PASSWORD="${OPENSKY_PASSWORD:-}"
if [ -z "$OPENSKY_USERNAME" ] && [ -f "$CREDS_PATH" ]; then
  OPENSKY_USERNAME=$(python3 -c "
import json
try: print(json.load(open('$CREDS_PATH')).get('OPENSKY_USERNAME',''))
except: pass
" 2>/dev/null || true)
  OPENSKY_PASSWORD=$(python3 -c "
import json
try: print(json.load(open('$CREDS_PATH')).get('OPENSKY_PASSWORD',''))
except: pass
" 2>/dev/null || true)
fi
if [ -z "$OPENSKY_USERNAME" ]; then
  info "OpenSky: anonymous (10s cadence). Set OPENSKY_USERNAME/PASSWORD to lift rate limit."
fi

# ── 3. Apply network policy ─────────────────────────────────────────────
info "Applying flight_tracking_opensky network policy…"

CURRENT_POLICY=$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null | sed '1,/^---$/d' || true)
POLICY_FILE=$(mktemp /tmp/flight-tracking-policy-XXXX.yaml)

if echo "$CURRENT_POLICY" | grep -q "flight_tracking_opensky"; then
  ok "Policy already contains flight_tracking_opensky"
else
  cat > "$POLICY_FILE" <<EOF
$(echo "$CURRENT_POLICY" | sed -e :a -e '/^\s*$/{$d;N;ba' -e '}')
  flight_tracking_opensky:
    name: flight_tracking_opensky
    endpoints:
    - host: opensky-network.org
      port: 443
      protocol: rest
      tls: terminate
      enforcement: enforce
      rules:
      - allow:
          method: GET
          path: /api/states/all
      - allow:
          method: GET
          path: /api/states/all*
    binaries:
    - path: /usr/bin/python3
    - path: /usr/bin/python3.11
EOF
  openshell policy set "$SANDBOX_NAME" --policy "$POLICY_FILE" --wait 2>&1 \
    && ok "Policy applied" \
    || fail "openshell policy set failed; review $POLICY_FILE"
  rm -f "$POLICY_FILE"
fi

# ── 4. Stage server files inside the sandbox ────────────────────────────
info "Provisioning $SANDBOX_BASE in the sandbox…"

ssh_sandbox "mkdir -p $SANDBOX_BASE/app/static/data $SKILLS_BASE/flight-tracking/scripts" 2>/dev/null

upload() {
  local src="$1" dest="$2"
  cat "$src" | ssh_sandbox "cat > $dest"
}

upload "$SCRIPT_DIR/app/server.py"                                     "$SANDBOX_BASE/app/server.py"
upload "$SCRIPT_DIR/app/requirements.txt"                              "$SANDBOX_BASE/app/requirements.txt"
upload "$SCRIPT_DIR/app/static/index.html"                             "$SANDBOX_BASE/app/static/index.html"
upload "$SCRIPT_DIR/app/static/styles.css"                             "$SANDBOX_BASE/app/static/styles.css"
upload "$SCRIPT_DIR/app/static/app.js"                                 "$SANDBOX_BASE/app/static/app.js"
upload "$SCRIPT_DIR/app/static/data/airports.json"                     "$SANDBOX_BASE/app/static/data/airports.json"
upload "$SCRIPT_DIR/start.sh"                                          "$SANDBOX_BASE/start.sh"
upload "$SCRIPT_DIR/skills/flight-tracking/SKILL.md"                   "$SKILLS_BASE/flight-tracking/SKILL.md"
upload "$SCRIPT_DIR/skills/flight-tracking/scripts/fly"                "$SKILLS_BASE/flight-tracking/scripts/fly"

ssh_sandbox "chmod +x $SANDBOX_BASE/start.sh $SKILLS_BASE/flight-tracking/scripts/fly" 2>/dev/null
ok "Files staged"

# ── 5. flight.env ───────────────────────────────────────────────────────
info "Writing flight.env (kept inside sandbox at $SANDBOX_BASE/flight.env)…"

# Use a HEREDOC so embedded characters in the API key don't break the shell.
ssh_sandbox "cat > $SANDBOX_BASE/flight.env" <<EOF
INFERENCE_BASE_URL=$INFERENCE_BASE_URL
INFERENCE_MODEL=$INFERENCE_MODEL
INFERENCE_API_KEY=$INFERENCE_API_KEY
OPENSKY_USERNAME=$OPENSKY_USERNAME
OPENSKY_PASSWORD=$OPENSKY_PASSWORD
FLIGHT_APP_PORT=$PORT
EOF
ssh_sandbox "chmod 600 $SANDBOX_BASE/flight.env" 2>/dev/null
ok "flight.env written"

# ── 6. Build venv + install deps inside the sandbox ─────────────────────
info "Building Python venv inside the sandbox (one-time)…"
ssh_sandbox "
set -euo pipefail
cd $SANDBOX_BASE
if [ ! -x venv/bin/python ]; then
  python3 -m venv venv
fi
. venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r app/requirements.txt
"
ok "Python deps installed"

# ── 7. (Re)start the server inside the sandbox ──────────────────────────
info "Starting FlightOps server inside the sandbox on port $PORT…"

ssh_sandbox "
pkill -f 'uvicorn server:app' >/dev/null 2>&1 || true
sleep 1
nohup $SANDBOX_BASE/start.sh > $SANDBOX_BASE/server.log 2>&1 &
sleep 2
pgrep -f 'uvicorn server:app' >/dev/null
" && ok "Server is running" || fail "Server failed to start. Tail $SANDBOX_BASE/server.log"

# ── 8. Host-side port forward ───────────────────────────────────────────
info "Forwarding localhost:$PORT to the sandbox…"

# Stop any prior forward on this port, then re-start in the background.
openshell forward stop "$PORT" >/dev/null 2>&1 || true
openshell forward start "$PORT" "$SANDBOX_NAME" -d >/dev/null 2>&1 || true
sleep 1
if openshell forward list 2>/dev/null | grep -q ":$PORT"; then
  ok "Port forward active: http://localhost:$PORT"
else
  warn "Port forward not visible — start it manually: openshell forward start $PORT $SANDBOX_NAME -d"
fi

# ── 9. Refresh agent sessions so the skill is picked up ────────────────
ssh_sandbox "[ -f $SESSIONS_PATH ] && echo '{}' > $SESSIONS_PATH || true" 2>/dev/null \
  && ok "Agent sessions cleared (skill will load on next message)"

# ── 10. Health check ────────────────────────────────────────────────────
info "Probing health endpoint…"
sleep 2
HEALTH=$(curl -fsSL "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)
if [ -n "$HEALTH" ]; then
  ok "Backend reachable: $HEALTH"
else
  warn "Health check did not return — server may still be warming up."
fi

cat <<EOF

  ╔════════════════════════════════════════════════════════════╗
  ║  FlightOps installed                                      ║
  ╚════════════════════════════════════════════════════════════╝

  Console:     http://localhost:$PORT
  API:         http://localhost:$PORT/api/health
  Logs:        ssh into $SANDBOX_NAME, then tail $SANDBOX_BASE/server.log
  Skill:       /sandbox/.openclaw-data/skills/flight-tracking
  Helper:      \`fly\` CLI inside the sandbox (try: fly goto IAD)

  Try in chat:
    "Go to IAD and analyse traffic"
    "Show inbound arcs to JFK"
    "Any unusual squawks near LHR right now?"

EOF
