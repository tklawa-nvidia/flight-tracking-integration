---
name: flight-tracking
description: "Live aircraft tracking, airport lookup, and interactive map control for the FlightOps map UI at http://127.0.0.1:18890. Use this skill whenever the user asks about live air traffic, what is flying near an airport, inbound patterns, unusual squawks, METAR weather, NAS status / ground stops, ARTCC boundaries, OR wants the map driven in ANY way (fly to an airport, draw arcs, highlight a flight, RECOLOUR the planes, toggle a layer, filter by phase or squawk, switch to 3D, change METAR colour mode). HARD RULES: (1) ALL data and ALL map control live behind http://127.0.0.1:18890 in this same sandbox. NEVER curl upstream FAA/OpenSky/AWC/adsbdb/hexdb hosts directly — they're firewalled. If a local call fails say 'service unavailable', do not invent a network-outage excuse. (2) ANY request that changes what's drawn on the map REQUIRES issuing the matching POST /api/map/... call FIRST, BEFORE describing the result. Never claim 'I've updated the map to ...' unless you actually issued the POST this turn — the user is looking at a live chart and will see if nothing changed. (3) The aircraft colour scheme has exactly FOUR fixed presets — phase | altitude | vrate | squawk — set via POST /api/map/color {\"mode\":\"<one of four>\"}. Aliases 'elevation', 'flight level', 'rate of climb', 'emergency' are accepted. Do NOT invent custom palettes; describe only what the preset shows: phase = orange family, altitude = single-hue green ramp (mint→deep green, NOT a rainbow), vrate = diverging purple (violet↔magenta), squawk = grey + red/amber for 7500/7600/7700. (4) Other map controls: POST /api/map/metar-color (flt_cat|wind|temp|visibility), POST /api/map/layer, POST /api/map/filter, POST /api/map/goto, POST /api/map/view, POST /api/map/arcs (auto-tilts), POST /api/map/airspace3d, POST /api/map/track (ONE-shot 'find this plane and follow it' — preferred over /api/map/highlight + /api/map/view in two calls). (5) EVERY /api/map/* response includes a `delivered` integer = how many browser tabs received the broadcast. If `delivered:0` the dashboard isn't open right now — say 'I issued the command but no map UI is connected to receive it; please open http://127.0.0.1:18890 first', do NOT claim the map updated. The server caches the last sticky command for ~3 minutes, so a tab that opens shortly after will still catch up. (6) For 'find a flight matching X and track it' (e.g. 'what just left IAD heading to TPA, zoom on it'), use the find→track pattern: GET /api/flights/find?departing=IAD&arriving=TPA picks candidates server-side, then POST /api/map/track {\"flight\":\"<id from find>\"} drives the camera. NEVER loop over /api/route/<callsign> per live flight — that's hundreds of HTTP calls inside a tool exec and will time out. Open the SKILL.md body for examples and field schemas."
---

# flight-tracking

Backend for live aircraft data and the FlightOps map UI. The map and API both
run inside this sandbox at `http://127.0.0.1:18890`. The same HTTP surface is
also reachable from any external trigger (Telegram bot, dashboard, etc.) the
operator wires up.

## ⚠️ Hard rules — read first

1. **Every data feed lives behind `http://127.0.0.1:18890`.** That URL is a
   FastAPI proxy running in *this same sandbox*. It is not the public internet.
   Localhost is always reachable from your shell — if `curl http://127.0.0.1:18890/...`
   fails, retry once and then surface "service unavailable" honestly.
2. **NEVER call upstream services directly.** Do **not** curl
   `nasstatus.faa.gov`, `aviationweather.gov`, `opensky-network.org`,
   `adsbdb.com`, `hexdb.io`, `tfr.faa.gov`, `ais-faa.opendata.arcgis.com`,
   or any other external host. They are blocked by the egress policy.
   The local proxy already wraps every upstream you might want and adds
   normalisation + caching.
3. **Never invent a "network outage" excuse.** If you haven't actually
   tried `curl http://127.0.0.1:18890/api/<endpoint>` yet, *try it*.
   The proxy has been up reliably for the entire deployment lifetime;
   "I can't reach FAA" or "DNS is failing" is almost always a sign you
   forgot to call the tool. Hallucinating a network failure as a way
   out of "I'm not sure" is the worst possible answer for a live-ops
   dashboard.
4. **If the local proxy genuinely returns non-200 or empty data**, say
   "data unavailable" or "no advisory at this time" — do not fabricate
   a plausible-sounding inferred answer from `/api/flights`. The
   inferential fallback is a flag that the user should re-ask later,
   not a substitute for the real feed.
