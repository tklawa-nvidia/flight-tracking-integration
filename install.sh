#!/usr/bin/env bash
# Flight Tracking Integration — sandbox installer (Tier-1 host-proxy).
#
# What this does:
#   1. Detect the OpenClaw sandbox layout. Two are supported:
#         New (openshell ≥ 0.0.44 / openclaw ≥ 2026.5.x):
#           config:  /sandbox/.openclaw/openclaw.json
#           skills:  /sandbox/.openclaw/skills/
#           agents:  /sandbox/.openclaw/agents/<agent>/
#         Legacy (older builds):
#           skills:  /sandbox/.openclaw-data/skills/
#           agents:  /sandbox/.openclaw-data/agents/<agent>/
#      The FastAPI server + venv always live under
#      /sandbox/.openclaw-data/flight-tracking/ (host-managed data
#      area, present on both layouts) so a reinstall never rebuilds
#      the venv.
#   2. Read OpenSky creds from ~/.nemoclaw/credentials.json (Tier-1
#      source of truth) and refresh the openshell provider record.
#   3. Start the two host-side proxies (opensky-proxy.py:9202 and
#      faa-proxy.py:9203). They mint Bearer tokens / IP-rewrap on
#      the host so the sandbox never sees OpenSky secrets and never
#      egresses from the gateway ASN.
#   4. Apply the Tier-1 network policy: sandbox can reach the host
#      proxies + FAA ArcGIS / TFR direct, but NOT opensky-network.org
#      or auth.opensky-network.org.
#   5. Stage server.py + static assets + venv inside the sandbox.
#   6. Write flight.env with proxy URLs + the detected OPENCLAW_AGENT_HOME
#      so server.py reads the right sessions/JSONL files.
#   7. On the new layout: enable the flight-tracking skill in
#      openclaw.json and ensure tools.profile=coding so the chat
#      agent gets the `exec` tool in its system prompt (without
#      this it spins out searching for tools that aren't surfaced).
#   8. Install + start the systemd-user tunnel (or fall back to
#      `openshell forward`) so the browser can reach
#      http://localhost:18890.
#
# Tested compat matrix:
#   openshell 0.0.44 + openclaw 2026.5.18         (current — new layout)
#   older openshell pre-skill-registry builds     (legacy layout)
#
# Operational flags (all optional; defaults are safe to use):
#   ./install.sh [sandbox-name]
#   ./install.sh --status        Print current install + proxy state and exit
#   ./install.sh --uninstall     Stop proxies, drop policy block, remove
#                                skill + app from sandbox, stop tunnel
#   ./install.sh --update-creds  Force-prompt for new OpenSky creds
#   ./install.sh --skip-systemd  Don't touch the systemd-user tunnel
#                                (also via SKIP_SYSTEMD_TUNNEL=1)
#   ./install.sh --port N        Override the FastAPI port (default 18890,
#                                also via FLIGHT_APP_PORT=N)
#   ./install.sh -h | --help     Show this help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.nemoclaw/flight-tracking"

PORT="${FLIGHT_APP_PORT:-18890}"
OPENSKY_PROXY_PORT="${OPENSKY_PROXY_PORT:-9202}"
FAA_PROXY_PORT="${FAA_PROXY_PORT:-9203}"
# Live-aircraft data source for the host proxy:
#   "community" (default) — free, no-auth ADS-B aggregators (adsb.fi /
#     airplanes.live / adsb.lol). These are reachable from cloud/VM IPs.
#     OpenSky blackholes datacenter IP ranges (AWS/GCP/Azure), so from a
#     brev VM opensky-network.org is unreachable — hence community is the
#     default so planes show out of the box.
#   "opensky" — legacy OAuth2 path to opensky-network.org (needs creds and
#     an egress IP OpenSky accepts; use only on non-cloud hosts).
# Record whether the source was pinned explicitly (env/flag) BEFORE we apply
# the default — lets the interactive wizard offer a choice only when the user
# didn't already decide (and stay non-interactive for CI / piped installs).
if [ -n "${FLIGHT_ADSB_SOURCE:-}" ]; then
  FLIGHT_ADSB_SOURCE_EXPLICIT=1
else
  FLIGHT_ADSB_SOURCE_EXPLICIT=0
fi
FLIGHT_ADSB_SOURCE="${FLIGHT_ADSB_SOURCE:-community}"
# Gateway name registered with `nemoclaw onboard`. "nemoclaw" is the
# convention; override via OPENSHELL_GATEWAY=<name> if you renamed it.
GATEWAY_NAME="${OPENSHELL_GATEWAY:-nemoclaw}"
# Skip the systemd-user tunnel install (e.g. on macOS hosts that don't
# have systemd-user — the script will fall back to `openshell forward`).
SKIP_SYSTEMD_TUNNEL="${SKIP_SYSTEMD_TUNNEL:-0}"
# Optional public "Brev link" forward: a second, reboot-persistent
# systemd-user unit that binds 0.0.0.0:$BREV_FORWARD_PORT -> sandbox app,
# so Brev can publish the console as a secure link
# (*.apps.run.brev.nvidia.com). Off by default because 0.0.0.0 exposes
# the app on the VM's network; enable with --brev-link (optionally
# --brev-link <port>) or ENABLE_BREV_FORWARD=1. The loopback
# flight-tunnel.service (localhost) is always installed regardless.
ENABLE_BREV_FORWARD="${ENABLE_BREV_FORWARD:-0}"
BREV_FORWARD_PORT="${BREV_FORWARD_PORT:-18891}"

CREDS_PATH="$HOME/.nemoclaw/credentials.json"

# App dir stays in .openclaw-data (host-managed data area, present on
# both layouts) so reinstalls don't blow away the venv.
SANDBOX_BASE="/sandbox/.openclaw-data/flight-tracking"

# Skills + agent home + openclaw.json are layout-dependent; populated
# by detect_paths() below once SANDBOX_NAME is known.
LAYOUT=""
SKILLS_BASE=""
OPENCLAW_AGENT_HOME=""
OPENCLAW_JSON=""

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { printf "${CYAN}  ▸ %s${NC}\n" "$1"; }
ok()    { printf "${GREEN}  ✓ %s${NC}\n" "$1"; }
warn()  { printf "${YELLOW}  ⚠ %s${NC}\n" "$1"; }
fail()  { printf "${RED}  ✗ %s${NC}\n" "$1"; exit 1; }

