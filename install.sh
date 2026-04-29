#!/usr/bin/env bash
# Flight Tracking Integration — sandbox installer (Tier-1 host-proxy).
#
# This installer takes the same approach as the Planet integration:
# OpenSky OAuth2 credentials live ONLY on the host, in
# ~/.nemoclaw/credentials.json (chmod 600). A small Python daemon
# (`opensky-proxy.py`) runs on the host, mints/refreshes Bearer tokens
# itself, and forwards the sandbox's calls to opensky-network.org. The
# sandbox process only knows the proxy URL — never the client_id or
# secret. A sandbox compromise therefore cannot exfiltrate the key.
#
# Steps performed:
#   1. Read OpenSky creds from credentials.json (prompt if missing).
#   2. (Optionally) refresh the openshell provider record (gateway).
#   3. Start/restart `opensky-proxy.py` on the host (port 9202).
#   4. Detect the host's primary IP and write a network policy that
#      lets the sandbox reach <HOST_IP>:9202 (and ARCGIS / TFR), but
#      NOT opensky-network.org / auth.opensky-network.org directly.
#   5. Stage server files into the sandbox.
#   6. Render flight.env with OPENSKY_PROXY_URL only — no keys.
#   7. Build venv, restart uvicorn, set up the openshell forward.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SANDBOX_NAME="${1:-${OPENSHELL_SANDBOX:-}}"
PORT="${FLIGHT_APP_PORT:-18890}"
OPENSKY_PROXY_PORT="${OPENSKY_PROXY_PORT:-9202}"
# Gateway name registered with `nemoclaw onboard`. "nemoclaw" is the
# convention; override via OPENSHELL_GATEWAY=<name> if you renamed it.
GATEWAY_NAME="${OPENSHELL_GATEWAY:-nemoclaw}"
# Skip the systemd-user tunnel install (e.g. on macOS hosts that don't
# have systemd-user — the script will fall back to `openshell forward`).
SKIP_SYSTEMD_TUNNEL="${SKIP_SYSTEMD_TUNNEL:-0}"

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
      -o ProxyCommand="openshell ssh-proxy --gateway-name $GATEWAY_NAME --name $SANDBOX_NAME" \
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

# ── 2. Resolve OpenSky creds — host-canonical via ~/.nemoclaw/credentials.json ─
#
# Source of truth is ~/.nemoclaw/credentials.json on the HOST. We mirror
# whatever lands here into:
#   1. the openshell provider `flight-tracking-opensky` (gateway-side
#      canonical record, used by future credential-injection paths and
#      makes rotations trivial: edit credentials.json + re-run install.sh)
#   2. the per-sandbox flight.env (current runtime read path — the
#      FastAPI server reads OPENSKY_CLIENT_ID/SECRET from its environment
#      and there's no shell-resolvable resolver for SecretRefs today).
#
# Detect-or-prompt UX:
#   * Both keys present in credentials.json → ask use existing / replace.
#   * Either missing → prompt for the missing values and persist.
#   * `OPENSKY_CLIENT_ID=… ./install.sh` env override still wins (for CI).
#
# Chat & inference auth route through OpenClaw via `openclaw agent --json`,
# so we don't need any inference key of our own.
ok "Chat will route through OpenClaw (\`openclaw agent --json\`)."
ok "OpenClaw already owns inference auth via the gateway-managed route."

# Read whatever's in credentials.json today (may be empty / missing entirely).
read_cred() {
  local key="$1"
  [ -f "$CREDS_PATH" ] || { echo ""; return; }
  python3 -c "
import json, sys
try:
    print(json.load(open('$CREDS_PATH')).get('$key','') or '')
except Exception:
    pass
" 2>/dev/null
}
SAVED_OPENSKY_CLIENT_ID=$(read_cred OPENSKY_CLIENT_ID)
SAVED_OPENSKY_CLIENT_SECRET=$(read_cred OPENSKY_CLIENT_SECRET)
# Legacy Basic-auth vars — kept for back-compat with internal mirrors that
# haven't migrated to OAuth2 yet. Not prompted, not saved by the new wizard.
OPENSKY_USERNAME=$(read_cred OPENSKY_USERNAME)
OPENSKY_PASSWORD=$(read_cred OPENSKY_PASSWORD)

