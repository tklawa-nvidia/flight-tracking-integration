---
name: flight-tracking
description: Live aircraft tracking, airport lookup, and interactive map control. Use when the user asks about live air traffic, what is flying near an airport, inbound traffic patterns, unusual squawks, or wants the FlightOps map driven (fly to an airport, draw arcs, highlight a flight).
---

# flight-tracking

Backend for live aircraft data and the FlightOps map UI. The map and API both
run inside this sandbox at `http://127.0.0.1:18890`. The same HTTP surface is
also reachable from any external trigger (Telegram bot, dashboard, etc.) the
operator wires up.

## When to invoke this skill

- The user asks about *live* air traffic ("what's flying over IAD right now?", "any inbound to JFK?").
- The user wants the map driven without leaving chat ("go to LHR", "show me arcs into LAX").
- The user asks about unusual situations (emergency squawks, ground stops, sudden traffic spikes).
- The user asks for an airport's basic facts and the answer benefits from the live overlay (the skill is the bridge to that data; if the user only wants a static fact, answer normally).

If the user is asking a generic aviation question that does not need live data
(e.g. "how does ATC handoff work?"), do not invoke the skill — answer from
general knowledge.

## API surface

All endpoints are local to the sandbox.

### Read

```
GET  /api/health
GET  /api/flights?bbox=west,south,east,north
GET  /api/airports?bbox=...&limit=N
GET  /api/airport/{IATA_or_ICAO}
GET  /api/analyze?airport=IAD&radius_km=80
```

### Write (drives the map for any connected browsers)

```
POST /api/map/goto       {"target":"IAD","zoom":9}
POST /api/map/arcs       {"airport":"IAD","radius_km":80}
POST /api/map/layer      {"layer":"arcs","visible":true}     # layer ∈ {flights, airports, arcs, trails}
POST /api/map/highlight  {"flight":"UAL123" | "a1b2c3"}      # callsign or ICAO24
POST /api/map/command    {"type":"...","payload":{...}}      # generic broadcast
```

## How to drive a typical request

```bash
# user: "go to IAD and analyse the traffic"
curl -sX POST http://127.0.0.1:18890/api/map/goto \
     -H 'Content-Type: application/json' \
     -d '{"target":"IAD","zoom":9}'

curl -s "http://127.0.0.1:18890/api/analyze?airport=IAD&radius_km=80"
```

Then summarise the analysis JSON back to the user in 3–5 short bullets:
total airborne, vertical-mode mix (climb/cruise/descent), top countries of
origin, any notable squawks (7500/7600/7700).

## bbox convention

OpenSky returns up to ~25 sq° anonymously. Stay under that or analysis
calls will be rate-limited. `radius_km` of 80 around a single airport is
within budget; 200 km is borderline.

## Error handling

- 429 from `/api/flights` or `/api/analyze` → wait ≥10 s and retry once. If still 429, tell the user the upstream is rate-limited.
- 502 from `/api/flights` → OpenSky upstream is unreachable. Don't retry tightly.

## Things the skill does NOT do

- Does not call any external service directly. All upstream calls go through
  the sandbox-side FastAPI server which is governed by the network policy in
  `policy/flight-tracking.yaml`.
- Does not invent flight numbers or fabricate fields when data is missing.
  Say "no contact" or "data unavailable" instead.
- Does not push state changes the user did not ask for. Layers stay how the
  user left them unless the user explicitly asks to change them.