# ── openshell binary discovery ──────────────────────────────────────────
# Avoids relying on PATH being correctly set in non-interactive shells
# (cron jobs, systemd ExecStartPre hooks, MCP-spawned invocations).
OPENSHELL_BIN=""
for candidate in \
  "$(command -v openshell 2>/dev/null || true)" \
  "$HOME/.local/bin/openshell" \
  "/usr/local/bin/openshell" \
  "/usr/bin/openshell"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    OPENSHELL_BIN="$candidate"; break
  fi
done

ssh_sandbox() {
  # -F /dev/null skips system-wide SSH config; some cloud images ship
  # /etc/ssh/ssh_config.d files with bad owner/permissions, which OpenSSH
  # 9.x treats as fatal and aborts before our exec gets a chance to run.
  ssh -F /dev/null \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o GlobalKnownHostsFile=/dev/null \
      -o LogLevel=ERROR \
      -o ConnectTimeout=15 \
      -o ProxyCommand="$OPENSHELL_BIN ssh-proxy --gateway-name $GATEWAY_NAME --name $SANDBOX_NAME" \
      "sandbox@openshell-$SANDBOX_NAME" "$@"
}

usage_exit() {
  cat <<EOF

  Usage: ./install.sh [options] [sandbox-name]

  Options:
    --status               Print install + proxy state and exit
    --uninstall            Remove sandbox-side install, stop proxies + tunnel
    --update-creds         Force-prompt for new OpenSky OAuth2 credentials
    --skip-systemd         Don't touch the systemd-user tunnel
    --brev-link [<port>]   Also install a reboot-persistent 0.0.0.0 forward
                           (default port 18891) so Brev can publish a secure
                           link. Loopback/localhost access is unaffected.
    --port <N>             FastAPI port (default 18890)
    --opensky-port <N>     opensky-proxy port (default 9202)
    --faa-port <N>         faa-proxy port (default 9203)
    -h, --help             Show this help

  Env vars:
    OPENSHELL_GATEWAY      Gateway name registered with nemoclaw onboard
                           (default "nemoclaw")
    OPENSHELL_SANDBOX      Default sandbox name when positional is omitted
    OPENSKY_PROXY_HOST     Override auto-detected host IP for sandbox→host bridge
    FLIGHT_APP_PORT        FastAPI port (default 18890)
    SKIP_SYSTEMD_TUNNEL    Set to 1 to skip systemd-user tunnel install
    FLIGHT_ADSB_SOURCE     Live-aircraft source: community (default) | opensky
    OPENSKY_EGRESS_PROXY   Route OpenSky's outbound calls through a non-datacenter
                           hop so a cloud VM can reach OpenSky despite its IP block
                           (e.g. http://127.0.0.1:8080 or socks5h://127.0.0.1:1080).
                           Only used with FLIGHT_ADSB_SOURCE=opensky.
    ENABLE_BREV_FORWARD    Set to 1 to install the public 0.0.0.0 Brev forward
    BREV_FORWARD_PORT      Public port for the Brev forward (default 18891)

EOF
  exit 0
}

# ── 0a. Parse args ──────────────────────────────────────────────────────
SANDBOX_NAME=""
DO_STATUS=false
DO_UNINSTALL=false
FORCE_UPDATE_CREDS=false

while [ $# -gt 0 ]; do
  case "$1" in
    --status)        DO_STATUS=true;        shift ;;
    --uninstall)     DO_UNINSTALL=true;     shift ;;
    --update-creds)  FORCE_UPDATE_CREDS=true; shift ;;
    --skip-systemd)  SKIP_SYSTEMD_TUNNEL=1; shift ;;
    --brev-link)
      ENABLE_BREV_FORWARD=1
      # Optional numeric port may follow (e.g. --brev-link 18895).
      case "${2:-}" in
        ''|--*) ;;
        *[!0-9]*) fail "--brev-link port must be numeric: $2" ;;
        *) BREV_FORWARD_PORT="$2"; shift ;;
      esac
      shift ;;
    --port)          PORT="${2:?--port needs a value}"; shift 2 ;;
    --opensky-port)  OPENSKY_PROXY_PORT="${2:?--opensky-port needs a value}"; shift 2 ;;
    --faa-port)      FAA_PROXY_PORT="${2:?--faa-port needs a value}"; shift 2 ;;
    -h|--help)       usage_exit ;;
    -*)              fail "Unknown option: $1 (try --help)" ;;
    *)
      if [ -z "$SANDBOX_NAME" ]; then SANDBOX_NAME="$1"; shift
      else fail "Unknown argument: $1"; fi ;;
  esac
done

# Sandbox name from positional, env, or sandboxes.json default.
if [ -z "$SANDBOX_NAME" ]; then
  SANDBOX_NAME="${OPENSHELL_SANDBOX:-}"
fi
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
if [ -z "$SANDBOX_NAME" ] && [ "$DO_STATUS" != true ]; then
  printf "  Sandbox name: "
  read -r SANDBOX_NAME
fi

# ── 0b. Path detection ──────────────────────────────────────────────────
# Run a SINGLE round-trip into the sandbox to figure out which layout is
# in play. Result is cached in the LAYOUT/SKILLS_BASE/OPENCLAW_AGENT_HOME/
# OPENCLAW_JSON globals.
#
# Decision logic:
#   * /sandbox/.openclaw/openclaw.json exists  → new layout (canonical)
#   * /sandbox/.openclaw-data/agents/ exists   → legacy layout
#   * neither                                  → assume new (best-guess
#     for fresh-onboard sandboxes; harmless if openclaw.json is absent
#     — the registry-update step no-ops with a warning)
detect_paths() {
  if [ -z "$SANDBOX_NAME" ] || [ -z "$OPENSHELL_BIN" ]; then
    LAYOUT="new"
    SKILLS_BASE="/sandbox/.openclaw/skills"
    OPENCLAW_AGENT_HOME="/sandbox/.openclaw/agents/main"
    OPENCLAW_JSON="/sandbox/.openclaw/openclaw.json"
    return 0
  fi
  local probe
  probe=$(ssh_sandbox '
    if [ -f /sandbox/.openclaw/openclaw.json ]; then echo new
    elif [ -d /sandbox/.openclaw-data/agents ]; then echo legacy
    elif [ -d /sandbox/.openclaw/agents ]; then echo new
    else echo unknown
    fi' 2>/dev/null || echo unknown)
  case "$probe" in
    new)
      LAYOUT="new"
      SKILLS_BASE="/sandbox/.openclaw/skills"
      OPENCLAW_AGENT_HOME="/sandbox/.openclaw/agents/main"
      OPENCLAW_JSON="/sandbox/.openclaw/openclaw.json"
      ;;
    legacy)
      LAYOUT="legacy"
      SKILLS_BASE="/sandbox/.openclaw-data/skills"
      OPENCLAW_AGENT_HOME="/sandbox/.openclaw-data/agents/main"
      OPENCLAW_JSON=""
      ;;
    *)
      # Sandbox unreachable / not yet onboarded. Default to new but warn.
      LAYOUT="new"
      SKILLS_BASE="/sandbox/.openclaw/skills"
      OPENCLAW_AGENT_HOME="/sandbox/.openclaw/agents/main"
      OPENCLAW_JSON="/sandbox/.openclaw/openclaw.json"
      ;;
  esac
}