# Env override beats credentials.json (CI / one-shot rotations).
OPENSKY_CLIENT_ID="${OPENSKY_CLIENT_ID:-$SAVED_OPENSKY_CLIENT_ID}"
OPENSKY_CLIENT_SECRET="${OPENSKY_CLIENT_SECRET:-$SAVED_OPENSKY_CLIENT_SECRET}"

# Mask helper for status prints — never echo the full secret.
mask() {
  local v="${1:-}"
  local n=${#v}
  if   [ "$n" -eq 0 ];  then echo "(unset)"
  elif [ "$n" -le 6 ];  then echo "***"
  else echo "${v:0:4}…${v: -4} (${n}c)"
  fi
}

prompt_for_creds() {
  printf "    OPENSKY_CLIENT_ID:     "
  read -r OPENSKY_CLIENT_ID
  printf "    OPENSKY_CLIENT_SECRET: "
  # -s suppresses local echo of the secret
  read -rs OPENSKY_CLIENT_SECRET
  printf "\n"
  if [ -z "$OPENSKY_CLIENT_ID" ] || [ -z "$OPENSKY_CLIENT_SECRET" ]; then
    warn "Both values are required for OAuth2 (~4,000 credits/day)."
    OPENSKY_CLIENT_ID=""
    OPENSKY_CLIENT_SECRET=""
  fi
}

if [ -n "$OPENSKY_CLIENT_ID" ] && [ -n "$OPENSKY_CLIENT_SECRET" ]; then
  echo
  ok "OpenSky credentials found in $CREDS_PATH"
  printf "    OPENSKY_CLIENT_ID     = %s\n" "$(mask "$OPENSKY_CLIENT_ID")"
  printf "    OPENSKY_CLIENT_SECRET = %s\n" "$(mask "$OPENSKY_CLIENT_SECRET")"
  if [ -t 0 ]; then
    printf "    Use existing, [r]eplace, or [s]kip OpenSky upgrade? [U/r/s] "
    read -r answer
    case "${answer:-U}" in
      r|R)
        info "Enter new OpenSky OAuth2 credentials:"
        prompt_for_creds
        ;;
      s|S)
        warn "Skipping OAuth2 — server will fall back to anonymous (~400 credits/day)."
        OPENSKY_CLIENT_ID=""
        OPENSKY_CLIENT_SECRET=""
        ;;
      *)
        ok "Using existing credentials"
        ;;
    esac
  else
    ok "Non-interactive shell — using existing credentials"
  fi
else
  echo
  warn "No OpenSky OAuth2 credentials in $CREDS_PATH"
  info "Without them the server falls back to anonymous (~400 credits/day,"
  info "fast rate-limit hits when running country-wide views)."
  if [ -t 0 ]; then
    printf "    Add OAuth2 credentials now? [Y/n] "
    read -r answer
    case "${answer:-Y}" in
      n|N) warn "Skipped — running anonymous." ;;
      *)   prompt_for_creds ;;
    esac
  fi
fi

# Persist new / changed credentials into credentials.json so it stays the
# single source of truth. We update atomically (tempfile + replace), keep
# 0600 permissions, and only touch the OpenSky keys (other tools' creds
# in the same file are left exactly as we found them).
if [ -n "$OPENSKY_CLIENT_ID" ] && [ -n "$OPENSKY_CLIENT_SECRET" ]; then
  if [ "$OPENSKY_CLIENT_ID" != "$SAVED_OPENSKY_CLIENT_ID" ] \
     || [ "$OPENSKY_CLIENT_SECRET" != "$SAVED_OPENSKY_CLIENT_SECRET" ]; then
    info "Saving credentials to $CREDS_PATH"
    OPENSKY_CLIENT_ID="$OPENSKY_CLIENT_ID" \
    OPENSKY_CLIENT_SECRET="$OPENSKY_CLIENT_SECRET" \
    CREDS_PATH="$CREDS_PATH" \
    python3 - <<'PY'
