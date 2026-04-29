# FlightOps — Flight Tracking Integration for NemoClaw

A live aircraft console you can drive from chat. Ask your NemoClaw agent
"go to IAD and analyse the traffic" and a deck.gl + MapLibre map running
inside the sandbox flies to Dulles, draws the live aircraft within 80 km,
and the agent summarises what's flying. Subscribe Telegram and the same
summary lands in your DM.

## What you get

- A modern dark-themed map (MapLibre vector tiles + deck.gl GPU layers).
- Live aircraft via [OpenSky Network](https://opensky-network.org/),
  refreshed every ~10 seconds, with rotated plane icons and animated
  trails for the selected flight.
- A curated dataset of ~125 globally significant airports (OpenFlights
  subset) with click-through detail panels.
- A FlightOps copilot pane wired to your Nemotron inference. It calls the
  same map-control API as the OpenClaw skill — chat or skill, both end up
  on the same surface.
- One-shot CLI helper `fly goto IAD`, `fly analyze JFK 100`, etc.
- A **Tier-1 host-side proxy** for OpenSky (the same pattern Planet
  uses): the sandbox can't reach `opensky-network.org` directly. A
  small Python daemon (`opensky-proxy.py`) runs on the host, holds the
  OAuth2 credentials, and forwards the sandbox's calls upstream with a
  Bearer token attached. The sandbox knows only the proxy URL — the
  `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` never enter the
  sandbox, so a sandbox compromise can't exfiltrate them. Tile
  downloads happen in your browser, not the sandbox.

## Repo layout

```
flight-tracking-integration/
├── app/
│   ├── server.py              # FastAPI: flight proxy, airports, chat, ws bus
│   ├── requirements.txt
│   └── static/
│       ├── index.html
│       ├── styles.css
│       ├── app.js             # MapLibre + deck.gl frontend
│       └── data/airports.json # OpenFlights subset (ODbL)
├── skills/flight-tracking/
│   ├── SKILL.md               # OpenClaw skill descriptor
│   └── scripts/fly            # bash CLI helper
├── scripts/systemd/
│   └── flight-tunnel.service.template  # rendered → ~/.config/systemd/user/
├── policy/flight-tracking.yaml
├── opensky-proxy.py           # HOST-side OAuth2-injecting proxy (Tier-1)
├── start.sh                   # sandbox-side launcher
├── install.sh                 # host-side installer
└── README.md
```

## Architecture: how OpenSky credentials stay out of the sandbox

```
  ┌──────────────────────────┐                ┌────────────────────────────┐
  │  HOST (your VM)          │                │  SANDBOX (openshell)       │
  │                          │                │                            │
  │  ~/.nemoclaw/            │                │  /sandbox/.openclaw-data/  │
  │   credentials.json       │                │   flight-tracking/         │
  │   (chmod 600, host only) │                │     flight.env             │
  │      │                   │                │       OPENSKY_PROXY_URL=…  │
  │      ▼                   │                │       (no secrets)         │
  │   opensky-proxy.py       │  HTTP    ┌─────┤                            │
  │   :9202                  │◀─────────┤     │  uvicorn server.py         │
  │   • mints OAuth2 token   │ Bearer   │     │   (binary: /usr/bin/       │
  │   • caches + refreshes   │ injected │     │    python3 only — policy   │
  │   • forwards to OpenSky  │          │     │    blocks everything else) │
  │      │                   │          │     │                            │
  │      ▼ Bearer            │          │     │   policy:                  │
  │   opensky-network.org    │          │     │   • <HOST>:9202 ALLOWED    │
  │                          │          │     │   • opensky-network.org    │
  │                          │          │     │     BLOCKED                │
  └──────────────────────────┘          │     └────────────────────────────┘
                                        │
            policy enforcement boundary ┘
```

To rotate the OpenSky key: edit `~/.nemoclaw/credentials.json` on the
host, re-run `./install.sh <sandbox>`. The sandbox is never touched
during rotation — the proxy reads creds at request time so the new
token is minted within seconds.

## Prerequisites

- **NemoClaw + OpenShell**: `nemoclaw onboard` already run; the gateway
  is registered (default name `nemoclaw` — set `OPENSHELL_GATEWAY=<name>`
  before `./install.sh` if you renamed it).
- **A running sandbox**: pass its name as `$1` to `install.sh`. The
  installer prints the available list if you forget.
- **Inference auth**: configured automatically by `nemoclaw onboard` —
  the installer reads `endpointUrl`, `model`, and `compatible_api_key`
  out of `~/.nemoclaw/credentials.json`.
- **Host CLI tools**: `openshell`, `python3`, `ssh`, `curl`. All
  standard on a Brev VM.
- **systemd-user (Linux only, recommended)**: enables the auto-recovering
  port-forward unit. Brev VMs and most modern Linux distros ship it. On
  macOS the installer skips this step automatically and falls back to
  `openshell forward start` (works, just less robust under gateway flaps).
- **Optional — OpenSky API client**:
  [Account ▸ API client ▸ "Create new"](https://opensky-network.org/manage-account).
  Anonymous use is rate-limited to ~400 credits/day; adding
  `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` to
  `~/.nemoclaw/credentials.json` lifts it to ~4,000/day via OAuth2
  client_credentials. (OpenSky removed Basic auth in March 2026.) When
  set, the **host-side proxy** in `opensky-proxy.py` holds the keys; the
  sandbox itself never sees them — see the architecture diagram above.

## Install

A clean clone on a fresh Brev VM only needs:

```bash
git clone <this repo>
cd flight-tracking-integration
./install.sh <sandbox-name>
```

The installer is idempotent — re-run it any time to pick up code changes
or rotate OpenSky keys. It does, in order:

1. Verifies the sandbox exists and inference auth is wired up.
2. Reads inference + (optional) OpenSky creds from
   `~/.nemoclaw/credentials.json`. If `OPENSKY_CLIENT_ID` /
   `OPENSKY_CLIENT_SECRET` are present, registers/refreshes them as an
   `openshell provider` named `flight-tracking-opensky` (rotation-safe).
3. Starts (or restarts) `opensky-proxy.py` on the host at
   `0.0.0.0:9202`. This is the daemon that holds the OpenSky bearer
   token; the sandbox calls it instead of `opensky-network.org`.
4. Detects the host VM's reachable IP and patches it into
   `policy/flight-tracking.yaml`, then applies the policy to the sandbox
   so it can reach the proxy and **nothing else** on the OpenSky side.
5. Uploads the app, the OpenClaw skill, and `start.sh` into
   `/sandbox/.openclaw-data/flight-tracking/`.
6. Builds a Python venv and installs deps **inside** the sandbox.
7. Writes `flight.env` inside the sandbox containing **only**
   `OPENSKY_PROXY_URL` — no client secrets.
8. (Re)starts `uvicorn server:app` on port `18890` inside the sandbox.
9. **Renders + installs the systemd-user tunnel** at
   `~/.config/systemd/user/flight-tunnel.service` (Linux only, skipped
   on macOS). This is the recovery layer described in
   *Troubleshooting* below.
10. Cycles the tunnel and verifies the host can reach the sandbox via
    `curl http://localhost:18890/api/health`. If the systemd path fails
    or isn't available, falls back to `openshell forward start`.
11. Clears agent sessions so the OpenClaw skill picks up any SKILL.md
    changes on the next chat turn.

Override knobs (export before `./install.sh`):

| var                     | default        | what it does                                           |
|-------------------------|----------------|--------------------------------------------------------|
| `OPENSHELL_GATEWAY`     | `nemoclaw`     | gateway name registered by `nemoclaw onboard`          |
| `FLIGHT_APP_PORT`       | `18890`        | host + sandbox port for the FastAPI                    |
| `OPENSKY_PROXY_PORT`    | `9202`         | host port the OpenSky proxy listens on                 |
| `SKIP_SYSTEMD_TUNNEL`   | `0`            | set `1` to skip the systemd-user unit install entirely |

## Use it

### From the browser

Open <http://localhost:18890>. Pan/zoom the map; the data refreshes for
the new bbox. Click any plane or airport to open the detail drawer. Use
the chat panel on the left rail.

### From the OpenClaw skill (chat / Telegram)

Once the sandbox sees the skill, your agent can drive the map for any
connected browser:

```
You: Go to LHR and show inbound arcs.
NemoClaw: Flying to LHR. 31 inbound aircraft within 80 km.
          Mix: 8 cruise / 19 descent / 4 climb. Top countries: GB, IE, DE.
          No notable squawks.
```

### From the command line (inside the sandbox)

```bash
fly goto IAD
fly analyze IAD 80
fly arcs JFK 120
fly highlight UAL123
fly layer trails on
fly health
```

### From Telegram

Whatever Telegram bridge you already have wired to NemoClaw (the
`telegram-integration-demo` setup) — the FlightOps skill plugs in
automatically because the SKILL.md has the right description for the
agent's selector.

## Architecture (one-liner)

```
Browser ──HTTP/WS──▶ openshell forward ──▶ sandbox:18890 (FastAPI)
                                             │
                                             ├── /api/flights ─▶ opensky-network.org (network policy: allow)
                                             ├── /api/chat    ─▶ Nemotron via openshell inference
                                             └── /ws/map       ◀── /api/map/* (skill / curl / chat)
```

Skill calls and chat tool-calls both land on the same `/api/map/*`
endpoints, which broadcast the resulting deck.gl command (`goto`,
`arcs`, `layer`, `highlight`) to every connected browser over the
WebSocket bus. That's why "go to IAD" from Telegram and "go to IAD"
typed into the left rail produce identical map behaviour.

## Security posture

- **OpenSky credentials never enter the sandbox.** `OPENSKY_CLIENT_ID` /
  `OPENSKY_CLIENT_SECRET` live only in `~/.nemoclaw/credentials.json`
  (chmod 600) and an `openshell provider` record on the gateway. The
  host-side `opensky-proxy.py` reads them at request time, mints a
  Bearer token, and forwards to opensky-network.org. The sandbox knows
  only the proxy URL, and the network policy forbids it from reaching
  `opensky-network.org` directly.
- **Network policy is the authoritative guardrail.** See
  `policy/flight-tracking.yaml` — outbound HTTPS is gated to a small
  allowlist (the host proxy, FAA AIS, AWC, adsbdb/hexdb, openfreemap
  tiles for the page itself). Trying to curl anything else from inside
  the sandbox returns "blocked by policy".
- **No public listener.** The sandbox-side server is reachable from
  the host *only* through the SSH-tunnel-via-`openshell` forward
  (either the systemd-user unit or `openshell forward start`). Nothing
  is bound to a public interface.
- **Inference auth.** The Nemotron `compatible_api_key` is rendered
  into `flight.env` so the sandbox-side FastAPI can call inference for
  the in-page chat panel. The OpenClaw chat path doesn't need it —
  that runs `openclaw agent --json` inside the sandbox, which inherits
  the gateway-managed inference route. To harden further, drop
  `INFERENCE_API_KEY` from `flight.env` and proxy inference through
  127.0.0.1 just like OpenSky.
- **Rotation.** Edit `~/.nemoclaw/credentials.json` and re-run
  `./install.sh`. The sandbox is never touched during rotation; the
  proxy reads creds at request time so the new token is minted in
  seconds.

## Troubleshooting

### `localhost:18890` refuses or hangs

This is almost always the host→sandbox port forward, not the FastAPI
itself. Check what state the tunnel's in:

```bash
# (Linux) Recommended — managed by systemd-user
systemctl --user status  flight-tunnel.service
systemctl --user restart flight-tunnel.service
journalctl --user -u flight-tunnel.service -f

# (Any host) Fallback path
openshell forward list
openshell forward stop  18890
openshell forward start 18890 <sandbox> -d
```

To verify end-to-end after a restart:

```bash
curl -s http://localhost:18890/api/health | head -c 300; echo
# Expect: {"ok":true,..."opensky_auth":"host-proxy",...}
```

If health is reachable but the map page is blank, hard-refresh the
browser (Cmd/Ctrl-Shift-R) — the WebSocket bus reconnects on its own,
but stale JS may still hold the old socket open.

### `opensky_auth: "anonymous"` instead of `"host-proxy"`

The sandbox's `flight.env` lost its `OPENSKY_PROXY_URL`, *or* the host
proxy isn't running. Recover with:

```bash
# Is the host proxy listening?
curl -s http://127.0.0.1:9202/health
ps aux | grep [o]pensky-proxy

# If missing, just re-run the installer — it'll relaunch it.
./install.sh <sandbox-name>
```

The proxy log lives at `/tmp/opensky-proxy.log` on the host.

### Server inside the sandbox didn't restart

`install.sh` walks `/proc` to kill any old `uvicorn server:app` before
starting the new one. If it fails (rare, only seen on stripped-down
sandbox images), do it manually:

```bash
ssh -F /dev/null \
    -o ProxyCommand="openshell ssh-proxy --gateway-name nemoclaw --name <sandbox>" \
    sandbox@openshell-<sandbox> \
    'pkill -9 -f "uvicorn server:app" || true'

# then re-run the installer
./install.sh <sandbox>
```

## Data

- **Aircraft:** OpenSky Network. Free under their
  [terms of use](https://opensky-network.org/about/terms-of-use); please
  cite them in any redistributable output. Subject to rate limits
  (10 s anonymous, 5 s authenticated).
- **Airports:** A 125-airport subset of
  [OpenFlights airports.dat](https://openflights.org/data.html), used
  under the ODbL. `static/data/airports.json` contains the full subset.
- **Tiles:** [openfreemap.org](https://openfreemap.org) — free,
  unauthenticated dark vector tiles. No API key required.

## Created by

[Tim Klawa](https://github.com/tklawa-nvidia/)