5. **Side-effects on the map require the corresponding POST.** When
   the user asks you to *do* something to the chart (recolour planes,
   toggle a layer, filter buckets, fly to an airport, draw arcs,
   tilt the camera, switch METAR colour mode, enable 3D), you must
   issue the matching `POST /api/map/...` call **before** describing
   the result. The user is looking at a live map and will notice
   immediately if the chart didn't change. "I've updated the map
   to colour by altitude" without a real `POST /api/map/color
   {"mode":"altitude"}` is a bug, not an answer.
6. **Never invent visual details that aren't in the preset.** The
   four colour presets, the chip-filter buckets, the four METAR
   colour modes, and the layer enum are *fixed* and live in `app.js`.
   Don't describe a "rainbow gradient" or a "blue-and-yellow scheme"
   — describe what the user will actually see (see the colour-mode
   table further down).

## When to invoke this skill

- The user asks about *live* air traffic ("what's flying over IAD right now?", "any inbound to JFK?").
- The user asks about a *specific aircraft or flight* by callsign or ICAO24 ("where is UAL123 going?", "what's a1b2c3 doing?", "where did this plane come from?"). Use `/api/flight/{icao24}` for origin/destination/timing and `/api/flight/{icao24}/track` for the actual route flown.
- The user wants the map driven without leaving chat ("go to LHR", "show me arcs into LAX", "highlight UAL123").
- The user wants the **aircraft colour scheme** changed ("colour planes by altitude", "show climbing vs descending", "switch to emergency squawk view", "reset colours to phase of flight"). Use `POST /api/map/color`.
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
GET  /api/airports?bbox=...&types=large,medium,small&limit=N
# `types` filters the OurAirports-derived dataset (~11k entries: 1,184
# large, 4,093 medium, 6,144 small). Default is no filter; pass
# `types=large` for hub-only summaries, `types=large,medium` to include
# regionals, omit `types` for everything down to GA strips with codes.
# Response contains {airports, count, total, truncated}.
GET  /api/airport/{IATA_or_ICAO}
GET  /api/analyze?airport=IAD&radius_km=80

# Per-aircraft lookups — origin/destination/timing and waypoint route.
# `icao24` is the lower-case 6-character ICAO24 transponder hex code.
# Resolve a callsign first via /api/flights then read the `id` field.
GET  /api/flight/{icao24}?lookback_hours=24
GET  /api/flight/{icao24}/track?time=0

# Globally cached FAA AIS layers — full GeoJSON FeatureCollection.
GET  /api/airspace/{sua|classes|tfrs|runways|artcc}

# Bbox-only FAA layers — too large to ship globally. The bbox is required.
GET  /api/airspace/{taxiways|obstacles|ats|navaids}?bbox=west,south,east,north

# Reasoning helper — point-in-polygon + nearest neighbours across any
# combination of the global *and* bbox layers (we synthesize the bbox for
# you from radius_km when bbox-only datasets are requested). artcc and
# navaids are valid datasets here too.
GET  /api/airspace/lookup?lat=...&lon=...&radius_km=50&datasets=sua,tfrs,runways,obstacles

# Operational-data overlays (the "30-min demo" features):
GET  /api/weather/metar?bbox=west,south,east,north  # METARs with VFR/MVFR/IFR/LIFR
GET  /api/nas/status                                 # nationwide NAS advisories
GET  /api/nas/airport/{code}                         # per-airport NAS advisory (FAA 3-letter or ICAO)
GET  /api/registry/{icao24}                          # aircraft registry (adsbdb / hexdb)
GET  /api/route/{callsign}                           # callsign → origin/destination/airline
```

### Write (drives the map for any connected browsers)