import json, os, tempfile
p = os.environ['CREDS_PATH']
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    with open(p) as f:
        d = json.load(f)
except Exception:
    d = {}
d['OPENSKY_CLIENT_ID']     = os.environ['OPENSKY_CLIENT_ID']
d['OPENSKY_CLIENT_SECRET'] = os.environ['OPENSKY_CLIENT_SECRET']
fd, tmp = tempfile.mkstemp(prefix='cred.', dir=os.path.dirname(p) or '.')
with os.fdopen(fd, 'w') as f:
    json.dump(d, f, indent=2, sort_keys=True)
os.chmod(tmp, 0o600)
os.replace(tmp, p)
PY
    ok "credentials.json updated"
  fi
fi

# Mirror credentials.json → openshell provider so the gateway has a
# canonical record. Idempotent: create the provider if it's missing,
# otherwise update only the credentials. Required even when we still
# write flight.env into the sandbox today — provider registration is
# the prerequisite for the future host-side proxy / runtime-resolved
# secret path that gets the keys fully out of the sandbox.
sync_openshell_provider() {
  [ -n "$OPENSKY_CLIENT_ID" ] && [ -n "$OPENSKY_CLIENT_SECRET" ] || return 0
  if openshell provider get flight-tracking-opensky >/dev/null 2>&1; then
    info "Updating openshell provider 'flight-tracking-opensky'…"
    openshell provider update flight-tracking-opensky \
      --credential "OPENSKY_CLIENT_ID=$OPENSKY_CLIENT_ID" \
      --credential "OPENSKY_CLIENT_SECRET=$OPENSKY_CLIENT_SECRET" >/dev/null 2>&1 \
      && ok "Provider 'flight-tracking-opensky' refreshed" \
      || warn "Provider update failed; gateway record may be stale"
  else
    info "Registering openshell provider 'flight-tracking-opensky'…"
    openshell provider create \
      --name flight-tracking-opensky --type generic \
      --credential "OPENSKY_CLIENT_ID=$OPENSKY_CLIENT_ID" \
      --credential "OPENSKY_CLIENT_SECRET=$OPENSKY_CLIENT_SECRET" >/dev/null 2>&1 \
      && ok "Provider 'flight-tracking-opensky' created" \
      || warn "Provider create failed; flight.env will still work but rotation requires re-running this script"
  fi
}
sync_openshell_provider

if [ -n "$OPENSKY_CLIENT_ID" ] && [ -n "$OPENSKY_CLIENT_SECRET" ]; then
  ok "OpenSky: OAuth2 client_credentials (~4,000 credits/day)"
  ok "  source-of-truth: $CREDS_PATH"
  ok "  gateway record:  openshell provider 'flight-tracking-opensky'"
elif [ -n "$OPENSKY_USERNAME" ]; then
  warn "OpenSky: legacy Basic auth — not supported by OpenSky since March 2026."
  warn "Add OPENSKY_CLIENT_ID/SECRET to $CREDS_PATH to upgrade."
else
  info "OpenSky: anonymous (~400 credits/day)."
fi

# ── 3. Start the host-side OpenSky proxy ────────────────────────────────
#
# The proxy runs on the host (outside the sandbox), reads creds from
# credentials.json, mints/refreshes OAuth2 Bearer tokens, and forwards
# sandbox requests to opensky-network.org. The sandbox itself never
# sees the secret.
#
# We restart on every install so that:
#   * a credentials.json edit takes effect immediately (proxy reads
#     creds at request time too, but a restart guarantees a clean
#     token cache for the demo)
#   * a new code revision of opensky-proxy.py is picked up
#   * we reset any stuck state from a previous run
echo
info "Starting host-side opensky-proxy on 0.0.0.0:$OPENSKY_PROXY_PORT…"