# ── 0c. openclaw.json mutation (new layout only) ────────────────────────
# Enables flight-tracking in the skill registry and ensures
# tools.profile=coding so the agent gets the exec/read tools surfaced in
# the system prompt. Without tools.profile=coding the agent never sees
# exec and "spins out" looking for the right tool to call.
#
# Idempotent. No-op on legacy layouts that don't carry openclaw.json.
configure_openclaw_json() {
  [ -z "$OPENCLAW_JSON" ] && return 0
  if ! ssh_sandbox "[ -f $OPENCLAW_JSON ]" 2>/dev/null; then
    warn "$OPENCLAW_JSON not found; skipping skill-registry + tools-profile update"
    return 0
  fi
  local result
  result=$(ssh_sandbox "python3 - <<'PYEOF'
import json
p = '$OPENCLAW_JSON'
d = json.load(open(p))
changed = False

entry = d.setdefault('skills', {}).setdefault('entries', {}).setdefault('flight-tracking', {})
if entry.get('enabled') is not True:
    entry['enabled'] = True
    changed = True

tools = d.setdefault('tools', {})
if tools.get('profile') is None:
    tools['profile'] = 'coding'
    changed = True
elif tools.get('profile') != 'coding':
    print('warn-profile:' + str(tools.get('profile')))

if changed:
    json.dump(d, open(p, 'w'), indent=2)
    print('updated')
else:
    print('already configured')
PYEOF" 2>/dev/null || echo error)
  case "$result" in
    updated)            ok "openclaw.json: enabled flight-tracking + tools.profile=coding" ;;
    *already*)          ok "openclaw.json: flight-tracking + tools.profile already set" ;;
    *warn-profile:*)    warn "openclaw.json: tools.profile is '${result##*:}' (expected 'coding'); leaving as-is" ;;
    *)                  warn "openclaw.json mutation failed; agent may not see flight-tracking. Run with --status to verify." ;;
  esac
}

# ── --status mode ───────────────────────────────────────────────────────
if [ "$DO_STATUS" = true ]; then
  echo
  echo -e "${CYAN}  FlightOps Integration — status${NC}"
  echo
  if [ -f "$INSTALL_DIR/config.env" ]; then
    # shellcheck disable=SC1091
    . "$INSTALL_DIR/config.env"
    ok "Installed"
    echo "    Sandbox:        ${FLIGHT_SANDBOX:-unknown}"
    echo "    Layout:         ${FLIGHT_LAYOUT:-unknown}"
    echo "    Skills:         ${FLIGHT_SKILLS_BASE:-unknown}/flight-tracking/"
    echo "    Agent home:     ${FLIGHT_OPENCLAW_AGENT_HOME:-unknown}"
    echo "    Host IP:        ${FLIGHT_HOST_IP:-unknown}"
    echo "    App port:       ${FLIGHT_PORT:-unknown}"
    echo "    OpenSky proxy:  ${FLIGHT_OPENSKY_PROXY_PORT:-unknown}"
    echo "    Live data src:  ${FLIGHT_ADSB_SOURCE:-community}"
    echo "    FAA proxy:      ${FLIGHT_FAA_PROXY_PORT:-unknown}"
    echo "    Installed at:   ${FLIGHT_INSTALLED_AT:-unknown}"
  else
    warn "Not installed (no $INSTALL_DIR/config.env)"
  fi
  echo
  OS_PID=$(pgrep -f "python3.*opensky-proxy\.py" 2>/dev/null | head -1 || true)
  FA_PID=$(pgrep -f "python3.*faa-proxy\.py" 2>/dev/null | head -1 || true)
  [ -n "$OS_PID" ] && ok "opensky-proxy running (PID $OS_PID)" || warn "opensky-proxy NOT running"
  [ -n "$FA_PID" ] && ok "faa-proxy running (PID $FA_PID)"     || warn "faa-proxy NOT running"
  if curl -fsS -o /dev/null --max-time 2 \
        "http://127.0.0.1:${OPENSKY_PROXY_PORT}/health" 2>/dev/null; then
    ok "opensky-proxy /health passed"
  else
    warn "opensky-proxy /health failed"
  fi
  if curl -fsS -o /dev/null --max-time 2 \
        "http://127.0.0.1:${FAA_PROXY_PORT}/health" 2>/dev/null; then
    ok "faa-proxy /health passed"
  else
    warn "faa-proxy /health failed"
  fi
  echo
  if [ "${FLIGHT_ADSB_SOURCE:-community}" = "community" ]; then
    ok "Live data via community ADS-B feeds — no OpenSky credentials required"
  elif [ -f "$CREDS_PATH" ]; then
    HAS=$(python3 -c "import json; d=json.load(open('$CREDS_PATH')); print('yes' if (d.get('OPENSKY_CLIENT_ID') and d.get('OPENSKY_CLIENT_SECRET')) else 'no')" 2>/dev/null || echo no)
    [ "$HAS" = "yes" ] && ok "OPENSKY_CLIENT_ID/SECRET present in $CREDS_PATH" \
                       || warn "OPENSKY OAuth2 creds missing from $CREDS_PATH (anonymous fallback)"
  else
    warn "$CREDS_PATH does not exist"
  fi
  echo
  # Systemd tunnel state if installed.
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl --user list-unit-files flight-tunnel.service 2>/dev/null \
       | grep -q '^flight-tunnel.service'; then
    if systemctl --user is-active --quiet flight-tunnel.service 2>/dev/null; then
      ok "flight-tunnel.service active (loopback forward up)"
    else
      warn "flight-tunnel.service installed but NOT active"
    fi
  fi
  # Public Brev forward state (only if it was installed).
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl --user list-unit-files flight-brev-forward.service 2>/dev/null \
       | grep -q '^flight-brev-forward.service'; then
    if systemctl --user is-active --quiet flight-brev-forward.service 2>/dev/null; then
      ok "flight-brev-forward.service active (0.0.0.0:${FLIGHT_BREV_FORWARD_PORT:-18891} — Brev secure link)"
    else
      warn "flight-brev-forward.service installed but NOT active"
    fi
  fi
  # Browser-reachable backend?
  HEALTH=$(curl -fsSL --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)
  if [ -n "$HEALTH" ]; then
    ok "Backend reachable on http://127.0.0.1:${PORT}/api/health"
  else
    warn "Backend not reachable on http://127.0.0.1:${PORT}/api/health"
  fi
  echo
  exit 0
