"""Flight Tracking Integration — sandbox-side FastAPI server.

Serves a single-page MapLibre + deck.gl frontend, proxies the OpenSky Network
API for live aircraft state, exposes a curated OpenFlights airport dataset,
runs a tool-calling chat against the gateway-configured OpenAI-compatible
inference endpoint, and broadcasts external map commands to all connected
browsers over a WebSocket bus.

Design notes
------------
- Runs entirely inside the OpenShell sandbox. The browser reaches it through
  `openshell forward start <sandbox> 0.0.0.0:18890` (the install script
  configures this). No host-side proxy is involved in this demo — API keys
  live in the sandbox env (per user request) and outbound traffic is gated
  by the network policy preset in policy/flight-tracking.yaml.
- OpenSky is reachable anonymously (10s cadence, bbox <= 25 sq deg). If
  OPENSKY_USERNAME / OPENSKY_PASSWORD are set we use HTTP Basic to lift the
  rate limit to 5s. Responses are cached briefly in-process to avoid
  hammering the API when several browsers are open.
- The chat endpoint posts to the existing inference route configured by
  `openshell inference` (NVIDIA Nemotron via inference-api.nvidia.com in the
  default install). The LLM is given function-call tools that resolve to
  internal helpers and broadcast map commands back to the frontend over the
  WebSocket bus. The same tools are exposed as plain HTTP endpoints so the
  OpenClaw skill can drive the same surface from Telegram or the dashboard.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ── Constants ───────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = STATIC_DIR / "data"

OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_USER = os.getenv("OPENSKY_USERNAME", "").strip()
OPENSKY_PASS = os.getenv("OPENSKY_PASSWORD", "").strip()
OPENSKY_CACHE_TTL = 8.0  # seconds — slightly under anonymous 10s rate limit

# Inference endpoint — read from env at startup. Defaults match the standard
# NemoClaw OpenAI-compatible route. Install.sh fills these in from the
# gateway's stored inference config so the server doesn't need to talk to
# the gateway at request time.
INFERENCE_BASE_URL = os.getenv(
    "INFERENCE_BASE_URL", "https://inference-api.nvidia.com/v1"
).rstrip("/")
INFERENCE_API_KEY = os.getenv("INFERENCE_API_KEY", "").strip()
INFERENCE_MODEL = os.getenv("INFERENCE_MODEL", "nvidia/nvidia/nemotron-3-super-v3").strip()

DEFAULT_ANALYSIS_RADIUS_KM = 80.0
EARTH_RADIUS_KM = 6371.0


# ── Airport dataset ─────────────────────────────────────────────────────────


def _load_airports() -> list[dict[str, Any]]:
    path = DATA_DIR / "airports.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    if isinstance(raw, dict) and "airports" in raw:
        raw = raw["airports"]
    return raw


AIRPORTS: list[dict[str, Any]] = _load_airports()
AIRPORT_BY_IATA: dict[str, dict[str, Any]] = {
    a["code"].upper(): a for a in AIRPORTS if a.get("code")
}
AIRPORT_BY_ICAO: dict[str, dict[str, Any]] = {
    a["icao"].upper(): a for a in AIRPORTS if a.get("icao")
}


def find_airport(token: str) -> dict[str, Any] | None:
    """Resolve a free-form airport reference (IATA, ICAO, or city/name)."""

    if not token:
        return None
    t = token.strip().upper()
    if t in AIRPORT_BY_IATA:
        return AIRPORT_BY_IATA[t]
    if t in AIRPORT_BY_ICAO:
        return AIRPORT_BY_ICAO[t]
    needle = token.strip().lower()
    candidates = [
        a
        for a in AIRPORTS
        if needle in a.get("name", "").lower() or needle in a.get("city", "").lower()
    ]
    if not candidates:
        return None
    # Prefer the most "important" hit by a small heuristic — large_airport > medium > small
    weight = {"large_airport": 3, "medium_airport": 2, "small_airport": 1}
    candidates.sort(key=lambda a: weight.get(a.get("type", ""), 0), reverse=True)
    return candidates[0]


# ── Geometry helpers ────────────────────────────────────────────────────────


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bbox_from_center(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Return (south, north, west, east) bbox containing a circle of radius_km."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


# ── OpenSky proxy with simple in-process cache ──────────────────────────────


class OpenSkyCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def get(self, key: str) -> list[dict[str, Any]] | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry and (time.time() - entry[0]) < OPENSKY_CACHE_TTL:
                return entry[1]
            return None

    async def set(self, key: str, flights: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._entries[key] = (time.time(), flights)
            # keep memory bounded — drop anything older than 60s
            cutoff = time.time() - 60
            self._entries = {k: v for k, v in self._entries.items() if v[0] >= cutoff}


_cache = OpenSkyCache()
_http: httpx.AsyncClient | None = None


def _opensky_auth_header() -> dict[str, str]:
    if OPENSKY_USER and OPENSKY_PASS:
        token = base64.b64encode(f"{OPENSKY_USER}:{OPENSKY_PASS}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def _decode_state(row: list[Any]) -> dict[str, Any] | None:
    """Convert OpenSky's positional state vector into a typed dict.

    Schema (OpenSky 'states/all'):
      0:  icao24      (str)
      1:  callsign    (str|None)
      2:  origin_country (str)
      3:  time_position (int|None, unix s)
      4:  last_contact (int)
      5:  longitude   (float|None, deg)
      6:  latitude    (float|None, deg)
      7:  baro_altitude (float|None, m)
      8:  on_ground   (bool)
      9:  velocity    (float|None, m/s)
      10: true_track  (float|None, deg)
      11: vertical_rate (float|None, m/s)
      13: geo_altitude (float|None, m)
      14: squawk      (str|None)
    """
    if len(row) < 11 or row[5] is None or row[6] is None:
        return None
    return {
        "id": str(row[0] or "").strip().lower(),
        "callsign": (row[1] or "").strip() or None,
        "country": row[2] or None,
        "last_seen": row[4] or 0,
        "lon": float(row[5]),
        "lat": float(row[6]),
        "alt_m": float(row[7]) if row[7] is not None else (float(row[13]) if len(row) > 13 and row[13] is not None else None),
        "on_ground": bool(row[8]),
        "vel_mps": float(row[9]) if row[9] is not None else None,
        "heading": float(row[10]) if row[10] is not None else 0.0,
        "vrate_mps": float(row[11]) if len(row) > 11 and row[11] is not None else None,
        "squawk": row[14] if len(row) > 14 else None,
    }


async def fetch_flights(
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Fetch live flights, cached briefly to respect OpenSky rate limits."""
    if _http is None:
        raise RuntimeError("HTTP client not initialised")

    key = "global" if bbox is None else f"{bbox[0]:.2f},{bbox[1]:.2f},{bbox[2]:.2f},{bbox[3]:.2f}"
    cached = await _cache.get(key)
    if cached is not None:
        return {"flights": cached, "fetched_from": "cache"}

    params: dict[str, Any] = {}
    if bbox is not None:
        s, n, w, e = bbox
        params = {"lamin": s, "lamax": n, "lomin": w, "lomax": e}

    try:
        r = await _http.get(
            OPENSKY_URL,
            params=params,
            headers=_opensky_auth_header(),
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"opensky upstream error: {exc}") from exc

    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="opensky rate limit reached")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"opensky returned {r.status_code}")

    payload = r.json()
    states = payload.get("states") or []
    flights: list[dict[str, Any]] = []
    for row in states:
        decoded = _decode_state(row)
        if decoded is not None:
            flights.append(decoded)

    await _cache.set(key, flights)
    return {"flights": flights, "fetched_from": "live", "fetched_at": payload.get("time")}


