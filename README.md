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
- A bundled OurAirports dataset (~11,000 airports) with click-through
  detail panels and a curated "globally significant" subset for the
  default zoom-out view.
- Public-feed overlays for FAA AIS airspace, TFRs, NAS Status,
  Aviation Weather Center METARs, and FAA ARTCC boundaries.
- A FlightOps copilot pane wired to your Nemotron inference. It calls
  the same map-control API as the OpenClaw skill — chat or skill, both
  end up on the same surface.
- One-shot CLI helper `fly goto IAD`, `fly analyze JFK 100`, etc.
- **Two host-side proxy daemons** so the sandbox never sees secrets or
  hits hosts that block its egress IP:
  - `opensky-proxy.py` — holds the OpenSky OAuth2 client and injects
    a Bearer token on every forwarded call.
  - `faa-proxy.py` — IP-rewraps `nasstatus.faa.gov` and
    `aviationweather.gov` (which 403 the cloud ASN the sandbox egresses
    through).
- **No MCP**: the agent uses OpenClaw's SKILL.md model. The skill
  descriptor in `skills/flight-tracking/SKILL.md` declares the tool
  surface in plain English; the agent shells out to a bash CLI (`fly`)
  or `curl http://127.0.0.1:18890/api/...` to drive it. See
  [Key concepts](#key-concepts-a-nemoclaw-tour) below.

## Key concepts (a NemoClaw tour)

If you're picking up NemoClaw via this demo, here is the mental model.
Each concept maps to a concrete artefact in this repo, so you can read
it as an annotated walkthrough.

### 1. The sandbox

Everything except secrets and the host proxies runs inside an
**openshell sandbox** — a Linux namespace launched by the gateway with
its own filesystem, its own egress firewall, and a restricted set of
binaries it is allowed to execute. The FastAPI app, the OpenClaw
agent, the bundled airport dataset, and the static frontend all live
in `/sandbox/.openclaw-data/flight-tracking/` inside the sandbox. The
host VM never executes any user code from this repo at runtime — only
the two proxy daemons run on the host.

You can shell into the sandbox with:

```bash
ssh openshell-<sandbox-name>     # uses ~/.ssh/config entry written by openshell
```

…and you'll find the sandbox can `curl 127.0.0.1:18890` (its own
FastAPI) but **cannot** `curl opensky-network.org` — the egress
firewall returns "blocked by policy".

### 2. Network policy is the authoritative guardrail

`policy/flight-tracking.yaml` declares an **allowlist** of (host, port,
HTTP method, path-prefix) tuples the sandbox is allowed to reach.
Anything not on the list is blocked at the gateway, no matter what
code inside the sandbox tries. This is the layer that turns "we
shouldn't talk to OpenSky directly" into "we *can't* talk to OpenSky
directly".

```yaml
endpoints:
  - host: <HOST_IP>      # rendered to your Brev VM's IP
    port: 9202           # opensky-proxy.py
    rules:
      - allow: { method: GET, path: "/api/states/all*" }
      - allow: { method: GET, path: "/api/flights/aircraft*" }
      ...
  - host: services6.arcgis.com   # FAA AIS, no auth, browser-cacheable
    rules:
      - allow: { method: GET, path: "/.../FeatureServer/0/query*" }
```

`install.sh` substitutes `<HOST_IP>` with your VM's reachable address
and pushes the rendered policy to the gateway. Re-run the installer
to update it. **This file is the single audit point for "what can the
sandbox talk to".**

### 3. Host-proxy pattern (how API keys stay out of the sandbox)

The credential we care most about (OpenSky OAuth2 client) lives on the
host VM only:

```
~/.nemoclaw/credentials.json        chmod 600, never copied into sandbox
  ├── inference.endpointUrl
  ├── inference.compatible_api_key
  ├── opensky.client_id             ← ONLY here
  └── opensky.client_secret         ← ONLY here
```

A small Python daemon (`opensky-proxy.py`) reads those at request
time, mints a Bearer token, caches it until expiry, and forwards the
sandbox's HTTP calls to `opensky-network.org`. The sandbox's
`flight.env` contains `OPENSKY_PROXY_URL=http://<host>:9202` and
**nothing else** — no client ID, no secret. A complete compromise of
the sandbox cannot leak the OpenSky key because it isn't there.