# Best-effort kill of any prior copy of the daemon. We match the python
# command line rather than relying on a pidfile so a stale pidfile from
# a crashed previous run can't block us.
EXISTING_PID=$(pgrep -f "python3.*opensky-proxy\.py" 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
  info "Stopping existing opensky-proxy (PID $EXISTING_PID)…"
  kill "$EXISTING_PID" 2>/dev/null || true
  sleep 1
  # Force-kill anything that ignored SIGTERM.
  pgrep -f "python3.*opensky-proxy\.py" 2>/dev/null \
    | xargs -r kill -9 2>/dev/null || true
fi

# `setsid nohup` so the daemon survives the install.sh shell exit and
# so it gets its own session (won't be orphaned to the systemd reaper).
mkdir -p "$(dirname /tmp/opensky-proxy.log)" 2>/dev/null || true
setsid nohup python3 "$SCRIPT_DIR/opensky-proxy.py" \
    --port "$OPENSKY_PROXY_PORT" \
    > /tmp/opensky-proxy.log 2>&1 < /dev/null &
PROXY_PID=$!
disown 2>/dev/null || true
sleep 2

if kill -0 "$PROXY_PID" 2>/dev/null; then
  ok "opensky-proxy started (PID $PROXY_PID, port $OPENSKY_PROXY_PORT)"
else
  warn "opensky-proxy failed to start. Last 20 log lines:"
  tail -n 20 /tmp/opensky-proxy.log 2>/dev/null || true
  fail "Could not start opensky-proxy. Inspect /tmp/opensky-proxy.log"
fi

# Health-probe the daemon — a fresh fork can momentarily be running
# without yet being bound to the port, so retry briefly.
PROXY_READY=false
for _ in 1 2 3 4 5; do
  if curl -fsS -o /dev/null --max-time 2 \
        "http://127.0.0.1:${OPENSKY_PROXY_PORT}/health"; then
    PROXY_READY=true
    break
  fi
  sleep 1
done
if [ "$PROXY_READY" = true ]; then
  ok "opensky-proxy /health passed"
else
  warn "opensky-proxy /health didn't respond — proceeding but check /tmp/opensky-proxy.log"
fi

# ── 3b. Start the host-side FAA proxy ───────────────────────────────────
#
# Same pattern as opensky-proxy, but for public no-auth feeds that
# nonetheless reject requests from the openshell gateway's egress IP
# (FAA NAS Status, Aviation Weather Center). No credentials involved
# — this proxy just IP-rewraps the request via the host VM's address,
# which FAA's WAF accepts.
FAA_PROXY_PORT="${FAA_PROXY_PORT:-9203}"
echo
info "Starting host-side faa-proxy on 0.0.0.0:$FAA_PROXY_PORT…"

EXISTING_FAA_PID=$(pgrep -f "python3.*faa-proxy\.py" 2>/dev/null || true)
if [ -n "$EXISTING_FAA_PID" ]; then
  info "Stopping existing faa-proxy (PID $EXISTING_FAA_PID)…"
  kill "$EXISTING_FAA_PID" 2>/dev/null || true
  sleep 1
  pgrep -f "python3.*faa-proxy\.py" 2>/dev/null \
    | xargs -r kill -9 2>/dev/null || true
fi

setsid nohup python3 "$SCRIPT_DIR/faa-proxy.py" \
    --port "$FAA_PROXY_PORT" \
    > /tmp/faa-proxy.log 2>&1 < /dev/null &
FAA_PROXY_PID=$!
disown 2>/dev/null || true
sleep 2

if kill -0 "$FAA_PROXY_PID" 2>/dev/null; then
  ok "faa-proxy started (PID $FAA_PROXY_PID, port $FAA_PROXY_PORT)"
else
  warn "faa-proxy failed to start. Last 20 log lines:"
  tail -n 20 /tmp/faa-proxy.log 2>/dev/null || true
  fail "Could not start faa-proxy. Inspect /tmp/faa-proxy.log"
fi

FAA_READY=false
for _ in 1 2 3 4 5; do
  if curl -fsS -o /dev/null --max-time 2 \
        "http://127.0.0.1:${FAA_PROXY_PORT}/health"; then
    FAA_READY=true
    break
  fi
  sleep 1
done
if [ "$FAA_READY" = true ]; then
  ok "faa-proxy /health passed"
else
  warn "faa-proxy /health didn't respond — proceeding but check /tmp/faa-proxy.log"
fi

# ── 4. Detect the host IP the sandbox should dial ───────────────────────
#
# This must be the address the sandbox sees the host at, not 127.0.0.1
# (the sandbox has its own loopback). The brev VM publishes its primary
# interface address via `hostname -I`; that's what the sandbox's NAT
# tables route to. Allow override via env var for unusual networking
# setups.
HOST_IP="${OPENSKY_PROXY_HOST:-}"
if [ -z "$HOST_IP" ]; then
  HOST_IP=$( (hostname -I 2>/dev/null || true) | awk '{print $1}')
fi
if [ -z "$HOST_IP" ]; then
  HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
fi
[ -z "$HOST_IP" ] && fail "Could not detect host IP. Set OPENSKY_PROXY_HOST."
info "Host IP for sandbox→proxy traffic: $HOST_IP"

OPENSKY_PROXY_URL="http://${HOST_IP}:${OPENSKY_PROXY_PORT}"
FAA_PROXY_URL="http://${HOST_IP}:${FAA_PROXY_PORT}"

# ── 5. Apply network policy ─────────────────────────────────────────────
info "Applying flight_tracking_opensky network policy (Tier-1)…"

POLICY_FILE=$(mktemp /tmp/flight-tracking-policy-XXXX.yaml)
openshell policy get "$SANDBOX_NAME" --full 2>/dev/null | sed '1,/^---$/d' > "$POLICY_FILE"

# Idempotent upsert. Crucially, we DROP opensky-network.org and
# auth.opensky-network.org from the policy — under tier-1 the sandbox
# must reach OpenSky only via the host proxy. Anything else would
# bypass the kept-on-host credential boundary.
export HOST_IP
export PROXY_PORT="$OPENSKY_PROXY_PORT"
export FAA_PROXY_PORT
PATCH_RESULT=$(python3 - "$POLICY_FILE" <<'PY'
import os, sys, yaml
path = sys.argv[1]
host_ip        = os.environ['HOST_IP']
proxy_port     = int(os.environ['PROXY_PORT'])
faa_proxy_port = int(os.environ['FAA_PROXY_PORT'])

with open(path) as f:
    doc = yaml.safe_load(f) or {}
nps = doc.get('network_policies') or {}

desired = {
    'name': 'flight_tracking_opensky',
    'endpoints': [
        {
            # Tier-1 host proxy for OpenSky — the only OpenSky path the
            # sandbox is allowed to take. tls: passthrough because the
            # proxy listens HTTP-only on the loopback bridge; TLS
            # termination happens at the proxy↔OpenSky hop.
            'host': host_ip, 'port': proxy_port, 'protocol': 'rest',
            'tls': 'passthrough', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'GET', 'path': '/api/states/all*'}},
                {'allow': {'method': 'GET', 'path': '/api/flights/aircraft*'}},
                {'allow': {'method': 'GET', 'path': '/api/tracks/all*'}},
                {'allow': {'method': 'GET', 'path': '/health'}},
            ],
        },
        {
            # Tier-1 host proxy for FAA NAS Status + AWC METAR. Both
            # upstreams are public (no auth) but block the openshell
            # gateway's egress IP at the application layer (HTTP 403).
            # We IP-rewrap them through the host VM, which FAA accepts.
            'host': host_ip, 'port': faa_proxy_port, 'protocol': 'rest',
            'tls': 'passthrough', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'GET', 'path': '/nas/api/airport-events*'}},
                {'allow': {'method': 'GET', 'path': '/awc/api/data/metar*'}},
                {'allow': {'method': 'GET', 'path': '/health'}},
            ],
        },
        {
            # FAA AIS ArcGIS REST endpoint. One allow per FeatureServer the
            # app actually uses — see FAA_DATASETS in app/server.py.
            # Adding a new layer? Extend both this list and FAA_DATASETS.
            'host': 'services6.arcgis.com', 'port': 443, 'protocol': 'rest',
            'tls': 'terminate', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/Special_Use_Airspace/FeatureServer/0/query*'}},
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/Class_Airspace/FeatureServer/0/query*'}},
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/Boundary_Airspace/FeatureServer/0/query*'}},
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/AM_Runway/FeatureServer/0/query*'}},
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/AM_Taxiway/FeatureServer/0/query*'}},
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/Digital_Obstacle_File/FeatureServer/0/query*'}},
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/ATS_Route/FeatureServer/0/query*'}},
                {'allow': {'method': 'GET',
                           'path': '/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services/NAVAIDSystem/FeatureServer/0/query*'}},
            ],
        },
        {
            'host': 'tfr.faa.gov', 'port': 443, 'protocol': 'rest',
            'tls': 'terminate', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'GET', 'path': '/geoserver/TFR/ows*'}},
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
    && ok "Policy applied (host-proxy only — direct OpenSky access removed)" \
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