# ── WebSocket bus (push map commands to all connected browsers) ─────────────


class MapBus:
    """Fan-out hub for map commands generated outside the browser session."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> int:
        async with self._lock:
            targets = list(self._clients)
        delivered = 0
        for ws in targets:
            try:
                await ws.send_json(message)
                delivered += 1
            except Exception:
                # best-effort — let the receive loop clean up dead sockets
                pass
        return delivered


_bus = MapBus()


# ── LLM tool definitions ────────────────────────────────────────────────────


CHAT_SYSTEM_PROMPT = """You are FlightOps, a flight-tracking copilot embedded in a live map.

Capabilities:
- Drive the map: fly to airports, change layers, draw arcs, highlight specific flights.
- Inspect data: query live aircraft near an airport, summarise traffic, surface anomalies.

Style:
- Be terse. Pilot-radio cadence. No fluff.
- When the user names an airport (IATA or ICAO), call goto first so the map moves while you analyse.
- After analyse_traffic, summarise in 3-5 short bullets: count, mix (climb/cruise/descent), notable callsigns or country mix, anything unusual (squawk 7500/7600/7700, ground stops).
- Never invent flight numbers. If data is missing, say "no contact" rather than guess.
"""


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "goto",
            "description": "Fly the map to an airport, city, or coordinate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "IATA, ICAO, or free-form name."},
                    "zoom": {"type": "number", "default": 9},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_traffic",
            "description": "Summarise live aircraft within a radius of an airport. Returns counts, vertical-mode mix, notable squawks, country mix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "airport": {"type": "string"},
                    "radius_km": {"type": "number", "default": DEFAULT_ANALYSIS_RADIUS_KM},
                },
                "required": ["airport"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_arcs_to_airport",
            "description": "Draw great-circle arcs from each live aircraft within range to the named airport. Useful for showing inbound traffic patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "airport": {"type": "string"},
                    "radius_km": {"type": "number", "default": DEFAULT_ANALYSIS_RADIUS_KM},
                },
                "required": ["airport"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_layer",
            "description": "Toggle a map layer on or off. Layers: airports, flights, arcs, trails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer": {"type": "string", "enum": ["airports", "flights", "arcs", "trails"]},
                    "visible": {"type": "boolean"},
                },
                "required": ["layer", "visible"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "highlight_flight",
            "description": "Highlight a single flight by ICAO24 hex or callsign and draw its recent trail.",
            "parameters": {
                "type": "object",
                "properties": {"flight": {"type": "string"}},
                "required": ["flight"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_airports",
            "description": "Free-text search of the airport directory. Returns up to 6 matches.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


# ── Tool implementations (also exposed via plain HTTP for the skill) ────────


def _vertical_mode(vrate_mps: float | None) -> str:
    if vrate_mps is None:
        return "unknown"
    if vrate_mps > 1.5:
        return "climb"
    if vrate_mps < -1.5:
        return "descent"
    return "cruise"


async def tool_goto(target: str, zoom: float | None = None) -> dict[str, Any]:
    a = find_airport(target)
    if a is None:
        return {"ok": False, "error": f"No airport matched '{target}'."}
    payload = {
        "type": "goto",
        "lat": a["lat"],
        "lon": a["lon"],
        "zoom": zoom or 9,
        "label": f"{a['code']} — {a['name']}",
    }
    await _bus.broadcast(payload)
    return {"ok": True, **payload, "airport": a}


async def tool_analyze_traffic(airport: str, radius_km: float = DEFAULT_ANALYSIS_RADIUS_KM) -> dict[str, Any]:
    a = find_airport(airport)
    if a is None:
        return {"ok": False, "error": f"No airport matched '{airport}'."}
    bbox = bbox_from_center(a["lat"], a["lon"], radius_km)
    feed = await fetch_flights(bbox)
    flights = feed["flights"]
    nearby: list[dict[str, Any]] = []
    for f in flights:
        if haversine_km(a["lat"], a["lon"], f["lat"], f["lon"]) <= radius_km:
            nearby.append(f)

    vmodes = {"climb": 0, "cruise": 0, "descent": 0, "unknown": 0}
    countries: dict[str, int] = {}
    notable_squawks: list[dict[str, Any]] = []
    on_ground = 0
    for f in nearby:
        if f["on_ground"]:
            on_ground += 1
            continue
        vmodes[_vertical_mode(f["vrate_mps"])] += 1
        if f["country"]:
            countries[f["country"]] = countries.get(f["country"], 0) + 1
        sq = (f.get("squawk") or "").strip()
        if sq in {"7500", "7600", "7700"}:
            notable_squawks.append({"callsign": f["callsign"], "squawk": sq, "id": f["id"]})

    top_countries = sorted(countries.items(), key=lambda kv: kv[1], reverse=True)[:3]
    summary = {
        "airport": a,
        "radius_km": radius_km,
        "total": len(nearby),
        "airborne": len(nearby) - on_ground,
        "on_ground": on_ground,
        "vertical_modes": vmodes,
        "top_countries": [{"country": c, "count": n} for c, n in top_countries],
        "notable_squawks": notable_squawks,
        "fetched_from": feed.get("fetched_from"),
    }
    return {"ok": True, "summary": summary}


async def tool_show_arcs_to_airport(airport: str, radius_km: float = DEFAULT_ANALYSIS_RADIUS_KM) -> dict[str, Any]:
    a = find_airport(airport)
    if a is None:
        return {"ok": False, "error": f"No airport matched '{airport}'."}
    bbox = bbox_from_center(a["lat"], a["lon"], radius_km)
    feed = await fetch_flights(bbox)
    arcs = []
    for f in feed["flights"]:
        if f["on_ground"]:
            continue
        if haversine_km(a["lat"], a["lon"], f["lat"], f["lon"]) > radius_km:
            continue
        arcs.append(
            {
                "from": [f["lon"], f["lat"]],
                "to": [a["lon"], a["lat"]],
                "id": f["id"],
                "callsign": f["callsign"],
                "alt_m": f["alt_m"],
            }
        )
    payload = {"type": "arcs", "airport": a["code"], "arcs": arcs}
    await _bus.broadcast(payload)
    return {"ok": True, "count": len(arcs), "airport": a["code"]}


async def tool_set_layer(layer: str, visible: bool) -> dict[str, Any]:
    payload = {"type": "layer", "layer": layer, "visible": bool(visible)}
    await _bus.broadcast(payload)
    return {"ok": True, **payload}


async def tool_highlight_flight(flight: str) -> dict[str, Any]:
    payload = {"type": "highlight", "flight": flight.strip().upper()}
    await _bus.broadcast(payload)
    return {"ok": True, **payload}


def tool_search_airports(query: str) -> dict[str, Any]:
    q = (query or "").strip().lower()
    if not q:
        return {"ok": True, "matches": []}
    matches: list[dict[str, Any]] = []
    for a in AIRPORTS:
        haystack = f"{a.get('code', '')} {a.get('icao', '')} {a.get('name', '')} {a.get('city', '')}".lower()
        if q in haystack:
            matches.append({"code": a["code"], "icao": a["icao"], "name": a["name"], "city": a["city"], "country": a["country"], "lat": a["lat"], "lon": a["lon"]})
        if len(matches) >= 6:
            break
    return {"ok": True, "matches": matches}


TOOL_DISPATCH = {
    "goto": tool_goto,
    "analyze_traffic": tool_analyze_traffic,
    "show_arcs_to_airport": tool_show_arcs_to_airport,
    "set_layer": tool_set_layer,
    "highlight_flight": tool_highlight_flight,
    "search_airports": lambda **kw: asyncio.sleep(0, result=tool_search_airports(**kw)),
}


# ── Chat loop with tool calling ─────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)


async def _call_inference(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not INFERENCE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="INFERENCE_API_KEY not configured. Re-run install.sh after `nemoclaw onboard`.",
        )
    if _http is None:
        raise RuntimeError("HTTP client not initialised")

    body = {
        "model": INFERENCE_MODEL,
        "messages": messages,
        "tools": TOOLS_SCHEMA,
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 600,
    }
    r = await _http.post(
        f"{INFERENCE_BASE_URL}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {INFERENCE_API_KEY}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    return r.json()


async def run_chat(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Multi-turn tool-calling loop bounded to 4 tool rounds."""
    convo: list[dict[str, Any]] = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        *messages,
    ]
    actions: list[dict[str, Any]] = []
    for _ in range(4):
        response = await _call_inference(convo)
        choice = response["choices"][0]
        msg = choice["message"]
        convo.append(msg)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return {"reply": msg.get("content", ""), "actions": actions}

        for call in tool_calls:
            name = call["function"]["name"]
            args_raw = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {}
            handler = TOOL_DISPATCH.get(name)
            if handler is None:
                tool_result: dict[str, Any] = {"ok": False, "error": f"unknown tool {name}"}
            else:
                try:
                    tool_result = await handler(**args)  # type: ignore[arg-type]
                except TypeError:
                    tool_result = handler(**args)  # type: ignore[assignment]
            actions.append({"tool": name, "args": args, "result": tool_result})
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": json.dumps(tool_result, default=str),
                }
            )
    # Hit the budget — return whatever the last assistant message had.
    last_assistant = next((m for m in reversed(convo) if m.get("role") == "assistant"), None)
    return {"reply": (last_assistant or {}).get("content", "[tool-call budget exhausted]"), "actions": actions}


