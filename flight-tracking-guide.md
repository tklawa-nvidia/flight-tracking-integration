# Flight Tracking Demo for NemoClaw / OpenClaw

Add a live **FlightOps** airspace console to your NemoClaw sandbox: real-time aircraft on an interactive map, agent-driven map control from chat or Telegram, and read-only overlays from public aviation feeds (airspace, weather, NAS advisories, and airport detail). OpenSky credentials stay on the host; the sandbox reaches upstream APIs only through policy-enforced proxies.

> **Compatibility:** Verified against `openshell 0.0.44` + `openclaw 2026.5.18` (new `/sandbox/.openclaw/` layout) and earlier builds that still use the legacy `/sandbox/.openclaw-data/` layout. `install.sh` auto-detects which one is in play — no flags needed.

---

## What You Get

| Capability | Description |
|---|---|
| **Live aircraft** | Position, altitude, speed, heading, vertical rate, squawk, and phase-of-flight via [OpenSky Network](https://opensky-network.org/) (~10 s refresh) |
| **Interactive map** | MapLibre + deck.gl UI with trails, filters, 3D airspace, METAR/NAS layers, and airport detail |
| **Agent map control** | OpenClaw `flight-tracking` skill: `goto`, `track`, `arcs`, layers, colors, filters — same behavior from the in-page chat or Telegram |
| **Route & registry** | Callsign routes and aircraft registry via proxied `adsbdb` / `hexdb` lookups |
| **Public aeronautical data** | FAA AIS (SUA, Class airspace, ARTCC boundaries, runways, taxiways, obstacles, airways, navaids), TFRs, NAS Status, Aviation Weather Center METARs |
| **Airports** | Bundled OurAirports-derived dataset (~11k airports) with in-view search |
| **CLI helper** | `fly goto IAD`, `fly analyze JFK 80`, `fly health`, etc. inside the sandbox |

### Data sources (informational)

This demo combines several **public** feeds. Layer names in the UI may reference FAA-published datasets (e.g. AIS airspace, NAS Status); the demo is a **NemoClaw flight-tracking integration**, not an official government product.

| Source | Role |
|---|---|
| OpenSky Network | Live ADS-B state, per-aircraft history, tracks |
| FAA AIS (ArcGIS) | Airspace polygons, runways, taxiways, obstacles, routes, navaids, ARTCC boundaries |
| FAA NAS Status / AWC METAR | Airport advisories and METAR observations (via host proxy when sandbox egress is blocked) |
| FAA TFR GeoServer | Temporary flight restrictions |
| adsbdb / hexdb | Registry and callsign route enrichment |
| OurAirports (bundled) | Airport locations and metadata |
| OpenFreeMap / RainViewer | Basemap and optional weather radar tiles (browser-side) |

---

## Prerequisites

1. **NemoClaw** installed and a sandbox running (`nemoclaw onboard` completed)
2. **Host tools**: `openshell`, `nemoclaw`, `python3`, `ssh`, `curl`
3. **Linux (recommended)**: `systemd --user` for a resilient port-forward to `localhost:18890`
4. **Optional — OpenSky OAuth client**: [Create an API client](https://opensky-network.org/manage-account) and add `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` to `~/.nemoclaw/credentials.json` for higher rate limits (~4,000 credits/day vs ~400 anonymous)

---

## Quick Start

```bash
git clone https://github.com/brevdev/nemoclaw-demos.git
cd nemoclaw-demos/flight-tracking-demo
./install.sh <sandbox-name>
```

The installer:

1. **Auto-detects the sandbox layout** — `/sandbox/.openclaw/` (openshell ≥ 0.0.44) vs legacy `/sandbox/.openclaw-data/` — and routes the skill, agent home, and `openclaw.json` mutation to the right place.
2. Starts host-side **opensky-proxy** (port 9202) and **faa-proxy** (port 9203).
3. Applies an OpenShell network policy (sandbox → host proxies + allowed public endpoints, with `opensky-network.org` / `auth.opensky-network.org` removed so the only OpenSky path is the host proxy).
4. Deploys the FastAPI app, static UI, and `flight-tracking` skill into the sandbox.
5. On the new layout, enables `flight-tracking` in `openclaw.json` and ensures `tools.profile=coding` so the agent surfaces `exec` in its system prompt (without this the agent "spins out" hunting for tools).
6. Writes `flight.env` with proxy URLs + the detected agent home — **no OpenSky secrets in the sandbox**.
7. Restarts uvicorn on port **18890** and sets up host port forwarding (systemd-user unit, with `openshell forward` as fallback).

Open the map (after forwarding — see below):

```text
http://localhost:18890
```

On a **Brev** or remote VM, forward from your laptop:

```bash
brev port-forward <instance> --port 18890:18890
```

### Installer flags

| Flag | Purpose |
|---|---|
| `--status` | Print install + proxy + tunnel state and exit |
| `--uninstall` | Stop proxies, drop policy block, remove skill + app, disable tunnel |
| `--update-creds` | Force-prompt for new OpenSky OAuth2 credentials |
| `--skip-systemd` | Don't install/touch the systemd-user tunnel (use `openshell forward`) |
| `--port <N>` | FastAPI port (default `18890`) |
| `--opensky-port <N>` | opensky-proxy port (default `9202`) |
| `--faa-port <N>` | faa-proxy port (default `9203`) |

Env-var equivalents: `OPENSHELL_GATEWAY`, `OPENSHELL_SANDBOX`, `OPENSKY_PROXY_HOST`, `FLIGHT_APP_PORT`, `SKIP_SYSTEMD_TUNNEL=1`.

```bash
./install.sh --status      # what's installed + is everything up?
./install.sh --uninstall   # clean removal
```

---

## Security: Tier-1 Host Proxies

| Secret / blocked feed | Host component | Sandbox sees |
|---|---|---|
| OpenSky OAuth2 | `opensky-proxy.py` :9202 | `OPENSKY_PROXY_URL` only |
| NAS Status + AWC METAR (often 403 from cloud egress) | `faa-proxy.py` :9203 | `FAA_PROXY_URL` only |
| FAA AIS, TFRs | Direct (policy allowlist) | Local FastAPI proxies and caches |

The agent uses **OpenClaw skills** (not MCP): `skills/flight-tracking/SKILL.md` plus `fly` / `curl http://127.0.0.1:18890/api/...`. Map commands broadcast over `/ws/map` to every open browser tab.

---

## Live-aircraft data source (choosing a feed)

You can switch feeds two ways: **live, from the console UI** (no reinstall), or as the **install-time default**.

**In the console (per viewer):** open the **Layers** panel — under the **Live aircraft** toggle there's a **Data feed** selector with a short note of what that feed supports (live tracking/trails always work; only OpenSky adds *pre-built* history, and only from a non-cloud host). Pick `Community · auto`, a single vendor, or `OpenSky`. The choice is remembered per browser and takes effect on the next poll. Under the hood the UI passes `?provider=…` to `/api/flights`, the sandbox forwards it to the proxy, and `GET /api/sources` supplies the option list + capability notes.

**From chat (drives the map + the agent's own analysis):** the flight-tracking skill is provider-aware — just ask the OpenClaw/Nemoclaw agent to *"switch the live feed to airplanes.live"* (or `adsb.fi` / `adsb.lol` / `opensky` / `community`). It calls `POST /api/map/source`, which flips the feed for **both** the operator's map (all connected browsers update their selector + note) **and** every server-side read the agent uses (`/api/flights`, `/api/analyze`, `/api/flights/find`, `/api/map/track`) — so what the agent reasons about always matches what you see. The skill also knows the capability differences: on the default community feed it won't claim OpenSky-confirmed origins or offer pre-built per-aircraft history, and it falls back to the callsign route lookup (`/api/route`) for origin → destination.

**At install time (default for everyone):** the host proxy (`opensky-proxy.py`) sources live aircraft from OpenSky **or** from free community ADS-B aggregators, selected with a single env var, `FLIGHT_ADSB_SOURCE`:

| `FLIGHT_ADSB_SOURCE` | What it does |
|---|---|
| `community` (default) | Community aggregators with automatic fallback across `adsb.fi` → `airplanes.live` → `adsb.lol`. First to respond wins. |
| `adsb.fi` / `airplanes.live` / `adsb.lol` | Pin to that **one** vendor (no fallback). |
| `opensky` | Legacy OpenSky OAuth2 passthrough (needs credentials). |

> **Why the default is `community`, not `opensky`:** OpenSky blackholes datacenter/cloud IP ranges (AWS/GCP/Azure). From a Brev/cloud VM, `opensky-network.org` is unreachable (the SYN is dropped inside OpenSky's network — confirmed via TCP traceroute), so planes never load. The community feeds have no such block and need no credentials, so they work out of the box on cloud VMs. Use `opensky` only on a non-cloud host whose egress OpenSky still accepts.

Set it at install time, e.g.:

```bash
FLIGHT_ADSB_SOURCE=airplanes.live ./install.sh <sandbox-name>   # pin one vendor
FLIGHT_ADSB_SOURCE=opensky        ./install.sh <sandbox-name>   # non-cloud hosts
```

Optional companions: `FLIGHT_ADSB_PROVIDERS` (comma list to reorder/limit the `community` fallback) and `FLIGHT_ADSB_MAX_NM` (max query radius, default 250 nm — the aggregators are radius-based, so very zoomed-out views are covered as a 250 nm circle around the viewport centre).

Check what's live at any time — `GET /` on the proxy reports the mode, active providers, and capabilities:

```bash
curl -s http://127.0.0.1:9202/ | python3 -m json.tool
```

### What each feed supports

| Capability | OpenSky | Community (adsb.fi / airplanes.live / adsb.lol) |
|---|---|---|
| Live positions (lat/lon, alt, speed, heading, v-rate, squawk) | ✅ | ✅ |
| **Live tracking / follow a plane** (icon moves, camera follows) | ✅ | ✅ |
| **Live trail** (drawn as you watch — client accumulates each poll) | ✅ | ✅ |
| **Pre-built track history** (backfill of where a plane flew *before* you opened it) | ✅ | ❌ (no per-aircraft historical-track API) |
| Origin → destination in the detail drawer | ✅ (icao24 flight history) | ✅ via callsign route lookup (`adsbdb`), ❌ the icao24 path |
| Aircraft registry (registration, type, operator, photo) | n/a (uses `adsbdb`/`hexdb`) | ✅ (unchanged — `adsbdb`/`hexdb`, independent of the source) |
| Needs credentials | ✅ OAuth2 | ❌ none |
| Works from a cloud/VM IP | ❌ (blocks cloud ranges) | ✅ |

**So does losing OpenSky history stop you from tracking flights? No.** You can still fully track any flight live — the plane renders, moves, you can select/follow/highlight it, colour by altitude/phase/squawk, and a trail builds up in real time as the app polls. The only things the community feeds don't provide are: (1) the *pre-built* track showing where a plane had already been before you started watching, and (2) OpenSky's icao24-keyed origin/destination — and even origin/destination is still filled in from the callsign route lookup (`adsbdb`). Registry/photo enrichment is unaffected because it never used OpenSky.

---

## Usage Examples

### Map prompts (agent)

- "Go to IAD and analyze traffic within 80 km"
- "Find the latest departure from IAD heading west and track it on the map"
- "Color planes by altitude"
- "Show inbound arcs to JFK and tilt the map"
- "Any ground stops or GDPs right now?"
- "Toggle METAR and NAS status layers"
- "Filter to emergency squawks only"

### CLI (inside sandbox)

```bash
fly goto IAD
fly analyze IAD 80
fly arcs JFK 120
fly highlight UAL123
fly layer metar on
fly health
```

### Telegram

Wire Telegram through NemoClaw as usual. The same `flight-tracking` skill drives the map when a browser tab is connected (`delivered` > 0 on `/api/map/*` responses).

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `localhost:18890` refused (laptop) | Run `brev port-forward … --port 18890:18890` and keep it open |
| Empty response on 18890 (VM) | `systemctl --user restart flight-tunnel.service`; restart app: `ssh openshell-<sandbox> 'cd /sandbox/.openclaw-data/flight-tracking && nohup ./start.sh >> server.log 2>&1 &'` |
| No aircraft / rate limited | Ensure `opensky-proxy.py` is running on the host; check `/api/health` for `opensky_auth: "host-proxy"` |
| NAS / METAR layer fails | Start `faa-proxy.py` on host :9203; re-run `./install.sh` |
| Agent says map updated but UI unchanged | Open `http://localhost:18890` first; check `delivered` in tool response |
| Chat panel hangs / says "openclaw binary not found" | `./install.sh --status` to see what's missing; usually means the sandbox image lost `/usr/local/bin/openclaw` — re-run `nemoclaw onboard` |
| Agent doesn't pick up the skill ("don't know about flights") | Disconnect + reconnect the TUI / chat to force the gateway to reload `openclaw.json`; verify `skills.entries["flight-tracking"].enabled` and `tools.profile=="coding"` in `/sandbox/.openclaw/openclaw.json` (or re-run `./install.sh`) |
| Wrong sessions path on legacy build | `install.sh` writes `OPENCLAW_AGENT_HOME` into `flight.env` based on detected layout; server.py falls back to auto-detect when the env var is unset — check `/api/health.openclaw_agent_home` |
| Want to roll back / move sandboxes | `./install.sh --uninstall` removes everything cleanly; re-run `./install.sh <new-sandbox>` |

---

## Repo Layout

```text
flight-tracking-demo/
├── flight-tracking-guide.md   # this file
├── install.sh                 # host installer
├── opensky-proxy.py           # host — OpenSky OAuth2
├── faa-proxy.py               # host — NAS + METAR IP rewrap
├── policy/flight-tracking.yaml
├── app/                       # FastAPI + MapLibre/deck.gl UI
├── skills/flight-tracking/    # OpenClaw skill + fly CLI
├── scripts/systemd/           # optional tunnel unit template
└── start.sh                   # sandbox-side uvicorn launcher
```

---

## Official Resources

- [NemoClaw](https://github.com/NVIDIA/NemoClaw) · [NemoClaw Brev Launchable](https://build.nvidia.com/nemoclaw)
- [nemoclaw-demos](https://github.com/brevdev/nemoclaw-demos) — other integration examples
- [OpenSky API](https://opensky-network.org/) · [OpenSky terms of use](https://opensky-network.org/about/terms-of-use)