fi

[ -z "$OPENSHELL_BIN" ] && fail "openshell CLI not found. Is NemoClaw installed?"
[ -z "$SANDBOX_NAME" ] && fail "No sandbox name provided. Usage: ./install.sh <sandbox-name>"

# ── --uninstall mode ────────────────────────────────────────────────────
if [ "$DO_UNINSTALL" = true ]; then
  echo
  echo -e "${CYAN}  Removing FlightOps Integration from $SANDBOX_NAME…${NC}"
  echo
  detect_paths

  # 1. Stop the systemd-user tunnel.
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl --user list-unit-files flight-tunnel.service 2>/dev/null \
       | grep -q '^flight-tunnel.service'; then
    info "Stopping flight-tunnel.service…"
    systemctl --user stop flight-tunnel.service 2>/dev/null || true
    systemctl --user disable flight-tunnel.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/flight-tunnel.service"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "Tunnel unit removed"
  fi
  # Public Brev forward unit (if it was installed).
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl --user list-unit-files flight-brev-forward.service 2>/dev/null \
       | grep -q '^flight-brev-forward.service'; then
    info "Stopping flight-brev-forward.service…"
    systemctl --user stop flight-brev-forward.service 2>/dev/null || true
    systemctl --user disable flight-brev-forward.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/flight-brev-forward.service"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "Brev forward unit removed"
  fi
  openshell forward stop "$PORT" >/dev/null 2>&1 || true
  # Best-effort: stop any non-persistent Brev forward fallback too.
  if [ -f "$INSTALL_DIR/config.env" ]; then
    # shellcheck disable=SC1091
    . "$INSTALL_DIR/config.env" 2>/dev/null || true
    [ -n "${FLIGHT_BREV_FORWARD_PORT:-}" ] && \
      openshell forward stop "$FLIGHT_BREV_FORWARD_PORT" >/dev/null 2>&1 || true
  fi

  # 2. Stop both host-side proxies.
  for pat in "python3.*opensky-proxy\.py" "python3.*faa-proxy\.py"; do
    PIDS=$(pgrep -f "$pat" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
      info "Stopping ${pat##*\.\*} ($PIDS)…"
      echo "$PIDS" | xargs -r kill 2>/dev/null || true
      sleep 1
      pgrep -f "$pat" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    fi
  done
  ok "Host proxies stopped"

  # 3. Stop the uvicorn server in the sandbox + remove staged files.
  ssh_sandbox 'p="'"$PORT"'"; for pd in /proc/[0-9]*; do pid=$(basename "$pd"); [ -r "$pd/cmdline" ] || continue; cmd=$(tr "\0" " " < "$pd/cmdline" 2>/dev/null); case "$cmd" in *uvicorn*server:app*) case "$cmd" in *"--port $p "*) kill -9 "$pid" 2>/dev/null || true ;; esac ;; esac; done; true' 2>/dev/null || true
  ssh_sandbox "rm -rf $SANDBOX_BASE" 2>/dev/null && ok "Removed $SANDBOX_BASE" || warn "Could not remove $SANDBOX_BASE"
  ssh_sandbox "rm -rf $SKILLS_BASE/flight-tracking" 2>/dev/null && ok "Removed $SKILLS_BASE/flight-tracking" || warn "Could not remove skill dir"

  # 4. Disable in openclaw.json registry (new layout only).
  if [ -n "$OPENCLAW_JSON" ] && ssh_sandbox "[ -f $OPENCLAW_JSON ]" 2>/dev/null; then
    ssh_sandbox "python3 - <<'PYEOF'
import json
p = '$OPENCLAW_JSON'
d = json.load(open(p))
e = d.get('skills', {}).get('entries', {})
if 'flight-tracking' in e:
    e['flight-tracking']['enabled'] = False
    json.dump(d, open(p, 'w'), indent=2)
    print('disabled')
else:
    print('absent')
PYEOF" >/dev/null 2>&1 || true
    ok "Disabled flight-tracking in openclaw.json"
  fi

  # 5. Drop the policy block.
  if openshell policy get "$SANDBOX_NAME" --full 2>/dev/null \
       | grep -q "flight_tracking_opensky"; then
    info "Dropping flight_tracking_opensky from sandbox policy…"
    POL=$(mktemp /tmp/flight-policy-XXXX.yaml)
    openshell policy get "$SANDBOX_NAME" --full 2>/dev/null \
      | sed '1,/^---$/d' \
      | python3 -c "
import sys, yaml
d = yaml.safe_load(sys.stdin) or {}
nps = d.get('network_policies') or {}
nps.pop('flight_tracking_opensky', None)
d['network_policies'] = nps
print(yaml.safe_dump(d, sort_keys=False))" > "$POL"
    openshell policy set "$SANDBOX_NAME" --policy "$POL" --wait >/dev/null 2>&1 \
      && ok "Policy block removed" \
      || warn "policy set failed; review $POL"
    rm -f "$POL"
  fi

  # 6. Drop the local install marker.
  rm -rf "$INSTALL_DIR"
  ok "Removed $INSTALL_DIR"

  echo
  echo "  FlightOps uninstalled."
  echo "  Re-run ./install.sh $SANDBOX_NAME to reinstall."
  echo
  exit 0
fi

# ── Banner (install path) ───────────────────────────────────────────────
cat <<EOF

  ╔════════════════════════════════════════════════════════════╗
  ║  FlightOps — Flight Tracking Integration installer        ║
  ║  Live aircraft on a deck.gl + MapLibre console           ║
  ╚════════════════════════════════════════════════════════════╝

EOF

info "Target sandbox: $SANDBOX_NAME"

# ── 1. Prerequisites ────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || fail "python3 not found on host"
$OPENSHELL_BIN sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME" \
  || fail "Sandbox '$SANDBOX_NAME' not found. Run 'nemoclaw onboard' first."
ok "Prerequisites OK"

info "Detecting OpenClaw sandbox layout…"
detect_paths
ok "Layout: $LAYOUT"
ok "  skills:      $SKILLS_BASE"
ok "  agent home:  $OPENCLAW_AGENT_HOME"
[ -n "$OPENCLAW_JSON" ] && ok "  openclaw.json: $OPENCLAW_JSON" \
                        || ok "  openclaw.json: (legacy layout — registry update skipped)"

# ── 2. Resolve OpenSky creds — host-canonical via ~/.nemoclaw/credentials.json ─
#
# Source of truth is ~/.nemoclaw/credentials.json on the HOST. We mirror
# whatever lands here into:
#   1. the openshell provider `flight-tracking-opensky` (gateway-side
#      canonical record, used by future credential-injection paths and
#      makes rotations trivial: edit credentials.json + re-run install.sh)
#   2. flight.env — proxy URLs only, NO secrets.
#
# Detect-or-prompt UX:
#   * Both keys present in credentials.json → ask use existing / replace.
#   * Either missing → prompt for the missing values and persist.
#   * `OPENSKY_CLIENT_ID=… ./install.sh` env override still wins (for CI).
#   * `--update-creds` skips the "use existing" question.
#
# Chat & inference auth route through OpenClaw via `openclaw agent --json`,
# so we don't need any inference key of our own.
ok "Chat will route through OpenClaw (\`openclaw agent --json\`)."
ok "OpenClaw already owns inference auth via the gateway-managed route."

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
  read -rs OPENSKY_CLIENT_SECRET
  printf "\n"
  if [ -z "$OPENSKY_CLIENT_ID" ] || [ -z "$OPENSKY_CLIENT_SECRET" ]; then
    warn "Both values are required for OAuth2 (~4,000 credits/day)."
    OPENSKY_CLIENT_ID=""
    OPENSKY_CLIENT_SECRET=""
  fi
}