```
sandbox FastAPI                            host                       upstream
─────────────                              ────                       ────────
GET …:9202/api/states/all  ────────►  opensky-proxy.py
                                        + Authorization: Bearer …  ──►  opensky-network.org
                              ◄────  forwarded JSON                ◄──
```

The same pattern is reused by `faa-proxy.py` on port 9203, but for a
different reason: those FAA/NWS hosts are public, but they 403 the
cloud ASN the openshell gateway egresses through. The proxy
"IP-rewraps" the call from the host VM, which has a residential-class
IP they accept.

### 4. Skills (the agent's tool surface)

OpenClaw discovers tools by reading **SKILL.md** files placed in
`skills/<name>/SKILL.md`. The YAML frontmatter is the *only* thing
the agent's selector model sees when deciding whether to invoke this
skill — so the description has to be written for the model, not for
humans. Look at `skills/flight-tracking/SKILL.md`:

```yaml
---
name: flight-tracking
description: "Live aircraft tracking, airport lookup, and interactive
  map control for the FlightOps map UI at http://127.0.0.1:18890. Use
  this skill whenever the user asks about live air traffic … HARD
  RULES: (1) ALL data and ALL map control live behind
  http://127.0.0.1:18890 … (2) ANY request that changes what's drawn
  on the map REQUIRES issuing the matching POST /api/map/... call
  FIRST … (5) EVERY /api/map/* response includes a `delivered`
  integer … (6) For 'find a flight matching X and track it' use the
  find→track pattern …"
---
```

Three things to notice:

