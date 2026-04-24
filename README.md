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
- A network policy preset that allows only `opensky-network.org` outbound
  from the sandbox-side server. Tile downloads happen in your browser, not
  the sandbox, so the sandbox never reaches the tile CDN.

## Repo layout

```
flight-tracking-integration/
├── app/
│   ├── server.py              # FastAPI: OpenSky proxy, airports, chat, ws bus
│   ├── requirements.txt
│   └── static/
│       ├── index.html
│       ├── styles.css
│       ├── app.js             # MapLibre + deck.gl frontend
│       └── data/airports.json # OpenFlights subset (ODbL)
├── skills/flight-tracking/
│   ├── SKILL.md               # OpenClaw skill descriptor
│   └── scripts/fly            # bash CLI helper
├── policy/flight-tracking.yaml
├── start.sh                   # sandbox-side launcher
├── install.sh                 # host-side installer
└── README.md
```

## Prerequisites

- Working NemoClaw + OpenShell setup (`nemoclaw onboard` already run).
- A running sandbox (the installer lists them; pass the name as `$1`).
- Inference key already configured via `nemoclaw onboard` (the installer
  pulls `endpointUrl`, `model`, and the `compatible_api_key` value out of
  `~/.nemoclaw/credentials.json` automatically).
- Optional: an OpenSky account. Anonymous works at 10 s cadence; setting
  `OPENSKY_USERNAME` / `OPENSKY_PASSWORD` in `~/.nemoclaw/credentials.json`
  lifts that to 5 s.

## Install

```bash
cd flight-tracking-integration
./install.sh <sandbox-name>
```

The installer:

1. Verifies the sandbox exists.
2. Reads inference + (optional) OpenSky creds from `~/.nemoclaw/credentials.json`.
3. Adds an `flight_tracking_opensky` block to the sandbox network policy.
4. Uploads the app, the OpenClaw skill, and `start.sh` into
   `/sandbox/.openclaw-data/flight-tracking/` (canonical writable path).
5. Builds a Python venv and installs deps **inside** the sandbox.
6. Boots `uvicorn server:app` on port `18890` inside the sandbox.
7. Runs `openshell forward start 18890 <sandbox> -d` so the host browser
   can reach it at <http://localhost:18890>.

To reinstall or pick up code changes, just re-run `./install.sh`.

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

This demo intentionally keeps the OpenSky upstream and the Nemotron
inference key inside the sandbox — there's no host proxy. Reasons:

- OpenSky's anonymous tier is keyless; there's nothing to leak.
- The Nemotron key is already discoverable to the agent via the
  gateway-managed inference path, so duplicating it inside `flight.env`
  doesn't widen the blast radius.
- The sandbox-side server is reachable from the host **only** through
  `openshell forward`, which terminates inside the gateway's local
  127.0.0.1 listener. No port is opened on the public network.

If you want to harden later, drop `INFERENCE_API_KEY` from
`flight.env` and route inference through a host-side proxy on
127.0.0.1 — `server.py` already reads `INFERENCE_BASE_URL`, so swapping
it is a one-line change.

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