# ── Live-feed onboarding wizard ─────────────────────────────────────────────
# When the operator hasn't pinned a source (no FLIGHT_ADSB_SOURCE env/flag)
# and we're on an interactive TTY, offer a friendly choice. Whatever they
# pick, the host proxy ALWAYS auto-falls-back to the community feeds if
# OpenSky is unreachable — so a hackathon attendee can never end up with a
# blank map. Non-interactive / piped installs keep the safe community default.
if [ "$FLIGHT_ADSB_SOURCE_EXPLICIT" != "1" ] && [ -t 0 ]; then
  echo
  info "Choose the live-aircraft data feed:"
  printf "      1) Community feeds  — no account, works from cloud/VM  (recommended)\n"
  printf "      2) OpenSky          — enter credentials (full history; often blocked\n"
  printf "                            from cloud IPs → auto-falls back to community)\n"
  printf "    Selection [1]: "
  read -r _feed_choice
  case "${_feed_choice:-1}" in
    2) FLIGHT_ADSB_SOURCE="opensky"
       ok "Selected OpenSky (with automatic community fallback if unreachable)." ;;
    *) FLIGHT_ADSB_SOURCE="community"
       ok "Selected community feeds — no credentials needed." ;;
  esac
fi

if [ "$FLIGHT_ADSB_SOURCE" = "community" ]; then
  echo
  ok "Live-aircraft data source: community ADS-B feeds (adsb.fi / airplanes.live / adsb.lol)"
  info "No OpenSky account or credentials are required in community mode."
  info "These feeds are reachable from cloud/VM IPs, unlike opensky-network.org."
  info "To use OpenSky instead, re-run with FLIGHT_ADSB_SOURCE=opensky (non-cloud hosts only)."
  # Not used in community mode; leave any existing credentials.json untouched.
  OPENSKY_CLIENT_ID=""; OPENSKY_CLIENT_SECRET=""
elif [ -n "$OPENSKY_CLIENT_ID" ] && [ -n "$OPENSKY_CLIENT_SECRET" ]; then
  echo
  ok "OpenSky credentials found in $CREDS_PATH"
  printf "    OPENSKY_CLIENT_ID     = %s\n" "$(mask "$OPENSKY_CLIENT_ID")"
  printf "    OPENSKY_CLIENT_SECRET = %s\n" "$(mask "$OPENSKY_CLIENT_SECRET")"
  if [ "$FORCE_UPDATE_CREDS" = true ]; then
    info "Enter new OpenSky OAuth2 credentials (--update-creds):"
    prompt_for_creds
  elif [ -t 0 ]; then
    printf "    Use existing, [r]eplace, or [s]kip OpenSky upgrade? [U/r/s] "
    read -r answer
    case "${answer:-U}" in
      r|R) info "Enter new OpenSky OAuth2 credentials:"; prompt_for_creds ;;
      s|S) warn "Skipping OAuth2 — server will fall back to anonymous (~400 credits/day)."
           OPENSKY_CLIENT_ID=""; OPENSKY_CLIENT_SECRET="" ;;
      *)   ok "Using existing credentials" ;;
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

# Persist new / changed credentials into credentials.json (single source
# of truth). Atomic write, 0600 permissions, only touches OpenSky keys
# (other tools' creds in the same file are left exactly as found).
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
  if $OPENSHELL_BIN provider get flight-tracking-opensky >/dev/null 2>&1; then
    info "Updating openshell provider 'flight-tracking-opensky'…"
    $OPENSHELL_BIN provider update flight-tracking-opensky \
      --credential "OPENSKY_CLIENT_ID=$OPENSKY_CLIENT_ID" \
      --credential "OPENSKY_CLIENT_SECRET=$OPENSKY_CLIENT_SECRET" >/dev/null 2>&1 \
      && ok "Provider 'flight-tracking-opensky' refreshed" \
      || warn "Provider update failed; gateway record may be stale"
  else
    info "Registering openshell provider 'flight-tracking-opensky'…"
    $OPENSHELL_BIN provider create \
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
#   * a credentials.json edit takes effect immediately
#   * a new code revision of opensky-proxy.py is picked up
#   * we reset any stuck state from a previous run
echo
info "Starting host-side opensky-proxy on 0.0.0.0:$OPENSKY_PROXY_PORT (data source: $FLIGHT_ADSB_SOURCE)…"