# ── App + lifespan ──────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _http
    _http = httpx.AsyncClient(http2=False)
    try:
        yield
    finally:
        await _http.aclose()
        _http = None


app = FastAPI(
    title="Flight Tracking Integration",
    description="Live aircraft tracking with deck.gl + OpenClaw skill bridge.",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "airports_loaded": len(AIRPORTS),
        "opensky_authenticated": bool(OPENSKY_USER and OPENSKY_PASS),
        "inference_configured": bool(INFERENCE_API_KEY),
        "model": INFERENCE_MODEL,
    }


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bbox must be 'west,south,east,north'") from exc
    if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90):
        raise HTTPException(status_code=400, detail="bbox out of range")
    return (s, n, w, e)


@app.get("/api/flights")
async def api_flights(
    bbox: str | None = Query(default=None, description="west,south,east,north"),
):
    parsed = _parse_bbox(bbox)
    return await fetch_flights(parsed)


@app.get("/api/airports")
async def api_airports(
    bbox: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
):
    parsed = _parse_bbox(bbox)
    out: list[dict[str, Any]] = []
    if parsed is None:
        out = AIRPORTS[:limit]
    else:
        s, n, w, e = parsed
        for a in AIRPORTS:
            if s <= a["lat"] <= n and w <= a["lon"] <= e:
                out.append(a)
                if len(out) >= limit:
                    break
    return {"airports": out, "count": len(out)}


