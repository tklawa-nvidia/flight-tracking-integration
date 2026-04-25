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
  # -F /dev/null skips system-wide SSH config; some cloud images ship
  # /etc/ssh/ssh_config.d files with bad owner/permissions, which OpenSSH
  # 9.x treats as fatal and aborts before our exec gets a chance to run.
  ssh -F /dev/null \
      -o StrictHostKeyChecking=no \
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

# ── 2. Resolve OpenSky creds (chat goes through OpenClaw — no inference key) ─
ok "Chat will route through OpenClaw (\`openclaw agent --json\`)."
ok "OpenClaw already owns inference auth via the gateway-managed route."

# OpenSky removed Basic auth in March 2026 in favour of OAuth2 client_credentials.
# We pull the API client id/secret from ~/.nemoclaw/credentials.json so the
# secret never leaves the host filesystem (the install script writes a
# permissioned flight.env into the sandbox; the file is .gitignored). The
# legacy USERNAME/PASSWORD vars are still read for backwards compatibility
# with internal mirrors that haven't migrated yet.
OPENSKY_CLIENT_ID="${OPENSKY_CLIENT_ID:-}"
OPENSKY_CLIENT_SECRET="${OPENSKY_CLIENT_SECRET:-}"
OPENSKY_USERNAME="${OPENSKY_USERNAME:-}"
OPENSKY_PASSWORD="${OPENSKY_PASSWORD:-}"
if [ -f "$CREDS_PATH" ]; then
  if [ -z "$OPENSKY_CLIENT_ID" ]; then
    OPENSKY_CLIENT_ID=$(python3 -c "
import json
try: print(json.load(open('$CREDS_PATH')).get('OPENSKY_CLIENT_ID',''))
except: pass
" 2>/dev/null || true)
  fi
  if [ -z "$OPENSKY_CLIENT_SECRET" ]; then
    OPENSKY_CLIENT_SECRET=$(python3 -c "
import json
try: print(json.load(open('$CREDS_PATH')).get('OPENSKY_CLIENT_SECRET',''))
except: pass
" 2>/dev/null || true)
  fi
  if [ -z "$OPENSKY_USERNAME" ]; then
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
fi

if [ -n "$OPENSKY_CLIENT_ID" ] && [ -n "$OPENSKY_CLIENT_SECRET" ]; then
  ok "OpenSky: OAuth2 client_credentials (~4,000 credits/day)"
elif [ -n "$OPENSKY_USERNAME" ]; then
  warn "OpenSky: legacy Basic auth — not supported by OpenSky since March 2026."
  warn "Add OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET to $CREDS_PATH to upgrade."
else
  info "OpenSky: anonymous (~400 credits/day). Add OPENSKY_CLIENT_ID/SECRET to $CREDS_PATH for ~4,000."
fi

# ── 3. Apply network policy ─────────────────────────────────────────────
info "Applying flight_tracking_opensky network policy…"

POLICY_FILE=$(mktemp /tmp/flight-tracking-policy-XXXX.yaml)
openshell policy get "$SANDBOX_NAME" --full 2>/dev/null | sed '1,/^---$/d' > "$POLICY_FILE"

# Idempotent upsert of the flight_tracking_opensky block. Earlier versions
# of the demo only opened opensky-network.org; OAuth2 also needs
# auth.opensky-network.org for the token mint, so we reconcile both hosts.
PATCH_RESULT=$(python3 - "$POLICY_FILE" <<'PY'
import sys, yaml
path = sys.argv[1]
with open(path) as f:
    doc = yaml.safe_load(f) or {}
nps = doc.get('network_policies') or {}
desired = {
    'name': 'flight_tracking_opensky',
    'endpoints': [
        {
            'host': 'opensky-network.org', 'port': 443, 'protocol': 'rest',
            'tls': 'terminate', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'GET', 'path': '/api/states/all'}},
                {'allow': {'method': 'GET', 'path': '/api/states/all*'}},
            ],
        },
        {
            'host': 'auth.opensky-network.org', 'port': 443, 'protocol': 'rest',
            'tls': 'terminate', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'POST',
                           'path': '/auth/realms/opensky-network/protocol/openid-connect/token'}},
            ],
        },
    ],
    'binaries': [
        {'path': '/usr/bin/python3'},
        {'path': '/usr/bin/python3.11'},
    ],
}
if nps.get('flight_tracking_opensky') == desired:
    print('unchanged')