# Best-effort kill of any prior copy of the daemon. Match the python
# command line rather than relying on a pidfile so a stale pidfile from
# a crashed previous run can't block us.
EXISTING_PID=$(pgrep -f "python3.*opensky-proxy\.py" 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
  info "Stopping existing opensky-proxy (PID $EXISTING_PID)…"
  kill "$EXISTING_PID" 2>/dev/null || true
  sleep 1
  pgrep -f "python3.*opensky-proxy\.py" 2>/dev/null \
    | xargs -r kill -9 2>/dev/null || true
fi

# `setsid nohup` so the daemon survives the install.sh shell exit and
# so it gets its own session (won't be orphaned to the systemd reaper).
mkdir -p "$(dirname /tmp/opensky-proxy.log)" 2>/dev/null || true
# OPENSKY_EGRESS_PROXY (optional): route OpenSky's outbound calls through a
# non-datacenter hop so a cloud VM can reach OpenSky despite the IP block.
# Only meaningful with FLIGHT_ADSB_SOURCE=opensky; harmless otherwise.
setsid nohup env "FLIGHT_ADSB_SOURCE=$FLIGHT_ADSB_SOURCE" \
    "OPENSKY_EGRESS_PROXY=${OPENSKY_EGRESS_PROXY:-}" \
    python3 "$SCRIPT_DIR/opensky-proxy.py" \
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
$OPENSHELL_BIN policy get "$SANDBOX_NAME" --full 2>/dev/null | sed '1,/^---$/d' > "$POLICY_FILE"

# Idempotent upsert. We REPLACE the flight_tracking_opensky block on
# every install (not just append) so host-IP changes between runs are
# applied cleanly. We DROP opensky-network.org and
# auth.opensky-network.org from the policy — under Tier-1 the sandbox
# must reach OpenSky only via the host proxy.
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
        # PyPI access for venv builds (FastAPI / uvicorn / httpx /
        # pydantic). Needed on the first install per sandbox; once the
        # venv is built it never gets used again (we don't run
        # `pip install` on subsequent restarts). `/**` glob = multi-
        # segment, because pip fetches /simple/<pkg>/ then
        # /packages/<a>/<b>/<c>/<wheel>. Scoped to GET so the sandbox
        # can read but not publish.
        {
            'host': 'pypi.org', 'port': 443, 'protocol': 'rest',
            'tls': 'terminate', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'GET', 'path': '/simple/**'}},
                {'allow': {'method': 'GET', 'path': '/pypi/**'}},
            ],
        },
        {
            'host': 'files.pythonhosted.org', 'port': 443, 'protocol': 'rest',
            'tls': 'terminate', 'enforcement': 'enforce',
            'rules': [
                {'allow': {'method': 'GET', 'path': '/packages/**'}},
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
  $OPENSHELL_BIN policy set "$SANDBOX_NAME" --policy "$POLICY_FILE" --wait 2>&1 \
    && ok "Policy applied (host-proxy only — direct OpenSky access removed)" \
    || fail "openshell policy set failed; review $POLICY_FILE"
fi
rm -f "$POLICY_FILE"

# ── 6. Stage server files inside the sandbox ────────────────────────────
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
upload "$SCRIPT_DIR/app/static/favicon.svg"                            "$SANDBOX_BASE/app/static/favicon.svg"
upload "$SCRIPT_DIR/app/static/data/airports.json"                     "$SANDBOX_BASE/app/static/data/airports.json"
upload "$SCRIPT_DIR/start.sh"                                          "$SANDBOX_BASE/start.sh"
upload "$SCRIPT_DIR/skills/flight-tracking/SKILL.md"                   "$SKILLS_BASE/flight-tracking/SKILL.md"
upload "$SCRIPT_DIR/skills/flight-tracking/scripts/fly"                "$SKILLS_BASE/flight-tracking/scripts/fly"

ssh_sandbox "chmod +x $SANDBOX_BASE/start.sh $SKILLS_BASE/flight-tracking/scripts/fly" 2>/dev/null
ok "Files staged"

# ── 6b. Update openclaw.json registry + tools.profile (new layout only) ─
configure_openclaw_json

# ── 7. flight.env (Tier-1: zero secrets in sandbox) ─────────────────────
#
# Carries proxy URLs + the detected OPENCLAW_AGENT_HOME so server.py
# reads sessions/JSONL from the right place on each layout. NO OpenSky
# secrets — those stay on the host inside opensky-proxy.py.
info "Writing flight.env (zero OpenSky secrets — Tier-1)…"

ssh_sandbox "cat > $SANDBOX_BASE/flight.env" <<EOF
# Auto-generated by install.sh — DO NOT EDIT BY HAND.
# Tier-1 architecture: OpenSky credentials live ONLY on the host at
# ~/.nemoclaw/credentials.json. The host-side opensky-proxy.py
# (PID via \`pgrep -f opensky-proxy\` on the host) injects the
# Bearer token; this sandbox never sees the secret.
# Rotate by editing credentials.json on the host then re-running install.sh.
OPENSKY_PROXY_URL=$OPENSKY_PROXY_URL
FAA_PROXY_URL=$FAA_PROXY_URL
FLIGHT_APP_PORT=$PORT
OPENCLAW_AGENT=main
OPENCLAW_AGENT_HOME=$OPENCLAW_AGENT_HOME
OPENCLAW_TIMEOUT_S=180
EOF
ssh_sandbox "chmod 600 $SANDBOX_BASE/flight.env" 2>/dev/null
ok "flight.env written (proxy URLs + agent home — no secrets)"

# ── 8. Build venv + install deps inside the sandbox ─────────────────────
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

# ── 9. (Re)start the server inside the sandbox ──────────────────────────
info "Starting FlightOps server inside the sandbox on port $PORT…"

# Two separate one-line ssh calls. Multi-line heredoc invocations of
# ssh_sandbox here were causing the SSH session to hang waiting on
# fd cleanup of the disowned background uvicorn — single-line form
# returns in ~3s instead. The minimal sandbox image strips pkill/
# pgrep and restricts plain `ps` to our own session, so the kill
# step walks /proc directly (always available, always sees our own
# UID's processes regardless of session).

# 9a — kill any prior uvicorn serving this app. Belt-and-braces: kill by
# port (fuser, shared network view) AND by /proc scan, so a stale build
# from a prior session can't keep the port and silently serve old code.
ssh_sandbox 'p="'"$PORT"'"; (command -v fuser >/dev/null 2>&1 && fuser -k "${p}/tcp" 2>/dev/null) || true; for pd in /proc/[0-9]*; do pid=$(basename "$pd"); [ -r "$pd/cmdline" ] || continue; cmd=$(tr "\0" " " < "$pd/cmdline" 2>/dev/null); case "$cmd" in *uvicorn*server:app*) case "$cmd" in *"--port $p "*) kill -9 "$pid" 2>/dev/null || true ;; esac ;; esac; done; sleep 1; true' \
  || true

# 9b — truncate the log + launch start.sh detached. Inline one-
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

# ── 9c. Install / refresh the systemd-user tunnel unit ──────────────────
TUNNEL_TEMPLATE="$SCRIPT_DIR/scripts/systemd/flight-tunnel.service.template"
TUNNEL_UNIT_DIR="$HOME/.config/systemd/user"
TUNNEL_UNIT="$TUNNEL_UNIT_DIR/flight-tunnel.service"

systemd_user_available() {
  # A user-bus has to be REACHABLE, not just present in PATH. On
  # headless / ssh-only sessions (Brev VMs, build images, MCP shells)
  # systemctl exists but the user manager bus is gone, so
  # `systemctl --user list-units` succeeds while `daemon-reload` /
  # `enable` exits non-zero. Probe with a no-side-effect call first so
  # we fall through to `openshell forward` cleanly instead of `set -e`-
  # killing the installer mid-step.
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user --version >/dev/null 2>&1 || return 1
  systemctl --user list-units --no-legend --no-pager >/dev/null 2>&1
}

if [ "$SKIP_SYSTEMD_TUNNEL" = "1" ]; then
  info "Skipping systemd-user tunnel install (SKIP_SYSTEMD_TUNNEL=1)."
elif ! systemd_user_available; then
  info "systemd-user bus not reachable — will use \`openshell forward\` fallback."
elif [ ! -f "$TUNNEL_TEMPLATE" ]; then
  warn "Tunnel template missing at $TUNNEL_TEMPLATE — skipping."
else
  info "Installing systemd-user tunnel unit (flight-tunnel.service)…"
  mkdir -p "$TUNNEL_UNIT_DIR"
  TMP_UNIT=$(mktemp)
  sed -e "s|__SANDBOX_NAME__|$SANDBOX_NAME|g" \
      -e "s|__GATEWAY_NAME__|$GATEWAY_NAME|g" \
      -e "s|__APP_PORT__|$PORT|g" \
      -e "s|__HOME__|$HOME|g" \
      "$TUNNEL_TEMPLATE" > "$TMP_UNIT"
  if [ ! -f "$TUNNEL_UNIT" ] || ! cmp -s "$TMP_UNIT" "$TUNNEL_UNIT"; then
    mv "$TMP_UNIT" "$TUNNEL_UNIT"
    # daemon-reload can still fail on bus disconnect mid-install;
    # don't let set -e kill us — fall through to forward fallback.
    systemctl --user daemon-reload 2>/dev/null \
      && ok "Wrote $TUNNEL_UNIT" \
      || warn "daemon-reload failed; unit written but not loaded"
  else
    rm -f "$TMP_UNIT"
    ok "Tunnel unit already up to date"
  fi
  systemctl --user enable flight-tunnel.service >/dev/null 2>&1 \
    && ok "Tunnel unit enabled (sandbox=$SANDBOX_NAME, gateway=$GATEWAY_NAME, port=$PORT)" \
    || warn "Could not enable flight-tunnel.service — falling back to \`openshell forward\`."
  # Enable linger so the user manager (and thus these forwards) starts at
  # boot on headless VMs where nobody logs in interactively. Best-effort:
  # try unprivileged first, then sudo -n; a failure only means the unit
  # won't auto-start until the next login.
  if [ "$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    if loginctl enable-linger "$(id -un)" >/dev/null 2>&1 \
       || sudo -n loginctl enable-linger "$(id -un)" >/dev/null 2>&1; then
      ok "Enabled systemd linger (forwards start on reboot)"
    else
      warn "Could not enable systemd linger — forwards may not start until next login."
      warn "  Fix with: sudo loginctl enable-linger $(id -un)"
    fi
  fi
fi

# ── 10. Host-side port forward ──────────────────────────────────────────
info "Forwarding localhost:$PORT to the sandbox…"

verify_forward() {
  curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PORT/api/health"
}

forward_ok=false

# (a) systemd-user unit if installed.
if command -v systemctl >/dev/null 2>&1 \
   && systemctl --user list-unit-files flight-tunnel.service \
        2>/dev/null | grep -q '^flight-tunnel.service'; then
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
  $OPENSHELL_BIN forward stop "$PORT" >/dev/null 2>&1 || true
  $OPENSHELL_BIN forward start "$PORT" "$SANDBOX_NAME" -d >/dev/null 2>&1 || true
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

# ── 10b. Optional public Brev forward (0.0.0.0, reboot-persistent) ───────
# A SECOND forward, bound to all interfaces, so Brev can publish the
# console as a secure link. Kept separate from the loopback tunnel above
# so local/Cursor access (127.0.0.1:$PORT) is never disturbed.
BREV_FORWARD_TEMPLATE="$SCRIPT_DIR/scripts/systemd/flight-brev-forward.service.template"
BREV_FORWARD_UNIT="$TUNNEL_UNIT_DIR/flight-brev-forward.service"
if [ "$ENABLE_BREV_FORWARD" = "1" ]; then
  echo
  if [ "$BREV_FORWARD_PORT" = "$PORT" ]; then
    warn "--brev-link port ($BREV_FORWARD_PORT) must differ from the app port ($PORT); skipping Brev forward."
  else
    info "Installing public Brev forward on 0.0.0.0:$BREV_FORWARD_PORT -> sandbox app ($PORT)…"
    brev_ok=false
    if systemd_user_available && [ -f "$BREV_FORWARD_TEMPLATE" ]; then
      mkdir -p "$TUNNEL_UNIT_DIR"
      TMP_BUNIT=$(mktemp)
      sed -e "s|__SANDBOX_NAME__|$SANDBOX_NAME|g" \
          -e "s|__GATEWAY_NAME__|$GATEWAY_NAME|g" \
          -e "s|__APP_PORT__|$PORT|g" \
          -e "s|__BREV_PORT__|$BREV_FORWARD_PORT|g" \
          -e "s|__HOME__|$HOME|g" \
          "$BREV_FORWARD_TEMPLATE" > "$TMP_BUNIT"
      if [ ! -f "$BREV_FORWARD_UNIT" ] || ! cmp -s "$TMP_BUNIT" "$BREV_FORWARD_UNIT"; then
        mv "$TMP_BUNIT" "$BREV_FORWARD_UNIT"
        systemctl --user daemon-reload 2>/dev/null \
          && ok "Wrote $BREV_FORWARD_UNIT" \
          || warn "daemon-reload failed; unit written but not loaded"
      else
        rm -f "$TMP_BUNIT"
        ok "Brev forward unit already up to date"
      fi
      systemctl --user enable flight-brev-forward.service >/dev/null 2>&1 \
        && ok "Brev forward unit enabled (survives reboot)" \
        || warn "Could not enable flight-brev-forward.service"
      systemctl --user restart flight-brev-forward.service >/dev/null 2>&1 || true
      for _ in 1 2 3 4 5; do
        sleep 1
        if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$BREV_FORWARD_PORT/api/health"; then
          brev_ok=true; break
        fi
      done
    else
      warn "systemd-user not available or template missing — using non-persistent \`openshell forward\` fallback."
      $OPENSHELL_BIN forward stop "$BREV_FORWARD_PORT" >/dev/null 2>&1 || true
      setsid nohup "$OPENSHELL_BIN" forward service --target-port "$PORT" \
          --local "0.0.0.0:$BREV_FORWARD_PORT" "$SANDBOX_NAME" \
          > /tmp/flight-brev-forward.log 2>&1 < /dev/null &
      disown 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        sleep 1
        if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$BREV_FORWARD_PORT/api/health"; then
          brev_ok=true; break
        fi
      done
    fi
    if $brev_ok; then
      ok "Public Brev forward active on 0.0.0.0:$BREV_FORWARD_PORT"
      info "Publish port $BREV_FORWARD_PORT in the Brev console for a secure *.apps.run.brev.nvidia.com link."
    else
      warn "Brev forward not reachable on 0.0.0.0:$BREV_FORWARD_PORT — inspect:"
      warn "  systemctl --user status flight-brev-forward.service"
      warn "  (or /tmp/flight-brev-forward.log if using the fallback)"
    fi
  fi
else
  info "Public Brev forward disabled. Enable a reboot-persistent secure-link forward with:"
  info "  ./install.sh $SANDBOX_NAME --brev-link            # default port 18891"
fi

# ── 11. Refresh agent sessions so the skill is picked up ────────────────
# Layout-aware: clear whichever sessions.json the agent home actually
# carries. No-op on a fresh install (file doesn't exist yet).
SESSIONS_PATH="$OPENCLAW_AGENT_HOME/sessions/sessions.json"
ssh_sandbox "[ -f $SESSIONS_PATH ] && echo '{}' > $SESSIONS_PATH || true" 2>/dev/null \
  && ok "Agent sessions cleared (skill will load on next message)"

# ── 12. Persist install marker so --status / --uninstall know what we set up ─
mkdir -p "$INSTALL_DIR"
cat > "$INSTALL_DIR/config.env" <<EOF
# Auto-generated by install.sh — used by --status / --uninstall.
FLIGHT_SANDBOX=$SANDBOX_NAME
FLIGHT_GATEWAY=$GATEWAY_NAME
FLIGHT_LAYOUT=$LAYOUT
FLIGHT_SKILLS_BASE=$SKILLS_BASE
FLIGHT_OPENCLAW_AGENT_HOME=$OPENCLAW_AGENT_HOME
FLIGHT_OPENCLAW_JSON=$OPENCLAW_JSON
FLIGHT_HOST_IP=$HOST_IP
FLIGHT_PORT=$PORT
FLIGHT_OPENSKY_PROXY_PORT=$OPENSKY_PROXY_PORT
FLIGHT_ADSB_SOURCE=$FLIGHT_ADSB_SOURCE
FLIGHT_ENABLE_BREV_FORWARD=$ENABLE_BREV_FORWARD
FLIGHT_BREV_FORWARD_PORT=$BREV_FORWARD_PORT
FLIGHT_FAA_PROXY_PORT=$FAA_PROXY_PORT
FLIGHT_OPENSHELL_BIN=$OPENSHELL_BIN
FLIGHT_INSTALLED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
ok "Install marker at $INSTALL_DIR/config.env"

# ── 13. Health check ────────────────────────────────────────────────────
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
  Skill:       $SKILLS_BASE/flight-tracking
  Agent home:  $OPENCLAW_AGENT_HOME
  Layout:      $LAYOUT
  Live data:   $FLIGHT_ADSB_SOURCE  (community = adsb.fi / airplanes.live / adsb.lol; no OpenSky creds needed)
  Helper:      \`fly\` CLI inside the sandbox (try: fly goto IAD)

  Secrets (Tier-1 host-proxy):
    canonical:    $CREDS_PATH        (host, chmod 600)
    opensky:      opensky-proxy.py @ http://${HOST_IP}:${OPENSKY_PROXY_PORT}  (host, /tmp/opensky-proxy.log)
    faa+awc:      faa-proxy.py     @ http://${HOST_IP}:${FAA_PROXY_PORT}     (host, /tmp/faa-proxy.log)
    gateway:      openshell provider 'flight-tracking-opensky'
    sandbox:      $SANDBOX_BASE/flight.env  (no secrets — only proxy URLs)
    rotate:       edit credentials.json → re-run ./install.sh

  Maintenance:
    Status:     ./install.sh --status
    Uninstall:  ./install.sh --uninstall

  Try in the chat panel on the dashboard:
    "Go to IAD and analyse traffic"
    "Show inbound arcs to JFK"
    "Any unusual squawks near LHR right now?"

EOF

if [ "$ENABLE_BREV_FORWARD" = "1" ] && [ "$BREV_FORWARD_PORT" != "$PORT" ]; then
  cat <<EOF
  Brev secure link:
    Public forward: 0.0.0.0:$BREV_FORWARD_PORT -> sandbox app ($PORT), reboot-persistent
    Unit:           systemctl --user status flight-brev-forward.service
    Next step:      publish port $BREV_FORWARD_PORT in the Brev console to get a
                    https://<name>.apps.run.brev.nvidia.com secure link.
    Note:           0.0.0.0 exposes the app on the VM network; the app has no auth
                    of its own — rely on Brev's secure link and remove with --uninstall.

EOF
fi