@app.get("/api/airport/{code}")
async def api_airport(code: str):
    a = find_airport(code)
    if a is None:
        raise HTTPException(status_code=404, detail="airport not found")
    return a


@app.get("/api/analyze")
async def api_analyze(airport: str, radius_km: float = DEFAULT_ANALYSIS_RADIUS_KM):
    return await tool_analyze_traffic(airport, radius_km)


class GotoBody(BaseModel):
    target: str
    zoom: float | None = None


@app.post("/api/map/goto")
async def api_map_goto(body: GotoBody):
    return await tool_goto(body.target, body.zoom)


class ArcBody(BaseModel):
    airport: str
    radius_km: float = DEFAULT_ANALYSIS_RADIUS_KM


@app.post("/api/map/arcs")
async def api_map_arcs(body: ArcBody):
    return await tool_show_arcs_to_airport(body.airport, body.radius_km)


class LayerBody(BaseModel):
    layer: str
    visible: bool


@app.post("/api/map/layer")
async def api_map_layer(body: LayerBody):
    return await tool_set_layer(body.layer, body.visible)


class HighlightBody(BaseModel):
    flight: str


@app.post("/api/map/highlight")
async def api_map_highlight(body: HighlightBody):
    return await tool_highlight_flight(body.flight)


class CommandBody(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/map/command")
async def api_map_command(body: CommandBody):
    """Generic broadcast — the OpenClaw skill posts here for arbitrary events."""
    msg = {"type": body.type, **body.payload}
    delivered = await _bus.broadcast(msg)
    return {"ok": True, "delivered": delivered, "message": msg}


@app.post("/api/chat")
async def api_chat(body: ChatRequest):
    serialised: list[dict[str, Any]] = []
    for m in body.messages:
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.name:
            entry["name"] = m.name
        if m.tool_call_id:
            entry["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            entry["tool_calls"] = m.tool_calls
        serialised.append(entry)
    return await run_chat(serialised)


@app.websocket("/ws/map")
async def ws_map(ws: WebSocket):
    await _bus.connect(ws)
    try:
        # We don't expect inbound messages — just keep the socket open.
        # Reading is required to detect disconnects in some browsers.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await _bus.disconnect(ws)


# Static file mount (frontend assets) — must come last so /api routes win.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html)


@app.get("/favicon.ico")
async def favicon():
    fav = STATIC_DIR / "favicon.svg"
    if fav.exists():
        return JSONResponse(status_code=200, content=None)
    return JSONResponse(status_code=204, content=None)


# Allow running directly: `python server.py`
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("FLIGHT_APP_PORT", "18890"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