```
POST /api/map/goto       {"target":"IAD","zoom":9,"pitch":55,"bearing":0}
# pitch/bearing are optional 3D camera hints. 0° pitch = top-down,
# 50–60° pitch reads as "looking across the chart" and is the
# right call any time depth helps the answer (e.g. inbound arcs,
# 3D airspace, climb/descent visualisation). Omit either field
# to keep the camera's current value.

POST /api/map/arcs       {"airport":"IAD","radius_km":80,"tilt":true}
# tilt defaults to true and auto-pans the camera with pitch=55° so
# the parabolic arcs read as 3D ribbons converging on the airport.
# Pass {"tilt": false} only if the user explicitly asked for a
# flat top-down view or already framed the camera themselves.

POST /api/map/view       {"pitch":60}                        # free-form camera
# Any of {lat,lon,zoom,pitch,bearing}; missing fields preserved.
# Use this when the user asks to angle/spin the map without
# re-targeting an airport ("tilt the map to 60", "spin north",
# "go straight down" → {"pitch":0}).

POST /api/map/layer      {"layer":"arcs","visible":true}
# layer ∈ {flights, airports, arcs, trails, paths, weather,
#          sua, classes, tfrs, runways, taxiways, obstacles, ats,
#          metar, nas, artcc, navaids}

POST /api/map/highlight  {"flight":"UAL123" | "a1b2c3"}      # callsign or ICAO24
# Lower-level: just selects the flight + enables the trails layer +
# tries to fly the camera. Browser may no-op if the bus message
# arrives before the next /api/flights tick has populated the
# client-side flight index. Prefer /api/map/track below for the full
# "find this plane and show it to me" flow.

POST /api/map/track      {"flight":"UAL108" | "a2ca5d"}      # ONE call: lookup + highlight + camera move
# This is the canonical "find this plane and track it" tool. The
# server resolves the live flight against the OpenSky feed, then
# broadcasts BOTH `highlight` (selects + opens drawer + enables
# trails) AND `view` (camera pan to the plane's current lat/lon)
# in a single round-trip — atomic from the agent's perspective.
# If the plane isn't in the live feed the response is
# {"ok":false,"error":"no live contact for ..."} so the agent
# learns immediately instead of broadcasting a stale highlight
# the browser silently ignores.
# `flight` is auto-classified: 6 hex chars → ICAO24, anything else
# → callsign. `callsign` and `icao24` fields are also accepted
# explicitly. Optional pose: zoom (default 10), pitch, bearing.
# Use this whenever the user asks to "track UAL108", "follow that
# plane", "find and zoom to the most recent IAD departure", etc.

POST /api/map/color      {"mode":"phase|altitude|vrate|squawk"}
# colour modes (also accepts aliases like "elevation", "rate of climb",
# "emergency", "default" — server normalises them):
#   phase    → takeoff/cruise/descent/ground (default)
#   altitude → light → bright green by flight level
#   vrate    → magenta climb / violet descent diverging palette
#   squawk   → highlights 7500/7600/7700, mutes everyone else to grey

POST /api/map/filter     {"mode":"squawk","only":"emergency"}
# Bucket filter for the chip legend. Hides every plane whose bucket
# is *not* in the armed set, while keeping the colour scheme for the
# planes that remain visible. Posting a filter for a mode that isn't
# the active colour mode auto-switches the colour mode too, since the
# filter is only meaningful when you can see what it's filtering.
#   mode:   "phase" or "squawk" (continuous modes don't bucket)
#   buckets: explicit replacement set ["climb","level-fast",…]
#            phase  ∈ {climb, level-slow, level-fast, descend, ground}
#            squawk ∈ {7500, 7600, 7700, normal, ground}
#   include: add these buckets to the current armed set
#   exclude: remove these buckets from the current armed set
#   only:    shortcut name; resolves to a buckets set
#            phase:  airborne | in-flight | level | cruise |
#                    climbing | takeoff | departing |
#                    descending | landing | arriving |
#                    ground | parked | moving
#            squawk: emergency | non-normal | abnormal | alerts |
#                    hijack | comms-failure | general |
#                    normal | airborne
#   reset:   true → re-arm every bucket (== no filtering)

POST /api/map/metar-color  {"mode":"flt_cat|wind|temp|visibility"}
# METAR overlay colour-mode. Same idea as /api/map/color but for the
# weather station bodies. Aliases: "flight category", "wind speed",
# "temperature", "vis" all resolve.

POST /api/map/airspace3d   {"enabled":true}
# Toggle 3D extrusion of airspace polygons + lift planes to their
# reported altitude. Looks great paired with a 50–60° pitch.

POST /api/map/command    {"type":"...","payload":{...}}      # generic broadcast
```

### Driving the map: a worked example

```bash
# user: "show me only the planes squawking emergency near IAD"
curl -sX POST http://127.0.0.1:18890/api/map/goto \
     -d '{"target":"IAD","zoom":7,"pitch":40}' -H 'Content-Type: application/json'
curl -sX POST http://127.0.0.1:18890/api/map/color \
     -d '{"mode":"squawk"}' -H 'Content-Type: application/json'
curl -sX POST http://127.0.0.1:18890/api/map/filter \
     -d '{"mode":"squawk","only":"emergency"}' -H 'Content-Type: application/json'
# now the chart shows only 7500/7600/7700 traffic, coloured by code,
# with the camera tilted at 40° so the operator can scan altitudes.

# user: "go back to normal" / "reset filters"
curl -sX POST http://127.0.0.1:18890/api/map/filter \
     -d '{"mode":"squawk","reset":true}' -H 'Content-Type: application/json'
curl -sX POST http://127.0.0.1:18890/api/map/filter \
     -d '{"mode":"phase","reset":true}'  -H 'Content-Type: application/json'
curl -sX POST http://127.0.0.1:18890/api/map/color \
     -d '{"mode":"phase"}' -H 'Content-Type: application/json'
```