# ── 6. flight.env (Tier-1: zero secrets in sandbox) ─────────────────────
#
# This file used to carry OPENSKY_CLIENT_ID/SECRET into the sandbox.
# Under Tier-1 those values STAY ON THE HOST — the sandbox only learns
# the proxy URL, and the proxy attaches the Bearer token at the
# host↔OpenSky hop. The file is chmod 600 (sandbox user only).
info "Writing flight.env (zero OpenSky secrets — Tier-1)…"

ssh_sandbox "cat > $SANDBOX_BASE/flight.env" <<EOF
# Auto-generated by install.sh — DO NOT EDIT BY HAND.
# Tier-1 architecture: OpenSky credentials live ONLY on the host at
# ~/.nemoclaw/credentials.json. The host-side opensky-proxy.py
# (PID listed via \`pgrep -f opensky-proxy\` on the host) injects
# the Bearer token; this sandbox never sees the secret.
# Rotate by editing credentials.json on the host then re-running
# install.sh.
OPENSKY_PROXY_URL=$OPENSKY_PROXY_URL
FAA_PROXY_URL=$FAA_PROXY_URL
FLIGHT_APP_PORT=$PORT
OPENCLAW_AGENT=main
OPENCLAW_TIMEOUT_S=180
EOF
ssh_sandbox "chmod 600 $SANDBOX_BASE/flight.env" 2>/dev/null
ok "flight.env written (proxy URLs only — no secrets)"

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