1. The **description doubles as the prompt**. Tool selection happens
   purely off this string, and so do behavioural rules ("never invent
   a network outage", "always issue the POST before claiming the map
   updated"). Iteration on this string is the highest-leverage way to
   improve agent behaviour without retraining anything.
2. The body of the SKILL.md is a **detailed API reference + worked
   examples**. Once the agent has selected the skill, it reads the
   body for parameter shapes and calling conventions.
3. The skill's tools are just shell commands. There is no JSON-RPC
   layer, no MCP server, no separate tool runtime. The agent runs
   `curl http://127.0.0.1:18890/api/flights/find?...` directly, or
   the convenience wrapper `fly` (in `skills/flight-tracking/scripts/`).

### 5. Map control via WebSocket bus

The interesting integration trick is that **the chat panel and the
OpenClaw skill share one map-control surface**. Both end up calling:

```
POST /api/map/{goto|view|arcs|layer|filter|color|highlight|track|airspace3d}
```

…which writes the command to a sticky in-memory broadcast channel
(`MapBus`) and then pushes it over `/ws/map` to every connected
browser tab. The same code path drives the map whether the request
came from:

- a user typing in the left rail (browser → FastAPI → MapBus)
- the OpenClaw agent (skill → `curl` inside sandbox → FastAPI → MapBus)
- a `fly goto IAD` from any sandbox shell
- a Telegram message routed through your other NemoClaw skill

```
                         ┌──────────────┐
  browser chat ──────────►              │
  fly CLI ───────────────►   /api/map   ├─────► MapBus ─wss/ws/map─► all open tabs
  openclaw skill ────────►              │
  Telegram bridge ───────►              │
                         └──────────────┘
```

The bus replays the last command for ~3 minutes, so a tab opened
right after a `goto` still catches up.

### 6. The dual chat path

There are two NemoClaw chat paths in this demo on purpose, and they
behave differently:

- **In-page chat (left rail)**: the FastAPI calls Nemotron directly
  using `INFERENCE_API_KEY` from `flight.env`. Streams tokens to the
  browser. Simple, fast, no agent loop — good for quick Q&A.
- **OpenClaw chat (Telegram, dashboard, agent CLI)**: runs
  `openclaw agent --json` *inside* the sandbox, which inherits the
  gateway-managed inference route, picks up `flight-tracking`'s
  SKILL.md, and can call its tools in a loop. This is where you get
  "find the flight, track it, summarise it" multi-step behaviour.

Both end up driving the same MapBus, so a Telegram-driven `goto LHR`
is visually identical to one typed in the page.

### 7. Provider records and key rotation

Sensitive credentials are also registered as an **openshell provider**
on the gateway, so when a future skill needs the same key it can pull
it from the gateway (over the gateway's own credential channel) rather
than reading the host file. To rotate the OpenSky key:

```bash
# edit the JSON file
$EDITOR ~/.nemoclaw/credentials.json

# re-run the installer — it refreshes the provider record AND
# tells the host proxy to drop its cached token.
./install.sh <sandbox>
```

The sandbox is never restarted, the SSH tunnel stays up, and the next
upstream call mints a token from the new secret. This is the
"rotate without an outage" property you want for production.

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
├── opensky-proxy.py           # HOST-side OAuth2-injecting proxy (port 9202)
├── faa-proxy.py               # HOST-side IP-rewrap proxy (port 9203)
├── start.sh                   # sandbox-side launcher
├── install.sh                 # host-side installer
└── README.md
```

## Wire diagram

```
Browser ──HTTP/WS──▶ openshell forward ──▶ sandbox:18890 (FastAPI)
                                             │
                                             ├── /api/flights ───▶ host:9202 (opensky-proxy.py) ──▶ opensky-network.org
                                             ├── /api/nas/*    ───▶ host:9203 (faa-proxy.py)    ──▶ nasstatus.faa.gov
                                             ├── /api/weather  ───▶ host:9203 (faa-proxy.py)    ──▶ aviationweather.gov
                                             ├── /api/airspace ───▶ services6.arcgis.com (FAA AIS)
                                             ├── /api/chat     ───▶ Nemotron via openshell inference
                                             └── /ws/map        ◀── /api/map/* (skill / fly CLI / chat)
```

The sandbox itself can only reach the boxes on the right via the
arrows shown — everything else is blocked at the gateway by the
network policy. See [Key concepts §2](#2-network-policy-is-the-authoritative-guardrail).

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
  sandbox itself never sees them — see
  [Key concepts §3](#3-host-proxy-pattern-how-api-keys-stay-out-of-the-sandbox).

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

## Security posture

A condensed checklist; the full reasoning is in
[Key concepts](#key-concepts-a-nemoclaw-tour) above.

- **OpenSky credentials never enter the sandbox.** They live in
  `~/.nemoclaw/credentials.json` (chmod 600) on the host and in an
  `openshell provider` record on the gateway. `opensky-proxy.py`
  injects the Bearer token at the host. `flight.env` inside the
  sandbox contains `OPENSKY_PROXY_URL` and *nothing else* — `cat
  flight.env` from inside a compromised sandbox leaks nothing useful.
- **Public-feed proxy is for IP reputation, not secrets.**
  `faa-proxy.py` (host:9203) wraps `nasstatus.faa.gov` and
  `aviationweather.gov` because those endpoints 403 the cloud ASN the
  openshell gateway egresses through, *not* because they need auth.
  Same network-policy mechanics either way.
- **Network policy is the authoritative guardrail.** See
  `policy/flight-tracking.yaml`. Outbound HTTPS from the sandbox is
  gated to a tight allowlist (the two host proxies + FAA AIS /
  TFRs). `curl https://opensky-network.org/...` from inside the
  sandbox returns "blocked by policy". This is enforced at the gateway,
  not at the application — sandbox code can't bypass it.
- **No public listener.** The sandbox-side FastAPI on port 18890 is
  reachable from the host *only* through the SSH-tunnel-via-`openshell`
  forward (managed by either the systemd-user unit on Linux or
  `openshell forward start` everywhere else). Nothing is bound to a
  public interface.
- **No MCP, no remote tool runtime.** OpenClaw skills run as plain
  shell commands inside the same sandbox. There is no separate JSON-RPC
  server to harden, no extra socket exposed.
- **Inference auth.** `compatible_api_key` is rendered into
  `flight.env` so the in-page chat can call Nemotron directly. The
  OpenClaw agent path uses the gateway-managed inference route and
  needs no key in the sandbox. To harden further, drop
  `INFERENCE_API_KEY` from `flight.env` and proxy inference through a
  third host daemon — same pattern as the OpenSky one.
- **Rotation = edit JSON + re-run installer.** The sandbox is not
  restarted; the proxy mints a fresh token from the new secret on the
  next request.

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