### What is *not* exposed via chat

To avoid hallucinating capabilities, here's what the agent **cannot**
drive remotely. If the user asks for one of these, say so plainly
rather than pretending to have done it.

- The vertical-scale slider for 3D extrusion (manual UI only).
- The drawer panels (registry, NAS, route info) — these populate
  reactively from `/api/map/highlight` selections.
- Theme / dark-mode / map basemap style — there is one fixed dark
  basemap.
- Adding *new* layers that aren't in the layer enum above — the agent
  cannot draw arbitrary GeoJSON. Use `/api/map/command` for a generic
  WebSocket broadcast only when you've already updated `app.js` to
  handle that custom message type.

## How to drive a typical request

```bash
# user: "go to IAD and analyse the traffic"
curl -sX POST http://127.0.0.1:18890/api/map/goto \
     -H 'Content-Type: application/json' \
     -d '{"target":"IAD","zoom":9}'

curl -s "http://127.0.0.1:18890/api/analyze?airport=IAD&radius_km=80"
```

### When the user asks for arcs

`/api/map/arcs` already auto-tilts the camera (pitch ≈ 55°, north-up)
before drawing — the user's natural ask "show me the arcs into IAD"
turns into a single POST. Don't also pre-call `/api/map/goto` unless
the user explicitly asked you not to tilt; the arcs handler already
broadcasts a goto with the right pose.

```bash
# user: "show me the inbound arcs into JFK"
curl -sX POST http://127.0.0.1:18890/api/map/arcs \
     -H 'Content-Type: application/json' \
     -d '{"airport":"JFK","radius_km":80}'
```

Then summarise the analysis JSON back to the user in 3–5 short bullets:
total airborne, vertical-mode mix (climb/cruise/descent), top countries of
origin, any notable squawks (7500/7600/7700).

## How to look up a specific flight