# Two separate one-line ssh calls. Multi-line heredoc invocations of
# ssh_sandbox here were causing the SSH session to hang waiting on
# fd cleanup of the disowned background uvicorn — single-line form
# returns in ~3s instead. The minimal sandbox image strips pkill/
# pgrep and restricts plain `ps` to our own session, so the kill
# step walks /proc directly (always available, always sees our own
# UID's processes regardless of session).

# Step 7a — kill any prior uvicorn serving this app.
ssh_sandbox 'for pd in /proc/[0-9]*; do pid=$(basename "$pd"); [ -r "$pd/cmdline" ] || continue; cmd=$(tr "\0" " " < "$pd/cmdline" 2>/dev/null); case "$cmd" in *uvicorn*server:app*) kill -9 "$pid" 2>/dev/null || true ;; esac; done; sleep 1; true' \
  || true

# Step 7b — truncate the log + launch start.sh detached. Inline one-
# liner so SSH's fd cleanup doesn't block on the disowned background.
ssh_sandbox "cd $SANDBOX_BASE && : > server.log && nohup ./start.sh > server.log 2>&1 < /dev/null & disown; true" \
  || true

# Wait until the port is accepting connections AND the listening server
# actually picked up the new flight.env. We don't just check that *some*
# uvicorn is listening — a stale uvicorn from a previous install can
# happily serve the old code on the same port and make this check pass
# with garbage. We probe /api/health and require opensky_auth=host-proxy
# (or anonymous, if creds were intentionally skipped) to know the new
# process is the one answering.
SERVER_UP=false
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  HEALTH=$(ssh_sandbox "
python3 - <<'PY' 2>/dev/null
import json, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:${PORT}/api/health', timeout=2) as r:
        print(r.read().decode())
except Exception:
    pass
PY
" 2>/dev/null || true)
  if [ -n "$HEALTH" ] && echo "$HEALTH" | grep -q '"opensky_auth":"host-proxy"'; then
    SERVER_UP=true
    break
  fi
done

if [ "$SERVER_UP" = true ]; then
  ok "Server is listening on :$PORT (opensky_auth=host-proxy)"
elif [ -n "${HEALTH:-}" ]; then
  warn "Server is listening but did NOT pick up host-proxy mode."
  warn "  /api/health says: $HEALTH"
  warn "  → a stale uvicorn from a previous install is probably still bound to :$PORT."
  warn "  Last 30 log lines:"
  ssh_sandbox "tail -n 30 $SANDBOX_BASE/server.log 2>/dev/null" || true
  fail "FlightOps backend failed to roll over to Tier-1 mode."
else
  warn "Server did not start. Last 30 log lines:"
  ssh_sandbox "tail -n 30 $SANDBOX_BASE/server.log 2>/dev/null" || true
  fail "FlightOps backend failed to come up. Inspect $SANDBOX_BASE/server.log"
fi

# ── 7c. Install / refresh the systemd-user tunnel unit ──────────────────
# Renders scripts/systemd/flight-tunnel.service.template into
# ~/.config/systemd/user/flight-tunnel.service with this install's
# sandbox name, gateway name, app port, and $HOME baked in. This is
# the recommended tunnel path — wraps `ssh -L` with TCP keepalives
# and Restart=always so demo-time gateway flaps auto-recover.
#
# Skipped automatically on hosts without systemd-user (e.g. macOS) or
# when SKIP_SYSTEMD_TUNNEL=1 is set; install.sh's port-forward step
# falls back to `openshell forward` in that case.
TUNNEL_TEMPLATE="$SCRIPT_DIR/scripts/systemd/flight-tunnel.service.template"
TUNNEL_UNIT_DIR="$HOME/.config/systemd/user"
TUNNEL_UNIT="$TUNNEL_UNIT_DIR/flight-tunnel.service"

systemd_user_available() {
  command -v systemctl >/dev/null 2>&1 || return 1
  # systemctl --user --version succeeds when the user manager is up.
  systemctl --user --version >/dev/null 2>&1
}

if [ "$SKIP_SYSTEMD_TUNNEL" = "1" ]; then
  info "Skipping systemd-user tunnel install (SKIP_SYSTEMD_TUNNEL=1)."
elif ! systemd_user_available; then
  info "systemd-user not available — will use \`openshell forward\` fallback."
elif [ ! -f "$TUNNEL_TEMPLATE" ]; then
  warn "Tunnel template missing at $TUNNEL_TEMPLATE — skipping."
else
  info "Installing systemd-user tunnel unit (flight-tunnel.service)…"
  mkdir -p "$TUNNEL_UNIT_DIR"
  # Render the template. sed -e per-placeholder so we don't choke on
  # paths containing slashes (HOME).
  TMP_UNIT=$(mktemp)
  sed -e "s|__SANDBOX_NAME__|$SANDBOX_NAME|g" \
      -e "s|__GATEWAY_NAME__|$GATEWAY_NAME|g" \
      -e "s|__APP_PORT__|$PORT|g" \
      -e "s|__HOME__|$HOME|g" \
      "$TUNNEL_TEMPLATE" > "$TMP_UNIT"
  # Only rewrite the unit (and reload systemd) if it actually changed —
  # avoids spurious restarts when the operator re-runs install.sh
  # against the same sandbox.
  if [ ! -f "$TUNNEL_UNIT" ] || ! cmp -s "$TMP_UNIT" "$TUNNEL_UNIT"; then
    mv "$TMP_UNIT" "$TUNNEL_UNIT"
    systemctl --user daemon-reload
    ok "Wrote $TUNNEL_UNIT"
  else
    rm -f "$TMP_UNIT"
    ok "Tunnel unit already up to date"
  fi
  systemctl --user enable flight-tunnel.service >/dev/null 2>&1 || true
  ok "Tunnel unit enabled (sandbox=$SANDBOX_NAME, gateway=$GATEWAY_NAME, port=$PORT)"
fi

# ── 8. Host-side port forward ───────────────────────────────────────────
# Two paths supported, preferred-first:
#
#   (a) systemd-user unit `flight-tunnel.service` (recommended). Wraps a
#       raw `ssh -L 18890:...` with ServerAliveInterval=30 +
#       ExitOnForwardFailure=yes + Restart=always, so a transient
#       openshell gateway flap auto-recovers within ~5s. This is the
#       path that survives demos.
#
#   (b) `openshell forward start ... -d` as a fallback for hosts that
#       don't have systemd-user (or where the unit isn't installed).
#       Less robust — when the underlying gateway connection breaks
#       the forward stays half-alive (accepts but never proxies),
#       which is what bit us during demo prep.
#
# Either way we VERIFY by hitting /api/health through the forward —
# `openshell forward list` returning a row isn't enough proof.
info "Forwarding localhost:$PORT to the sandbox…"

verify_forward() {
  curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PORT/api/health"
}

forward_ok=false

# (a) systemd-user unit if installed.
if command -v systemctl >/dev/null 2>&1 \
   && systemctl --user list-unit-files flight-tunnel.service \
        2>/dev/null | grep -q '^flight-tunnel.service'; then
  # Cycle to pick up any policy / sandbox changes from this install.
  systemctl --user restart flight-tunnel.service >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    sleep 1
    if verify_forward; then forward_ok=true; break; fi
  done
  if $forward_ok; then
    ok "Port forward active via systemd-user (flight-tunnel.service): http://localhost:$PORT"
  else
    warn "flight-tunnel.service didn't come up; falling back to \`openshell forward\`."
  fi
fi

# (b) openshell forward fallback.
if ! $forward_ok; then
  openshell forward stop "$PORT" >/dev/null 2>&1 || true
  openshell forward start "$PORT" "$SANDBOX_NAME" -d >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    sleep 1
    if verify_forward; then forward_ok=true; break; fi
  done
  if $forward_ok; then
    ok "Port forward active via openshell: http://localhost:$PORT"
  fi
fi

if ! $forward_ok; then
  warn "Port forward not reachable on http://localhost:$PORT — recover with:"
  warn "  systemctl --user restart flight-tunnel.service   # if the unit is installed"
  warn "  openshell forward stop $PORT && openshell forward start $PORT $SANDBOX_NAME -d"
fi

# ── 9. Refresh agent sessions so the skill is picked up ────────────────
ssh_sandbox "[ -f $SESSIONS_PATH ] && echo '{}' > $SESSIONS_PATH || true" 2>/dev/null \
  && ok "Agent sessions cleared (skill will load on next message)"

# ── 10. Health check ────────────────────────────────────────────────────
info "Probing health endpoint…"
sleep 2
# --max-time guard: a half-open SSH forward (the openshell-forward
# failure mode this install previously hit) accepts the TCP connect
# but never proxies bytes back, which would hang `curl -fsSL` for
# minutes. Bound it tight so the installer always returns.
HEALTH=$(curl -fsSL --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)
if [ -n "$HEALTH" ]; then
  ok "Backend reachable: $HEALTH"
else
  warn "Health check did not return — server may still be warming up,"
  warn "or the host→sandbox port forward is half-open. Recover with:"
  warn "  systemctl --user restart flight-tunnel.service"
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

  Secrets (Tier-1 host-proxy):
    canonical:    $CREDS_PATH        (host, chmod 600)
    opensky:      opensky-proxy.py @ http://${HOST_IP}:${OPENSKY_PROXY_PORT}  (host, /tmp/opensky-proxy.log)
    faa+awc:      faa-proxy.py     @ http://${HOST_IP}:${FAA_PROXY_PORT}     (host, /tmp/faa-proxy.log)
    gateway:      openshell provider 'flight-tracking-opensky'
    sandbox:      $SANDBOX_BASE/flight.env  (no secrets — only proxy URLs)
    rotate:       edit credentials.json → re-run ./install.sh

  Try in chat:
    "Go to IAD and analyse traffic"
    "Show inbound arcs to JFK"
    "Any unusual squawks near LHR right now?"

EOF