else:
    nps['flight_tracking_opensky'] = desired
    doc['network_policies'] = nps
    with open(path, 'w') as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    print('patched')
PY
)
if [ "$PATCH_RESULT" = "unchanged" ]; then
  ok "Policy already up to date"
else
  openshell policy set "$SANDBOX_NAME" --policy "$POLICY_FILE" --wait 2>&1 \
    && ok "Policy applied (added/updated flight_tracking_opensky)" \
    || fail "openshell policy set failed; review $POLICY_FILE"
fi
rm -f "$POLICY_FILE"

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

ssh_sandbox "cat > $SANDBOX_BASE/flight.env" <<EOF
OPENSKY_CLIENT_ID=$OPENSKY_CLIENT_ID
OPENSKY_CLIENT_SECRET=$OPENSKY_CLIENT_SECRET
OPENSKY_USERNAME=$OPENSKY_USERNAME
OPENSKY_PASSWORD=$OPENSKY_PASSWORD
FLIGHT_APP_PORT=$PORT
OPENCLAW_AGENT=main
OPENCLAW_TIMEOUT_S=180
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
set -e
# Best-effort kill of any prior uvicorn for this app.
if command -v pkill >/dev/null 2>&1; then
  pkill -f 'uvicorn server:app' >/dev/null 2>&1 || true
else
  ps -eo pid,args 2>/dev/null | awk '/uvicorn server:app/ && !/awk/ {print \$1}' \
    | xargs -r kill 2>/dev/null || true
fi
sleep 1
# Detach with setsid so the server survives the SSH disconnect on minimal images.
setsid nohup $SANDBOX_BASE/start.sh > $SANDBOX_BASE/server.log 2>&1 < /dev/null &
disown 2>/dev/null || true
"

# Wait until the port is actually accepting connections from inside the sandbox.
SERVER_UP=false
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if ssh_sandbox "
if command -v ss >/dev/null 2>&1; then
  ss -ltn 2>/dev/null | awk '{print \$4}' | grep -qE ':(${PORT})\$'
elif command -v netstat >/dev/null 2>&1; then
  netstat -ltn 2>/dev/null | awk '{print \$4}' | grep -qE ':(${PORT})\$'
else
  python3 -c 'import socket,sys;s=socket.socket();exit(0 if s.connect_ex((\"127.0.0.1\",${PORT}))==0 else 1)'
fi
  " 2>/dev/null; then
    SERVER_UP=true
    break
  fi
done

if [ "$SERVER_UP" = true ]; then
  ok "Server is listening on :$PORT"
else
  warn "Server did not start. Last 30 log lines:"
  ssh_sandbox "tail -n 30 $SANDBOX_BASE/server.log 2>/dev/null" || true
  fail "FlightOps backend failed to come up. Inspect $SANDBOX_BASE/server.log"
fi

# ── 8. Host-side port forward ───────────────────────────────────────────
info "Forwarding localhost:$PORT to the sandbox…"

# Stop any prior forward on this port, then re-start in the background.
openshell forward stop "$PORT" >/dev/null 2>&1 || true
openshell forward start "$PORT" "$SANDBOX_NAME" -d >/dev/null 2>&1 || true
sleep 1
# `openshell forward list` columns are: SANDBOX BIND PORT PID STATUS — match
# either the PORT column or a "BIND:PORT" pattern, whichever the version emits.
if openshell forward list 2>/dev/null | awk 'NR>1 {print $3}' | grep -qx "$PORT"; then
  ok "Port forward active: http://localhost:$PORT"
elif curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/api/health"; then
  ok "Port forward active (verified via /api/health): http://localhost:$PORT"
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