For "track this plane" / "find UAL123 and zoom in" / "follow that
flight" requests, use `/api/map/track` — ONE call does the lookup,
highlight, and camera move. Don't unroll it into separate
`/api/flights` + `/api/map/highlight` + `/api/map/view` calls; that
just gives the agent more chances to get distracted between steps
(and was the original cause of "the agent narrated the position but
the map never moved").

```bash
# user: "find UAL108 and track it on the map"
curl -sX POST http://127.0.0.1:18890/api/map/track \
     -H 'Content-Type: application/json' \
     -d '{"flight":"UAL108","zoom":10,"pitch":45}'
# → {"ok":true,"delivered":1,"flight":{"id":"A2CA5D","callsign":"UAL108",
#    "lat":39.11,"lon":-76.76,"alt_m":5097,...}}
# If `delivered` is 0, no browser tab is connected — tell the user
# to open the dashboard, do NOT claim the map updated.
```

## How to find AND track ("the most recent IAD departure heading to Tampa")

Two-step pattern: **find → pick → track**. Discovery is server-side
via `GET /api/flights/find` so the agent never has to loop over
/api/route per live callsign (which used to take hundreds of calls
and time out the chat turn — never do that).

```bash
# user: "what just left IAD heading to Tampa? zoom in on it"

# 1. FIND — server picks candidates by departing/arriving airport,
#    auto-applies climb-phase + heading-toward-arriving filters,
#    and (because both departing and arriving were given) confirms
#    the route against adsbdb in parallel for the top candidates.
curl -s "http://127.0.0.1:18890/api/flights/find?departing=IAD&arriving=TPA&limit=5"
# → {"ok":true,"count":1,"filters":{...},
#    "flights":[{"id":"a2ca5d","callsign":"UAL108","lat":...,"lon":...,
#                "alt_m":5097,"vrate_mps":12.3,"heading":188,
#                "distance_from_center_km":34,"heading_misalign_deg":7,
#                "route_match":"confirmed",
#                "route":{"origin":{"iata":"IAD"},"destination":{"iata":"TPA"},...}}]}

# 2. PICK the first/most-recent — it's already sorted by `latest`
#    when departing or arriving is set. (Other orders: closest,
#    lowest_alt, fastest_climb, aligned.)

# 3. TRACK — pass the resolved id (or callsign) to /api/map/track.
curl -sX POST http://127.0.0.1:18890/api/map/track \
     -H 'Content-Type: application/json' \
     -d '{"flight":"a2ca5d","zoom":10,"pitch":45}'
```

Other useful `find` shapes:

```bash
# Live read-only "what flights are going IAD→TPA right now?"
GET /api/flights/find?departing=IAD&arriving=TPA

# All flights climbing out of IAD (no destination filter)
GET /api/flights/find?departing=IAD&phase=climb

# Anything emergency squawking near JFK
GET /api/flights/find?near=JFK&radius_km=80&phase=airborne&order=closest

# Fastest climbers right now in CONUS (no airport anchor)
GET /api/flights/find?phase=climb&order=fastest_climb&limit=5
```

Filter cheat sheet (all optional, AND'd together):

| param           | example          | notes                                                   |
|-----------------|------------------|---------------------------------------------------------|
| `departing`     | `IAD` `KIAD`     | airport code/name; defines the search bbox + bearing    |
| `arriving`      | `TPA` `Tampa`    | derives heading filter; enables route confirmation      |
| `near`          | `JFK`            | search center if no `departing`                         |
| `radius_km`     | `120`            | default 150                                             |
| `phase`         | `climb` `cruise` | aliases: departing, arriving, level, takeoff, landing…  |
| `min_alt_m`     | `1500`           | below ≈ 5,000 ft is a useful "still climbing" gate      |
| `max_alt_m`     | `6000`           |                                                          |
| `heading_deg`   | `90`             | overrides departing→arriving auto-derived bearing       |
| `heading_tol_deg` | `45`           | default ±35°                                            |
| `since_seconds` | `600`            | last_seen within the last N seconds                     |
| `confirm_route` | `true` `false`   | default true if departing or arriving is set           |
| `order`         | `latest`         | latest, closest, lowest_alt, fastest_climb, aligned     |
| `limit`         | `10`             | 1–50                                                    |

DO NOT loop `/api/route/<cs>` over every live callsign. The fan-out
above (with `confirm_route=true`) does it server-side, capped at 20
in parallel, with caching.

### Route confirmation can fail — read `route_match` before claiming

`/api/flights/find` runs a layered confirmation pipeline against TWO
independent upstreams in parallel:

1. **OpenSky `/flights/aircraft`** (per-airframe, ICAO24-keyed). Derived
   from real ADS-B tracks — when a plane transitions on-ground →
   airborne, OpenSky materialises a flight record with
   `estDepartureAirport` set. **Authoritative for origin.** This is
   the same source the side drawer uses to caption "From: KBWI".
2. **adsbdb `/v0/callsign`** (callsign-keyed, scheduled route). Tells
   us "UAL108 typically flies IAD→LAX". Less authoritative than
   OpenSky for origin (callsigns get reused for repositioning legs,
   charters, etc.) but it's our only signal for *intended destination*
   of an in-progress flight.
3. **Geometric scoring** (altitude + climb rate + proximity + heading)
   as the last-resort fallback when both upstreams come back empty.

Each candidate carries a `route_match` label so you know which tier
matched (and why):

| `route_match`            | what it means                                                                                              |
|--------------------------|------------------------------------------------------------------------------------------------------------|
| `confirmed-opensky`      | OpenSky says this airframe took off from the requested airport (or recently arrived at the requested arrival airport). **Strongest signal.** |
| `confirmed`              | adsbdb's scheduled route matches departing/arriving as requested. Trust for destination claims.            |
| `geometric-departure`    | low alt + climbing + close + heading outbound — looks like a real takeoff but no upstream confirmed it     |
| `geometric-arrival`      | low alt + descending + close + heading inbound — same idea, mirrored                                       |
| `wrong-route`            | adsbdb has a route, but it's the wrong airport                                                             |
| `wrong-airport-opensky`  | OpenSky says this airframe took off from a *different* real airport. **Filtered out automatically** when departing= is set; only surfaced as a last resort. |
| `not-confirmed`          | neither upstream had usable data AND geometry was inconclusive                                             |

Plus per-candidate `departure_score` / `arrival_score` (≈10 = textbook,
≈0 = inconclusive, <0 = looks like the opposite) and an
`opensky_origin` block when the OpenSky tier matched. The list is
sorted: confirmed-opensky → confirmed → geometric → not-confirmed.
Pick the first row.

Required disclaimers when committing the camera:

* `confirmed-opensky` → say "tracking UAL108 — confirmed departure
  from KIAD per OpenSky ADS-B history". You can quote the
  `opensky_origin.first_seen` timestamp as the takeoff time.
* `confirmed` (adsbdb only) → say "tracking AAL123 (IAD → TPA per
  adsbdb)".
* `geometric-departure` / `geometric-arrival` → say "best geometric
  match — neither OpenSky nor adsbdb confirmed origin, but the
  aircraft is at 2,000 ft climbing out of IAD". Do NOT claim a
  destination unless adsbdb actually returned one. Do NOT claim a
  specific origin airport — just describe the geometry.
* `wrong-airport-opensky` (only appears as last-resort) → say "I
  could not find a confirmed departure from IAD; the closest match
  is actually departing from KBWI" and DO NOT track.
* If only `not-confirmed` rows came back, tell the user "no live
  flight matches a recent IAD departure right now" and DO NOT track.

**Anti-pattern to avoid:** the geometric heuristic alone will mark
nearby-field departures (e.g. KBWI flights climbing through KIAD's
150 km bubble heading west) as plausible KIAD departures. Always
prefer rows tagged `confirmed-opensky` over `geometric-*` when
attributing origin — the user's drawer panel will quote
`opensky_origin.estDepartureAirport` and any disagreement reads as
the agent making things up.

## How to look up a specific flight

For "where did this flight come from? / where is it going?" reads
that don't change the map, the per-flight read endpoints are still
the right tool:

```bash
# user: "where did UAL123 come from?"

# 1. Resolve the callsign to an icao24 by scanning the live state vectors.
#    Callsigns are space-padded by OpenSky and case-insensitive.
ICAO24=$(curl -s "http://127.0.0.1:18890/api/flights" \
  | python3 -c "import sys, json; r=json.load(sys.stdin)['flights']; \
                print(next((f['id'] for f in r if (f.get('callsign') or '').strip().upper()=='UAL123'), ''))")

# 2. Pull the recent flight summary (origin + destination + timing).
curl -s "http://127.0.0.1:18890/api/flight/$ICAO24"

# 3. (Optional) pull the waypoint track for "where it has been".
curl -s "http://127.0.0.1:18890/api/flight/$ICAO24/track?time=0"

# 4. (Optional) put it on the map — but if the user asked you to
#    "track" or "follow" the plane, use /api/map/track above instead.
curl -sX POST http://127.0.0.1:18890/api/map/highlight \
     -H 'Content-Type: application/json' \
     -d "{\"flight\":\"$ICAO24\"}"
```

`/api/flight/{icao24}` returns a `latest` object with `departure` /
`arrival` (each is a curated airport record: `icao`, `iata`, `name`,
`city`, `country`, `lat/lon`) plus `first_seen` / `last_seen` unix
timestamps. When OpenSky has no recent flight on file, `latest` is
`null` — say "no recent flight on file" rather than guessing.

`/api/flight/{icao24}/track` returns `available: false` if OpenSky's
tracks endpoint has no data for that aircraft (it's documented as
experimental and is missing for some callsigns). Fall back to "track
not published" in that case rather than fabricating waypoints.

A common failure mode in cloud deployments: OpenSky's `tracks/all`
endpoint is rate-limited per egress IP much more aggressively than
`states/all`, and routinely returns 403 anonymously from sandboxed
hosts even while `states/all` keeps working. The browser still draws
a cyan→deep-blue trail in that case, by falling back to the locally
accumulated fix history accumulated each state-vector poll — but the
trail only goes back as far as the page session.

To get the upstream historical track (~30 minutes back, denser
sampling), set OAuth client credentials on the server before launch:

```
OPENSKY_CLIENT_ID=<id>
OPENSKY_CLIENT_SECRET=<secret>
```

Free accounts at opensky-network.org → "Profile" → "API client" → "New
client". The token manager in `server.py` picks them up automatically
and switches every OpenSky call to authenticated, which gives a
per-account quota and much looser rate-limiter on `tracks/all`.

## How to change the aircraft colour scheme

The map already ships with **four** colour presets baked into the
client. The chat agent's job is to **flip the active preset**, not
to invent a custom palette and describe what it might look like.
Both the map renderer and the legend on the right rail are driven
from `state.colorMode`; `POST /api/map/color` is the only handle.

> **🚦 Rule: any user request that mentions colouring planes — "color
> by altitude", "show me climbing vs descending", "make emergencies
> stand out" — REQUIRES a `POST /api/map/color`. Do not describe a
> colour change in chat before issuing the POST. Do not describe a
> palette that isn't one of the four below — the user is looking at
> a real legend that won't match a hallucinated description.**

### The four canonical presets

| `mode`     | Aliases the server accepts                                                | What the user actually sees on screen                                                                                                        |
|------------|---------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| `phase`    | `phase of flight`, `flight phase`, `default`                              | **Orange family.** Cruise traffic: light to deep burnt orange (slow → fast). Climbing: blends toward warm yellow. Descending: brick red.     |
| `altitude` | `elevation`, `alt`, `fl`, `flight level`                                  | **Single-hue green ramp.** Pale mint at the surface, vibrant green around FL250, deep saturated green at FL400+. *Not* a rainbow.            |
| `vrate`    | `vertical rate`, `climb`, `descent`, `climb/descent`, `rate of climb`, `v/s`, `vs` | **Diverging purple palette.** Strong descent → deep violet, level → pale lilac, strong climb → bright magenta. Surfaces who's changing altitude.  |
| `squawk`   | `emergency`, `emergencies`, `alerts`, `transponder`                       | **Muted grey for normal traffic + saturated red/amber for the three reserved squawks.** 7500 = hot red, 7600 = amber, 7700 = bright red.      |

### Worked examples — issue the POST first, then describe

```bash
# user: "color the planes by altitude"
curl -sX POST http://127.0.0.1:18890/api/map/color \
     -H 'Content-Type: application/json' \
     -d '{"mode":"altitude"}'
# THEN reply: "Switched to the altitude preset. Lower-altitude
# traffic shows in pale mint and rises through vibrant green to a
# deep saturated green at FL400+." (Match the actual palette.)

# user: "show me who's climbing or descending"
curl -sX POST http://127.0.0.1:18890/api/map/color \
     -d '{"mode":"vrate"}' -H 'Content-Type: application/json'

# user: "any emergencies right now?"
curl -sX POST http://127.0.0.1:18890/api/map/color \
     -d '{"mode":"squawk"}' -H 'Content-Type: application/json'
# Then read /api/flights and surface any flight whose squawk is
# 7500 (hijack), 7600 (radio fail), or 7700 (general emergency).

# user: "go back to normal"
curl -sX POST http://127.0.0.1:18890/api/map/color \
     -d '{"mode":"phase"}' -H 'Content-Type: application/json'
```

The endpoint accepts the aliases listed above, so you can usually
pass the user's wording verbatim. If the mode is unrecognised the
server returns `{"ok": false, "error": ..., "valid": [...]}` — do
not retry blindly, ask the user to pick from the four canonical
modes.

### Things this endpoint does NOT do

- It does **not** accept arbitrary RGB values, custom palettes, or
  colour names. The client has four hard-coded ramps; that is the
  full set. If the user asks for "color planes by airline" or
  "make commercial jets blue", say "the map only supports four
  built-in colour modes (phase, altitude, climb/descent, emergency
  squawk) — which one would you like?".
- It does **not** filter which planes are visible. Pair with
  `POST /api/map/filter` for that (see the chip-filter section).
- It does **not** colour the METAR weather stations. Those have
  their own four-mode endpoint at `POST /api/map/metar-color`
  (see the operational-data overlays section).

## Airspace reasoning

The map can render seven FAA AIS layers, split by query strategy:

**Global (cached server-side, full layer in GeoJSON):**
- `sua` — Special Use Airspace (Prohibited / Restricted / Warning / MOA / NSA)
- `classes` — Class B / C / D shells around busy airports
- `tfrs` — active Temporary Flight Restrictions
- `runways` — every paved/metal runway in the NAS, with operational status

**Bbox-only (queried per request, must pass `bbox=west,south,east,north`):**
- `taxiways` — every taxiway with operational status
- `obstacles` — Digital Obstacle File ≥200 ft AGL (towers, cranes, chimneys…)
- `ats` — ATS routes (RNAV, OCEAN, CONV, etc.)

You almost never need to fetch the raw GeoJSON yourself —
`/api/airspace/lookup` does point-in-polygon and nearest-neighbour reasoning
across any subset:

```bash
# what's at or near KIAD? (default datasets: sua,tfrs,runways)
curl -s "http://127.0.0.1:18890/api/airspace/lookup?lat=38.94&lon=-77.46&radius_km=80"

# include obstacles + taxiways + ATS routes too
curl -s "http://127.0.0.1:18890/api/airspace/lookup?lat=38.94&lon=-77.46&radius_km=20&datasets=sua,tfrs,runways,taxiways,obstacles,ats"

# does the user want to see SUAs while you analyse them?
curl -sX POST http://127.0.0.1:18890/api/map/layer \
     -H 'Content-Type: application/json' \
     -d '{"layer":"sua","visible":true}'
```

The lookup response has two buckets:
- `containing` — features whose polygon contains the point (or, for points
  and lines, whose centroid is within ~500 m). Mention these first.
- `nearby`     — features whose bounding box / centroid is within
  `radius_km`, sorted ascending by distance. Each entry is decorated with
  `distance_km` so you can quote distances to the user.

Field guide for each dataset:
- `sua/classes`: `name`, `type` (P=Prohibited, R=Restricted, W=Warning,
  A=Alert, M=MOA, N=NSA), `class` (B/C/D for Class), altitude floor/ceiling,
  times-of-use. Respect those — a restricted area that's "0800-2200 DAILY"
  is only relevant inside that window.
- `tfrs`: `name`, `notam`, `state`, `updated`. Surface the NOTAM key so the
  user can look it up on tfr.faa.gov.
- `runways/taxiways`: `airport`, `runway`/`taxiway` designator, `surface`
  (hard/paved, metal, other), `status` (open / closed / under construction /
  closed indefinitely / repurposed as taxiway / unknown). When a major
  runway is *closed* or *under construction*, that's worth calling out —
  it changes which traffic flow an arrival can expect.
- `obstacles`: `type`, `agl_ft`, `msl_ft`, `lighting`, `location`. AGL is
  height above ground level — a 1000+ ft tower near an approach corridor
  is a real hazard; flag those.
- `ats`: `ident`, `type` (RNAV / CONV / OCEAN / DIR / GRNAV / UCON / AKCAP),
  `level`, `max_authorized_alt`, `hours`. Use these when the user asks
  about routings ("what airway is N12345 on?").
- `artcc`: `ident` (3-letter Center id like ZID/ZNY/ZAB), `name` (long
  form like "INDIANAPOLIS"), `local_type` (`ARTCC_L` for the low-altitude
  sectorisation we render). Use when the user asks "which Center owns
  Denver?", "who hands KIAD off to KCLE?", or "show me the boundary
  between New York Center and Boston Center".
- `navaids`: `ident` (3- or 4-letter id like IAD or HVQ), `name` (long
  form), `class` (VOR / VOR-DME / VORTAC / DME / TACAN / NDB / ILS /
  LOC), `channel` (TACAN/DME channel or VHF freq), `status`, `city`,
  `state`. Use these to surface inbound corridor fixes ("what NAVAIDs
  feed the ILS 1L approach into KIAD?", "is the TACAN at HVQ in
  service?"). The full IAP/SID/STAR linework is not exposed by the
  FAA AIS service — render NAVAIDs along the inbound corridor as a
  proxy for "show the published procedure".

## Operational-data overlays (30-min demo features)

Four small data feeds make the chart actually useful for live operations:

### METAR (weather observations)

```bash
# Where is it currently IFR/LIFR in the Northeast?
curl -s "http://127.0.0.1:18890/api/weather/metar?bbox=-80,38,-66,46" \
  | python3 -c "import sys,json; r=json.load(sys.stdin)['stations']; \
                print([s['station'] for s in r if s['flt_cat'] in ('IFR','LIFR')])"

# Show the layer on the map for the user
curl -sX POST http://127.0.0.1:18890/api/map/layer -d '{"layer":"metar","visible":true}' -H 'Content-Type: application/json'
```

`flt_cat` is one of `VFR / MVFR / IFR / LIFR`. Each station record
also carries the raw METAR (`raw`), wind, temperature, dewpoint,
visibility, altimeter — quote those when answering "what's the wind
at KIAD right now?".

### NAS Status (Ground Stops, GDPs, closures)

For *any* question about ground stops, ground delay programs, airspace
closures, departure delays, or arrival metering, **always** call
`/api/nas/status` (or the per-airport variant) first. Do not answer
inferentially from `/api/flights` — `/api/flights` shows traffic, not
flow-control orders, and is unable to detect an active GDP.

```bash
# Anything happening right now?
curl -s http://127.0.0.1:18890/api/nas/status | jq '.by_severity, .events | length'

# Per-airport: "is JFK delayed?"
curl -s http://127.0.0.1:18890/api/nas/airport/JFK
```

`severity` ranks `info < advisory < delay < ground_stop < closure`. A
ground stop or closure is the most operationally meaningful — surface
those first ("ATL has a ground stop due to thunderstorms until 22:30Z").

If the call returns HTTP 200 with `count: 0`, the honest answer is
"no NAS advisories in effect right now" — that's a real, normal state,
not a sign the upstream is broken.

### Aircraft registry / route

```bash
# user: "who flies a8ae7e?"
curl -s http://127.0.0.1:18890/api/registry/a8ae7e

# user: "where is UAL123 going?"
curl -s http://127.0.0.1:18890/api/route/UAL123
```

`/api/registry` returns registration (e.g. N12345), manufacturer,
type, ICAO type code, registered owner, country, and a photo URL when
one is on file. `/api/route` resolves callsigns to origin/destination/
airline using adsbdb. Both endpoints return `found: false` when the
upstream has nothing — say "no registry data on file" rather than
guessing.

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
