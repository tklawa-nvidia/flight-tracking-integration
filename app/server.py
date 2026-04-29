"""Flight Tracking Integration — sandbox-side FastAPI server.

Serves a single-page MapLibre + deck.gl frontend, proxies the OpenSky Network
API for live aircraft state, exposes a curated OpenFlights airport dataset,
forwards the chat panel to OpenClaw's local agent (which already has the
flight-tracking skill loaded), and broadcasts external map commands to all
connected browsers over a WebSocket bus.

Design notes
------------
- Runs entirely inside the OpenShell sandbox. The browser reaches it through
  `openshell forward start <sandbox> 0.0.0.0:18890` (the install script
  configures this).
- OpenSky is reached one of two ways:
    * **Tier-1 host proxy (default)** — `OPENSKY_PROXY_URL` points at a
      Python daemon running on the host (outside the sandbox) which
      reads OAuth2 credentials from `~/.nemoclaw/credentials.json`,
      mints/refreshes bearer tokens, and forwards to opensky-network.org.
      The sandbox itself never sees the client_id or secret. Mirrors
      the Planet integration's `planet-proxy.py` pattern.
    * **Direct (legacy / dev)** — when `OPENSKY_PROXY_URL` is unset and
      `OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET` are present in the
      env, we run the OAuth2 client_credentials dance ourselves.
      Anonymous fallback (~400 credits/day) when neither is set.
  Responses are cached briefly in-process either way so several open
  browsers don't burn through the daily credit budget.
- The chat panel does *not* call inference directly. It exec's
  `openclaw agent --json` so OpenClaw owns auth, model selection, skill
  routing, and conversation memory — exactly the way the TUI works. The
  flight-tracking skill we deploy at install time gives that agent the
  recipes it needs to drive the map (curl into /api/map/*).
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ── Constants ───────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = STATIC_DIR / "data"

# OpenSky access has two modes:
#   1. Tier-1 host proxy (preferred). When OPENSKY_PROXY_URL is set the
#      sandbox calls a Python daemon running on the host. That daemon
#      reads the OAuth2 client_credentials from
#      ~/.nemoclaw/credentials.json, mints/refreshes the bearer token,
#      and forwards to opensky-network.org. The sandbox itself never
#      sees the client_id/secret — same pattern Planet uses.
#   2. Direct OAuth2 from inside the sandbox (legacy / dev). Used when
#      OPENSKY_PROXY_URL is empty AND OPENSKY_CLIENT_ID/SECRET are set
#      in the process env. Kept so dev loops outside the install.sh
#      flow still work without standing up the host daemon.
OPENSKY_PROXY_URL = os.getenv("OPENSKY_PROXY_URL", "").strip().rstrip("/")
OPENSKY_DIRECT_BASE = "https://opensky-network.org"
OPENSKY_BASE = OPENSKY_PROXY_URL or OPENSKY_DIRECT_BASE
OPENSKY_URL = f"{OPENSKY_BASE}/api/states/all"
OPENSKY_FLIGHTS_URL = f"{OPENSKY_BASE}/api/flights/aircraft"
OPENSKY_TRACKS_URL = f"{OPENSKY_BASE}/api/tracks/all"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "").strip()
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
# Legacy basic-auth env vars are still read so existing installs keep
# working, but OpenSky removed Basic auth in March 2026 — these now only
# apply to internal forks/mirrors that still accept it.
OPENSKY_USER = os.getenv("OPENSKY_USERNAME", "").strip()
OPENSKY_PASS = os.getenv("OPENSKY_PASSWORD", "").strip()
OPENSKY_CACHE_TTL = 8.0  # seconds — slightly under anonymous 10s rate limit

# ── External operational/weather data sources ──────────────────────────────
# All of these are free, public, key-less, and CORS-friendly. They're proxied
# through the sandbox so the network policy stays auditable and so we get a
# server-side cache between the upstream and the browser. None of them carry
# auth, so failures are usually rate-limit or maintenance windows; we treat
# every error as "just don't render that overlay" and never block the chart.

# Aviation Weather Center (AWC) and FAA NAS Status both block requests
# coming from the cloud ASN that the openshell sandbox egresses through
# (returns 403 Forbidden), even though they're public, no-auth endpoints
# from a browser or VM. To work around that we route them through a
# small host-side forwarder, `faa-proxy.py`, the same way OpenSky goes
# through `opensky-proxy.py`. When FAA_PROXY_URL is set the constants
# below resolve to the proxy; otherwise they hit the real upstream
# (used for dev outside the sandbox).
FAA_PROXY_URL = os.getenv("FAA_PROXY_URL", "").strip().rstrip("/")
AWC_METAR_URL = (
    f"{FAA_PROXY_URL}/awc/api/data/metar"
    if FAA_PROXY_URL
    else "https://aviationweather.gov/api/data/metar"
)
METAR_CACHE_TTL = 5 * 60  # AWC publishes hourly; 5 min keeps the chart fresh

# FAA NAS Status — Air Traffic Control System Command Center publishes
# every active airport-level event (Ground Stop, Ground Delay Program,
# Airport Closure, AFP, deicing, etc.) as a single JSON payload. The
# response is one object per affected airport with sub-objects for each
# event type (groundStop, groundDelay, airportClosure, freeForm, …).
NAS_STATUS_URL = (
    f"{FAA_PROXY_URL}/nas/api/airport-events"
    if FAA_PROXY_URL
    else "https://nasstatus.faa.gov/api/airport-events"
)
NAS_CACHE_TTL = 90  # NAS Status updates whenever a new advisory is posted

# Aircraft + flight-route registry. adsbdb.com aggregates the FAA
# Releasable Aircraft Database, the EASA registry, OpenSky's metadata,
# and Plane Spotters photo links into a single REST surface. Free,
# anonymous, ~1 req/s is plenty for a demo. We use it for two things:
#   - GET /v0/aircraft/<icao24>   → registration, type, operator, photo
#   - GET /v0/callsign/<callsign> → origin/destination + airline
# hexdb.io serves as a fallback for the aircraft lookup if adsbdb is
# down — same data shape, slightly less rich.
ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft"
ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign"
HEXDB_AIRCRAFT_URL = "https://hexdb.io/api/v1/aircraft"
REGISTRY_CACHE_TTL = 24 * 3600  # registrations rarely change day-to-day

# OpenClaw integration — chat is a thin wrapper around `openclaw agent`.
# The binary lives at /usr/local/bin/openclaw inside the sandbox image; we
# look it up dynamically in case a future image moves it.
OPENCLAW_BIN = shutil.which("openclaw") or "/usr/local/bin/openclaw"
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT", "main").strip()
OPENCLAW_TIMEOUT_S = int(os.getenv("OPENCLAW_TIMEOUT_S", "180"))
# Where the agent writes its per-session JSONL transcripts. We read
# these post-call to surface tool calls + thinking back to the chat
# UI when the user has the "Show tool calls / thinking" toggle on.
OPENCLAW_AGENT_HOME = os.getenv(
    "OPENCLAW_AGENT_HOME",
    f"/sandbox/.openclaw-data/agents/{OPENCLAW_AGENT}",
).rstrip("/")
OPENCLAW_SESSIONS_DIR = f"{OPENCLAW_AGENT_HOME}/sessions"

DEFAULT_ANALYSIS_RADIUS_KM = 80.0
EARTH_RADIUS_KM = 6371.0

# ── FAA AIS airspace datasets ──────────────────────────────────────────────
# All three are public, key-less, and return GeoJSON when asked nicely.
# We cache them server-side so repeated map loads don't hammer the FAA, and
# so the chat agent's `airspace_lookup` tool can answer point queries from
# memory in microseconds. SUA + Class are updated on the FAA's 56-day cycle;
# TFRs are dynamic so we re-pull every 30 minutes.
ARCGIS_BASE = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services"
)

# Datasets we cache *globally* — small enough to fetch once and keep around.
# Each entry gets a long TTL because the upstream changes on the FAA's
# 56-day AIRAC cycle (or once a day for TFRs) and we'd rather serve stale
# data than block the chart on a slow refetch.
FAA_DATASETS: dict[str, dict[str, Any]] = {
    "sua": {
        "url": f"{ARCGIS_BASE}/Special_Use_Airspace/FeatureServer/0/query",
        "params": {
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
            "resultRecordCount": 4000,
        },
        "ttl_s": 24 * 3600,
        "label": "Special Use Airspace",
    },
    "classes": {
        # The Class_Airspace layer holds 6,000+ polygons and the FAA's
        # ArcGIS instance is *catastrophically* slow on `IN` queries (>2 min)
        # even with trimmed outFields, but a single-value `=` query returns
        # in ~10s. So we ask in parallel for each class we care about and
        # merge the results in fetch_airspace(). Class E is intentionally
        # skipped — it's 4,300+ polygons and ruins the chart.
        "url": f"{ARCGIS_BASE}/Class_Airspace/FeatureServer/0/query",
        "fanout": [
            {"where": "CLASS='B'"},
            {"where": "CLASS='C'"},
            {"where": "CLASS='D'"},
            {"where": "TYPE_CODE='MODE-C'"},
        ],
        "params": {
            "outFields": "TYPE_CODE,CLASS,LOCAL_TYPE,IDENT,ICAO_ID,NAME,"
                          "UPPER_VAL,UPPER_UOM,UPPER_CODE,"
                          "LOWER_VAL,LOWER_UOM,LOWER_CODE",
            "f": "geojson",
            "resultRecordCount": 2000,
        },
        "ttl_s": 24 * 3600,
        "label": "Class Airspace",
    },
    "tfrs": {
        "url": "https://tfr.faa.gov/geoserver/TFR/ows",
        "params": {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": "TFR:V_TFR_LOC",
            "maxFeatures": 500,
            "outputFormat": "application/json",
        },
        "ttl_s": 30 * 60,
        "label": "Temporary Flight Restrictions",
    },
    "runways": {
        # ~240 polygons across the entire NAS — cheap to grab globally.
        "url": f"{ARCGIS_BASE}/AM_Runway/FeatureServer/0/query",
        "params": {
            "where": "1=1",
            "outFields": "FAA_ID,ICAO_ID,DESIGNATOR,SURFACE,RWY_OPER,RWY_ID",
            "f": "geojson",
            "resultRecordCount": 4000,
        },
        "ttl_s": 24 * 3600,
        "label": "Airport Runways",
    },
    "artcc": {
        # Air Route Traffic Control Center boundaries. Boundary_Airspace
        # is a multi-purpose layer (FIRs, ARTCCs, ADIZs, etc.); we filter
        # to LOCAL_TYPE='ARTCC_L' which is the low-altitude (effectively
        # surface-to-FL230) ARTCC sectorisation that pilots associate
        # with "the Center". 21 polygons total — cheap to keep globally.
        # IDENT is the three-letter centre id (ZID, ZNY, ZAB, …) and
        # NAME is the long form (INDIANAPOLIS, NEW YORK, …).
        "url": f"{ARCGIS_BASE}/Boundary_Airspace/FeatureServer/0/query",
        "params": {
            "where": "TYPE_CODE='ARTCC' AND LOCAL_TYPE='ARTCC_L'",
            "outFields": "IDENT,NAME,TYPE_CODE,LOCAL_TYPE,UPPER_VAL,UPPER_UOM,UPPER_CODE,LOWER_VAL,LOWER_UOM,LOWER_CODE",
            "f": "geojson",
            "resultRecordCount": 200,
        },
        "ttl_s": 24 * 3600,
        "label": "ARTCC Boundaries",
    },
}
_airspace_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_airspace_locks: dict[str, asyncio.Lock] = {k: asyncio.Lock() for k in FAA_DATASETS}

# Datasets that are too large to cache globally — must always be queried
# with a bbox or `where` clause. We expose them via /api/airspace/{name}
# but require a bbox parameter; results are cached per-bbox for a short
# window so successive moveend pumps don't refetch the same square.
FAA_BBOX_DATASETS: dict[str, dict[str, Any]] = {
    "taxiways": {
        # ~12k polygons — fine in airport-scale bboxes, brutal globally.
        "url": f"{ARCGIS_BASE}/AM_Taxiway/FeatureServer/0/query",
        "outFields": "FAA_ID,ICAO_ID,DESIGNATOR,SURFACE,TWY_OPER",
        "max_records": 1500,
        "label": "Airport Taxiways",
    },
    "obstacles": {
        # 629k points nationwide — must filter aggressively. We ask the
        # FAA service for AGL >= 200 ft so we mostly surface towers,
        # cranes, and chimneys rather than light poles. The bbox keeps
        # the result set well under 2k features.
        "url": f"{ARCGIS_BASE}/Digital_Obstacle_File/FeatureServer/0/query",
        "outFields": "OAS_Number,Type_Code,Quantity,AGL,AMSL,Lighting,"
                     "City,State,Verified",
        "where_extra": "AGL >= 200",
        "max_records": 1500,
        "label": "Digital Obstacle File",
    },
    "ats": {
        # 18k linestrings nationwide. Bbox keeps the chart legible.
        "url": f"{ARCGIS_BASE}/ATS_Route/FeatureServer/0/query",
        "outFields": "IDENT,TYPE_CODE,LEVEL_,WKHR_CODE,MAA_VAL,MAA_UOM,"
                     "MEA_E_VAL,MEA_W_VAL",
        "max_records": 2000,
        "label": "ATS Routes",
    },
    "navaids": {
        # ~3,400 points nationwide — the radio aids (VOR/VORTAC/DME/TACAN/
        # NDB/ILS components) that approach plates and SIDs/STARs hang off.
        # We surface them on the chart as a stand-in for "show the
        # published procedure" because the full IAP/SID/STAR linework
        # isn't available as open polylines from the FAA AIS service —
        # but every IFR procedure references a chain of these fixes, so
        # rendering the NAVAIDs along an inbound corridor recreates the
        # spine of the approach you'd see on a chart. We restrict to
        # NAVAIDs flagged for US low- or high-altitude IFR use so the
        # display matches what's on an enroute chart.
        "url": f"{ARCGIS_BASE}/NAVAIDSystem/FeatureServer/0/query",
        "outFields": "IDENT,NAME_TXT,CLASS_TXT,CHANNEL,STATUS,CITY,STATE",
        "where_extra": "(US_LOW=1 OR US_HIGH=1) AND STATUS='IFR'",
        "max_records": 1500,
        "label": "Navaids (VOR/VORTAC/DME/TACAN)",
    },
}
# Per-(name, bbox) cache. Short TTL because users pan around a lot.
_bbox_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_bbox_cache_lock = asyncio.Lock()
BBOX_CACHE_TTL = 5 * 60  # 5 min — long enough for chat reasoning to reuse
BBOX_CACHE_MAX = 64


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


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from (lat1, lon1) toward (lat2, lon2), degrees [0, 360).

    Used by `/api/flights/find` to score how well a live aircraft's
    reported heading matches the bearing toward a hypothetical
    destination — i.e. "is this plane that just left IAD actually
    pointed at Tampa, or is it just airborne in the same neighborhood?"
    """
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def heading_misalignment_deg(reported_heading: float, target_bearing: float) -> float:
    """Smallest absolute difference between two compass headings (0..180°).

    `(290 - 10)` should be 80°, not 280°. We want the wrap-around
    minimum so a heading of 10° is "close" to a bearing of 350°.
    """
    diff = (reported_heading - target_bearing + 540.0) % 360.0 - 180.0
    return abs(diff)


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


class OpenSkyTokenManager:
    """OAuth2 client_credentials token manager for the OpenSky REST API.

    The /states/all endpoint accepts the bearer token issued by Keycloak at
    auth.opensky-network.org. Tokens are short-lived (typically 30 min) so
    we cache the current one until shortly before its `expires_in` window
    elapses, then refresh on demand.
    """

    LEAD_SECONDS = 60  # refresh this many seconds before expiry

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET)

    async def get(self) -> str | None:
        if not self.configured:
            return None
        if _http is None:
            return None
        async with self._lock:
            if self._token and time.time() < self._expires_at - self.LEAD_SECONDS:
                return self._token
            try:
                r = await _http.post(
                    OPENSKY_TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": OPENSKY_CLIENT_ID,
                        "client_secret": OPENSKY_CLIENT_SECRET,
                    },
                    timeout=15.0,
                )
            except httpx.RequestError as exc:
                # Don't crash the request — fall back to anonymous and let
                # the caller decide whether to surface the issue.
                return None
            if r.status_code != 200:
                return None
            try:
                payload = r.json()
            except Exception:
                return None
            self._token = payload.get("access_token")
            ttl = float(payload.get("expires_in", 1800))
            self._expires_at = time.time() + ttl
            return self._token


_opensky_tokens = OpenSkyTokenManager()


async def _opensky_auth_header() -> dict[str, str]:
    """Return the appropriate Authorization header for the OpenSky API.

    Order of preference:
      0. None when OPENSKY_PROXY_URL is set — the host-side proxy owns
         the credentials and injects Bearer auth itself. Keeping our
         own Authorization header here would duplicate (and leak) the
         legacy in-sandbox token via the proxy hop.
      1. OAuth2 client_credentials (the only auth OpenSky supports as of
         March 2026 for new installs) when running direct from the
         sandbox.
      2. Legacy HTTP Basic, kept for backwards compatibility with internal
         mirrors / older deployments. Only used if OAuth2 isn't configured.
    """
    if OPENSKY_PROXY_URL:
        return {}
    token = await _opensky_tokens.get()
    if token:
        return {"Authorization": f"Bearer {token}"}
    if OPENSKY_USER and OPENSKY_PASS:
        encoded = base64.b64encode(f"{OPENSKY_USER}:{OPENSKY_PASS}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
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
            headers=await _opensky_auth_header(),
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


# ── Per-aircraft flight lookups ─────────────────────────────────────────────
# OpenSky exposes two read-only endpoints that let us answer "where did
# this flight come from?" and "what route has it flown today?":
#
#   /api/flights/aircraft?icao24=...&begin=...&end=...
#       returns flight summaries: estDepartureAirport / estArrivalAirport
#       (ICAO codes), firstSeen, lastSeen, callsign — i.e. enough to
#       caption a flight as "DEN → IAD, dep 14:32, est arr 18:01".
#
#   /api/tracks/all?icao24=...&time=0
#       returns the recent waypoint track as
#       [(time, lat, lon, alt_m, heading, on_ground), ...]. We use this
#       to draw the "where it has been" cyan-blue gradient line on the
#       map when the user clicks a plane.
#
# Both are cached for FLIGHT_LOOKUP_TTL seconds so a chatty UI (or a chat
# agent that asks twice) doesn't burn through the daily credit budget.
# `/tracks/all` is documented as "experimental" by OpenSky and can return
# 404 for some aircraft / deployments; we treat that as "unavailable"
# rather than an error so the rest of the drawer still renders.

FLIGHT_LOOKUP_TTL = 60.0
FLIGHT_HISTORY_LOOKBACK_S = 24 * 3600  # last 24 h covers most "where from?" cases
_flight_cache: dict[str, tuple[float, Any]] = {}
_flight_cache_lock = asyncio.Lock()


async def _flight_cache_get(key: str) -> Any | None:
    async with _flight_cache_lock:
        entry = _flight_cache.get(key)
        if entry and time.time() - entry[0] < FLIGHT_LOOKUP_TTL:
            return entry[1]
        return None


async def _flight_cache_set(key: str, value: Any) -> None:
    async with _flight_cache_lock:
        _flight_cache[key] = (time.time(), value)
        # Bound memory at ~256 entries; drop anything older than 2×TTL.
        if len(_flight_cache) > 256:
            cutoff = time.time() - FLIGHT_LOOKUP_TTL * 2
            for k in list(_flight_cache):
                if _flight_cache[k][0] < cutoff:
                    _flight_cache.pop(k, None)


def _airport_summary(icao: str | None) -> dict[str, Any] | None:
    """Resolve an OpenSky-reported ICAO code into a curated airport row.

    OpenSky publishes the *estimated* departure/arrival airport as a
    4-letter ICAO code. If we know the airport in our OpenFlights
    bundle, we return the full record (name, city, country, lat/lon).
    If we don't recognise the code we still return a stub with just the
    ICAO so the drawer/chat can show *something* rather than nothing.
    """
    if not icao:
        return None
    a = AIRPORT_BY_ICAO.get(icao.strip().upper())
    if not a:
        return {
            "icao": icao.upper(),
            "iata": None, "name": None, "city": None, "country": None,
            "lat": None, "lon": None,
        }
    return {
        "icao": a.get("icao"),
        "iata": a.get("code"),
        "name": a.get("name"),
        "city": a.get("city"),
        "country": a.get("country"),
        "lat": a.get("lat"),
        "lon": a.get("lon"),
    }


async def fetch_aircraft_flights(
    icao24: str,
    lookback_s: int = FLIGHT_HISTORY_LOOKBACK_S,
) -> list[dict[str, Any]]:
    """Recent flights flown by `icao24` with origin/destination if known."""
    icao24 = icao24.strip().lower()
    if not icao24:
        return []
    cache_key = f"flights:{icao24}:{lookback_s}"
    cached = await _flight_cache_get(cache_key)
    if cached is not None:
        return cached

    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    end = int(time.time())
    begin = end - lookback_s
    try:
        r = await _http.get(
            OPENSKY_FLIGHTS_URL,
            params={"icao24": icao24, "begin": begin, "end": end},
            headers=await _opensky_auth_header(),
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"opensky upstream error: {exc}") from exc
    if r.status_code == 404:
        # OpenSky returns 404 when nothing is found for the window —
        # surface as an empty list rather than an error.
        await _flight_cache_set(cache_key, [])
        return []
    if r.status_code == 429:
        raise HTTPException(429, "opensky rate limit reached")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"opensky returned {r.status_code}")
    try:
        data = r.json()
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    data.sort(key=lambda f: f.get("lastSeen") or 0, reverse=True)
    await _flight_cache_set(cache_key, data)
    return data


async def fetch_aircraft_track(icao24: str, time_s: int = 0) -> dict[str, Any] | None:
    """Return the recent waypoint track for `icao24`, or None if none.

    `time_s = 0` asks OpenSky for the most recent flight for this aircraft.
    Older flights can be queried by passing a unix timestamp inside that
    flight's window. The endpoint can be unavailable on some deployments;
    we treat 404/410 as "no data" so the caller can degrade gracefully.
    """
    icao24 = icao24.strip().lower()
    if not icao24:
        return None
    cache_key = f"track:{icao24}:{time_s}"
    cached = await _flight_cache_get(cache_key)
    if cached is not None:
        return cached

    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    try:
        r = await _http.get(
            OPENSKY_TRACKS_URL,
            params={"icao24": icao24, "time": time_s},
            headers=await _opensky_auth_header(),
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"opensky upstream error: {exc}") from exc
    if r.status_code in (404, 410):
        await _flight_cache_set(cache_key, None)
        return None
    if r.status_code == 429:
        raise HTTPException(429, "opensky rate limit reached")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"opensky returned {r.status_code}")
    try:
        data = r.json()
    except Exception:
        data = None
    await _flight_cache_set(cache_key, data)
    return data


# ── WebSocket bus (push map commands to all connected browsers) ─────────────


# How long an agent-driven "sticky" map command is replayed to fresh
# WebSocket connections. Long enough to survive a browser reconnect blip,
# a uvicorn restart, or a tab-visibility flap mid-conversation. Short
# enough that a user reloading the page hours later isn't yanked back to
# wherever the agent last pointed the camera.
STICKY_TTL_SEC = 180


def _sticky_key(message: dict[str, Any]) -> str | None:
    """Return a slot key for messages that should be replayed on reconnect.

    `goto` and `view` share the `camera` slot because either one fully
    re-targets the camera — the most recent of either should win. Layers
    and filters get one slot per layer/mode so toggling 'metar' on doesn't
    forget that 'tfrs' was also turned on. Returning None opts the message
    out of replay (chat messages, transient toasts, etc.).
    """
    t = message.get("type")
    if t in ("goto", "view"):
        return "camera"
    if t in ("highlight", "color", "metar-color", "airspace3d", "arcs"):
        return t
    if t == "layer":
        layer = message.get("layer")
        return f"layer:{layer}" if layer else None
    if t == "filter":
        mode = message.get("mode")
        return f"filter:{mode}" if mode else None
    return None


class MapBus:
    """Fan-out hub for map commands generated outside the browser session.

    Also remembers the most recent agent-driven map state per "sticky slot"
    (camera pose, selected flight, layer/color/filter toggles, …) and
    replays it to any client that reconnects within ``STICKY_TTL_SEC``.
    Without this, every WebSocket blip silently dropped agent commands —
    the API still returned `ok` because no clients were subscribed at the
    instant of broadcast, so the agent had no way to know the message was
    lost.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # Keyed by _sticky_key(msg) → (monotonic_set_at, message_dict).
        self._sticky: dict[str, tuple[float, dict[str, Any]]] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        now = time.time()
        async with self._lock:
            self._clients.add(ws)
            # Drop expired entries on every connect — cheap garbage collect
            # and avoids unbounded growth if the agent keeps spamming the
            # bus while no client is around.
            stale = [
                k for k, (ts, _) in self._sticky.items()
                if now - ts > STICKY_TTL_SEC
            ]
            for k in stale:
                self._sticky.pop(k, None)
            replay = [msg for _, (_, msg) in sorted(self._sticky.items())]
        for msg in replay:
            try:
                await ws.send_json(msg)
            except Exception:
                # If the just-accepted socket is already dead, give up
                # quietly — the receive loop will run disconnect().
                return

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> int:
        key = _sticky_key(message)
        async with self._lock:
            if key is not None:
                self._sticky[key] = (time.time(), message)
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


# ── Tool implementations (exposed via plain HTTP for the skill) ─────────────


def _vertical_mode(vrate_mps: float | None) -> str:
    if vrate_mps is None:
        return "unknown"
    if vrate_mps > 1.5:
        return "climb"
    if vrate_mps < -1.5:
        return "descent"
    return "cruise"


async def tool_goto(
    target: str,
    zoom: float | None = None,
    pitch: float | None = None,
    bearing: float | None = None,
) -> dict[str, Any]:
    """Pan the map to an airport.

    `pitch` and `bearing` are optional 3D camera hints. Pass `pitch` to
    angle the camera (0 = top-down, 60 ≈ "looking across the chart");
    pass `bearing` to rotate the compass heading the camera faces. Both
    are forwarded to the browser's MapLibre flyTo. They're useful when
    the agent wants the user to see depth — most often when drawing
    inbound arcs (which read as flat lines from straight overhead but
    as 3D parabolas with a 50–60° tilt). Out of range values are
    clamped on the browser side.
    """
    a = find_airport(target)
    if a is None:
        return {"ok": False, "error": f"No airport matched '{target}'."}
    payload: dict[str, Any] = {
        "type": "goto",
        "lat": a["lat"],
        "lon": a["lon"],
        "zoom": zoom or 9,
        "label": f"{a['code']} — {a['name']}",
    }
    if pitch is not None:
        payload["pitch"] = float(pitch)
    if bearing is not None:
        payload["bearing"] = float(bearing)
    delivered = await _bus.broadcast(payload)
    return {"ok": True, "delivered": delivered, **payload, "airport": a}


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


async def tool_show_arcs_to_airport(
    airport: str,
    radius_km: float = DEFAULT_ANALYSIS_RADIUS_KM,
    *,
    tilt: bool = True,
) -> dict[str, Any]:
    """Draw inbound-traffic arcs into an airport.

    The arcs themselves are flat great-circle ribbons computed by
    deck.gl's ArcLayer; they look like a tangled flat hairball when the
    camera is straight down. Pass `tilt=True` (default) to also broadcast
    a `goto` with a ~55° pitch so the parabolic arcs read as 3D ribbons
    converging on the airport — the visual the user has in mind when
    they say "show me the inbound arcs". Set `tilt=False` if the user
    is on a flat-only review (or already framed the camera themselves).
    """
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
    if tilt:
        # Two zoom levels are picked so the airport sits in the lower
        # third of the viewport at the requested radius — keeps the
        # arc apex visible without zooming so far out the parabolas
        # collapse. 55° pitch is empirically the sweet spot: enough
        # depth that the arcs read as ribbons, not so much that the
        # horizon shows and basemap labels start projecting weirdly.
        # We send `goto` first so the camera is already settled when
        # the ArcLayer paints.
        zoom_for_radius = 8.5 if radius_km <= 90 else 7.5
        await _bus.broadcast(
            {
                "type": "goto",
                "lat": a["lat"],
                "lon": a["lon"],
                "zoom": zoom_for_radius,
                "pitch": 55,
                "bearing": 0,
                "label": f"{a['code']} — {a['name']}",
            }
        )
    payload = {"type": "arcs", "airport": a["code"], "arcs": arcs}
    delivered = await _bus.broadcast(payload)
    return {
        "ok": True,
        "delivered": delivered,
        "count": len(arcs),
        "airport": a["code"],
        "tilted": bool(tilt),
    }


async def tool_set_layer(layer: str, visible: bool) -> dict[str, Any]:
    payload = {"type": "layer", "layer": layer, "visible": bool(visible)}
    delivered = await _bus.broadcast(payload)
    return {"ok": True, "delivered": delivered, **payload}


async def tool_highlight_flight(flight: str) -> dict[str, Any]:
    payload = {"type": "highlight", "flight": flight.strip().upper()}
    delivered = await _bus.broadcast(payload)
    return {"ok": True, "delivered": delivered, **payload}


async def tool_track_flight(
    *,
    callsign: str | None = None,
    icao24:   str | None = None,
    zoom:     float | None = None,
    pitch:    float | None = None,
    bearing:  float | None = None,
) -> dict[str, Any]:
    """Combined plane-lookup + highlight + camera-move.

    Resolves the live flight against the global OpenSky feed, then issues
    BOTH a `highlight` broadcast (the browser uses it to set
    selectedFlightId, enable the trails layer, and open the detail
    drawer) AND a `view` broadcast carrying the plane's current lat/lon.
    The double-broadcast matters: the `highlight` handler tries to fly
    the camera too, but it can no-op for a few seconds if the bus
    message arrives before the next /api/flights tick has populated the
    client-side flight index. The follow-up `view` guarantees the camera
    arrives even in that race.

    Prefer this over driving /api/map/highlight + /api/map/view by hand:
    one round-trip, atomic from the agent's side, and if the flight
    isn't in the live feed we tell the agent immediately ("no live
    contact") instead of silently broadcasting a highlight the browser
    drops on the floor.
    """
    cs   = (callsign or "").strip().upper()
    hex24 = (icao24 or "").strip().lower()
    if not cs and not hex24:
        return {"ok": False, "error": "provide callsign or icao24"}

    feed = await fetch_flights(bbox=None)
    candidates: list[dict[str, Any]] = feed.get("flights") or []

    matched: dict[str, Any] | None = None
    if hex24:
        for f in candidates:
            if (f.get("id") or "").lower() == hex24 \
                    or (f.get("icao24") or "").lower() == hex24:
                matched = f
                break
    if matched is None and cs:
        for f in candidates:
            if (f.get("callsign") or "").strip().upper() == cs:
                matched = f
                break

    if matched is None:
        return {
            "ok": False,
            "error": f"no live contact for callsign={cs!r} icao24={hex24!r}",
            "hint": (
                "The plane may not be in OpenSky's current window. Try "
                "/api/flight/{icao24} for historical route, or "
                "/api/flights?bbox=... to scan a region for similar "
                "callsigns."
            ),
        }

    flight_id     = (matched.get("id") or matched.get("icao24") or "").upper()
    callsign_disp = (matched.get("callsign") or "").strip()
    lat = matched.get("lat")
    lon = matched.get("lon")

    delivered_h = await _bus.broadcast(
        {"type": "highlight", "flight": flight_id}
    )

    view: dict[str, Any] = {"type": "view"}
    if lat is not None: view["lat"] = float(lat)
    if lon is not None: view["lon"] = float(lon)
    view["zoom"] = float(zoom) if zoom is not None else 10.0
    if pitch   is not None: view["pitch"]   = float(pitch)
    if bearing is not None: view["bearing"] = float(bearing)
    delivered_v = await _bus.broadcast(view)

    return {
        "ok": True,
        # Either of the two broadcasts hitting at least one client is
        # success enough — the highlight handler's flyTo will catch up
        # the rest. We surface the higher of the two so the agent can
        # warn about an empty bus without false alarms during a brief
        # client churn.
        "delivered": max(delivered_h, delivered_v),
        "flight": {
            "id":         flight_id,
            "icao24":     (matched.get("id") or matched.get("icao24") or "").lower(),
            "callsign":   callsign_disp,
            "lat":        lat,
            "lon":        lon,
            "alt_m":      matched.get("alt_m"),
            "vrate_mps":  matched.get("vrate_mps"),
            "heading":    matched.get("heading"),
            "ground_speed_mps": matched.get("vel_mps") or matched.get("ground_speed_mps"),
            "on_ground":  matched.get("on_ground"),
            "country":    matched.get("country"),
            "squawk":     matched.get("squawk"),
        },
    }


# ── Flight discovery: find live flights matching a route / heuristic ─────
# This is the "look up first, then track" half of the find→pick→track
# pattern. Without it, the agent's only path to "what just left IAD
# heading to TPA?" was to fetch /api/flights and call /api/route per
# callsign — hundreds of HTTP calls inside a tool exec, hangs the
# turn, and the chat ends up rendering an empty assistant message
# as "completed". Pushing the matching server-side keeps the agent's
# tool calls under one second.

# Defaults chosen to match how a controller naturally describes a
# departure: ~150 km radius captures planes within ~10 minutes of
# wheels-up at common climb rates; +/-35° heading tolerance forgives
# initial turn-out vectors before the plane is fully on course.
_FIND_DEFAULT_RADIUS_KM = 150.0
_FIND_DEFAULT_HEADING_TOL_DEG = 35.0
_FIND_DEFAULT_LIMIT = 10
# Cap on parallel /api/route lookups when `confirm_route=true`. Each
# call hits adsbdb.com (already rate-limited per egress IP); 20 keeps
# us under the limit for cold caches without blowing the 1s budget.
_FIND_MAX_ROUTE_CONFIRM = 20
# Cap on parallel OpenSky /flights/aircraft lookups for the
# authoritative-origin tier (see _opensky_recent_flight). Capped lower
# than adsbdb because each call goes through the host proxy and
# burns OpenSky credits; 10 still covers "top candidates the user
# would care about" with the prelim sort by last_seen.
_FIND_MAX_OPENSKY_ORIGIN = 10
# How fresh the most-recent OpenSky flight record has to be for us to
# trust it as "the leg this plane is on right now". OpenSky materialises
# a flight record shortly after it detects a takeoff (usually within
# a minute or two); if the record is older than ~6 hours, the plane has
# been on the ground long enough that the prior leg's airports tell us
# nothing useful about today's departure.
_FIND_OPENSKY_FRESH_S = 6 * 3600


def _airport_codes(a: dict[str, Any] | None) -> set[str]:
    """All known codes (ICAO + IATA + lookup `code`) for a curated
    airport row, uppercased and with empties dropped. Used to cross-check
    upstream-provided airport codes (which may be ICAO from OpenSky or
    IATA from adsbdb) against our resolved target airport."""
    if not a:
        return set()
    out = {
        (a.get("icao") or "").upper(),
        (a.get("iata") or "").upper(),
        (a.get("code") or "").upper(),
    }
    out.discard("")
    return out


async def _opensky_recent_flight(icao24: str) -> dict[str, Any] | None:
    """Most recent flight record for `icao24` from OpenSky, or None.

    Used by tool_find_flights as the *authoritative* origin source —
    OpenSky's /flights/aircraft is derived from real ADS-B tracks
    (when an aircraft transitions on-ground -> airborne it materialises
    a record with estDepartureAirport set), so it can correctly tell us
    "this plane just took off from KBWI, not KIAD" even when adsbdb's
    callsign-keyed scheduled-route table comes back empty.

    A 6-hour lookback is plenty: anything older than that won't
    represent the leg the plane is currently flying, so we don't need
    the full 24h window the drawer uses. Fail-soft to None on any
    error so the caller falls back to adsbdb / geometric scoring.
    """
    try:
        flights = await fetch_aircraft_flights(icao24, lookback_s=_FIND_OPENSKY_FRESH_S)
    except HTTPException:
        return None
    return flights[0] if flights else None


def _classify_route_match(
    f:           dict[str, Any],
    departing_a: dict[str, Any] | None,
    arriving_a:  dict[str, Any] | None,
) -> str:
    """Layered confidence classification for a candidate flight.

    Reads two enrichment fields the caller may have populated on `f`:
      - `_opensky_origin`: most recent OpenSky flights/aircraft record
                           (authoritative — derived from real ADS-B).
      - `route`           : adsbdb route lookup (callsign-keyed,
                            scheduled route, fragile).

    Returns the canonical `_route_match` label:
      * `confirmed-opensky`     — OpenSky says this plane took off from
                                  the requested departing airport, OR
                                  the most recent completed leg arrived
                                  there and the plane is now airborne
                                  (i.e. it just lifted off again).
                                  Also covers "recently arrived at the
                                  requested arriving airport".
      * `wrong-airport-opensky` — OpenSky's most recent leg started at
                                  a different real airport within the
                                  freshness window. Strong signal that
                                  the geometric heuristic was fooled by
                                  a nearby field (e.g. BWI departure
                                  climbing through IAD's bubble).
      * `confirmed`             — adsbdb's scheduled route matches the
                                  departing/arriving filter. Less
                                  authoritative than OpenSky for
                                  origin, but useful for *destination*
                                  confirmation since OpenSky doesn't
                                  know the destination of in-progress
                                  flights.
      * `wrong-route`           — adsbdb confirms a route, just not
                                  the one the user asked about.
      * `not-confirmed`         — neither source had usable data;
                                  geometric scoring decides next.
    """
    op = f.get("_opensky_origin")
    r  = f.get("route")
    now = time.time()

    op_dep_icao  = ""
    op_arr_icao  = ""
    op_first_seen = 0
    op_last_seen  = 0
    op_is_fresh = False
    if op:
        op_dep_icao   = (op.get("estDepartureAirport") or "").upper()
        op_arr_icao   = (op.get("estArrivalAirport")  or "").upper()
        op_first_seen = int(op.get("firstSeen") or 0)
        op_last_seen  = int(op.get("lastSeen")  or 0)
        latest_ts = max(op_first_seen, op_last_seen)
        op_is_fresh = (latest_ts > 0) and ((now - latest_ts) < _FIND_OPENSKY_FRESH_S)

    if departing_a is not None and op_is_fresh:
        want = _airport_codes(departing_a)
        # Case A: OpenSky has materialised the in-progress leg with
        # estDepartureAirport set to the airport we asked about.
        # Strongest possible confirmation — derived from real ADS-B.
        if op_dep_icao and op_dep_icao in want:
            return "confirmed-opensky"
        # Case B: the most recent COMPLETED leg ended at the requested
        # airport. We're seeing the plane in the live /states/all feed
        # right now, so it must have just lifted off from there again
        # before OpenSky materialised the new in-progress record.
        # (estDepartureAirport here describes the *previous* leg's
        # origin and is irrelevant to the current departure question.)
        if op_arr_icao and op_arr_icao in want:
            return "confirmed-opensky"
        # Case C: OpenSky says the plane is currently airborne FROM a
        # different real airport (no arrival yet — record is for the
        # in-progress leg). Definitively wrong airport.
        if op_dep_icao and not op_arr_icao:
            return "wrong-airport-opensky"
        # Case D: the latest completed leg landed at a different
        # airport within the freshness window AND we're seeing the
        # plane in the live feed now → it took off from THAT airport,
        # not ours. Also definitively wrong.
        if op_arr_icao:
            return "wrong-airport-opensky"

    if arriving_a is not None and op_is_fresh:
        want = _airport_codes(arriving_a)
        if op_arr_icao and op_arr_icao in want:
            return "confirmed-opensky"

    if r and (departing_a or arriving_a):
        origin_iata = ((r.get("origin")      or {}).get("iata") or "").upper()
        origin_icao = ((r.get("origin")      or {}).get("icao") or "").upper()
        dest_iata   = ((r.get("destination") or {}).get("iata") or "").upper()
        dest_icao   = ((r.get("destination") or {}).get("icao") or "").upper()
        ok = True
        if departing_a is not None:
            want = _airport_codes(departing_a)
            if not (want & {origin_iata, origin_icao}):
                ok = False
        if ok and arriving_a is not None:
            want = _airport_codes(arriving_a)
            if not (want & {dest_iata, dest_icao}):
                ok = False
        return "confirmed" if ok else "wrong-route"

    return "not-confirmed"

_FIND_PHASE_PREDICATES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "ground":   lambda f: bool(f.get("on_ground")),
    "airborne": lambda f: not bool(f.get("on_ground")),
    "climb":    lambda f: not bool(f.get("on_ground")) and (f.get("vrate_mps") or 0) >  1.5,
    "descent":  lambda f: not bool(f.get("on_ground")) and (f.get("vrate_mps") or 0) < -1.5,
    "cruise":   lambda f: not bool(f.get("on_ground")) and abs(f.get("vrate_mps") or 0) <= 1.5,
    "level":    lambda f: not bool(f.get("on_ground")) and abs(f.get("vrate_mps") or 0) <= 1.5,
}
_FIND_PHASE_ALIASES = {
    "departing": "climb", "takeoff": "climb", "climbing": "climb",
    "arriving":  "descent", "landing": "descent", "descending": "descent",
    "level":     "cruise",
}


def _score_departure_likelihood(
    f: dict[str, Any], airport: dict[str, Any], radius_km: float,
) -> float:
    """Heuristic 0..~12 score that this flight is a fresh departure
    from `airport`.

    Rewards low altitude, strong climb rate, proximity to the
    airport, and a heading that points radially outward from the
    airport's lat/lon. A high-altitude level plane at the edge of
    the search bubble (i.e. cruise transit overhead) scores ~0; a
    plane that's at 2000 ft, climbing 1500 fpm, 15 km out, and
    pointing away scores ~10. Used as the primary sort key when
    adsbdb route confirmation comes back empty — without this we
    fell back to `last_seen` and routinely picked transit traffic.
    """
    score = 0.0
    alt = f.get("alt_m") or 99999.0
    vrate = f.get("vrate_mps") or 0.0
    dist = f.get("_distance_km")
    on_ground = bool(f.get("on_ground"))

    if on_ground:
        # Already on the ground at the departure airport — nearly
        # certainly the plane the user means if they said "the latest
        # flight that just left", but we mark it positively without
        # the bonus from climb rate (which is 0 on the ground).
        if dist is not None and dist < 5:
            score += 4
        return score

    # Altitude: takeoffs live in the bottom of the column. 0..3000m
    # is the climbout zone where the picture is unambiguous.
    if alt < 1500:
        score += 5
    elif alt < 3000:
        score += 3.5
    elif alt < 5000:
        score += 1.5
    elif alt > 9000:
        score -= 3  # cruise — almost certainly transit, not departure

    # Vertical rate: climb is the smoking gun for a departure.
    if vrate > 7:
        score += 5
    elif vrate > 2.5:
        score += 3.5
    elif vrate > 0.5:
        score += 1.5
    elif vrate < -1.5:
        score -= 3  # descending = arrival, not departure

    # Distance from airport: the closer the plane is to the airport,
    # the more likely it just lifted off. Edge-of-bubble = transit.
    if dist is not None:
        if dist < radius_km * 0.10:
            score += 3   # < 15 km of a 150 km bubble — right on top
        elif dist < radius_km * 0.30:
            score += 2
        elif dist < radius_km * 0.60:
            score += 0.5
        elif dist > radius_km * 0.85:
            score -= 1.5  # at the bubble's edge — likely transit

    # Heading direction: a real departure points radially outward
    # from the airport. Score the alignment between the plane's
    # heading and the bearing from the airport to the plane.
    h = f.get("heading")
    plat, plon = f.get("lat"), f.get("lon")
    if h is not None and plat is not None and plon is not None:
        outbound_bearing = bearing_deg(
            airport["lat"], airport["lon"], float(plat), float(plon),
        )
        misalign = heading_misalignment_deg(float(h), outbound_bearing)
        if misalign < 30:
            score += 1.5
        elif misalign < 60:
            score += 0.5
        elif misalign > 120:
            score -= 1  # heading back toward the airport — arrival

    return score


def _score_arrival_likelihood(
    f: dict[str, Any], airport: dict[str, Any], radius_km: float,
) -> float:
    """Mirror of `_score_departure_likelihood` for arriving traffic.

    Rewards descending vertical rate, low-but-not-touchdown altitude,
    proximity, and a heading pointed radially toward the airport.
    """
    score = 0.0
    alt = f.get("alt_m") or 99999.0
    vrate = f.get("vrate_mps") or 0.0
    dist = f.get("_distance_km")
    on_ground = bool(f.get("on_ground"))

    if on_ground:
        if dist is not None and dist < 5:
            score += 4
        return score

    if alt < 600:
        score += 5      # short-final / threshold
    elif alt < 2500:
        score += 3.5    # base/final
    elif alt < 4500:
        score += 1.5    # downwind / approach
    elif alt > 9000:
        score -= 3      # cruise overhead

    if vrate < -7:
        score += 5
    elif vrate < -2.5:
        score += 3.5
    elif vrate < -0.5:
        score += 1.5
    elif vrate > 1.5:
        score -= 3      # climbing = departure, not arrival

    if dist is not None:
        if dist < radius_km * 0.10:
            score += 3
        elif dist < radius_km * 0.30:
            score += 2
        elif dist < radius_km * 0.60:
            score += 0.5
        elif dist > radius_km * 0.85:
            score -= 1.5

    h = f.get("heading")
    plat, plon = f.get("lat"), f.get("lon")
    if h is not None and plat is not None and plon is not None:
        # Inbound bearing is FROM the plane TO the airport — i.e. the
        # heading the plane should be pointing if it's lined up.
        inbound_bearing = bearing_deg(
            float(plat), float(plon), airport["lat"], airport["lon"],
        )
        misalign = heading_misalignment_deg(float(h), inbound_bearing)
        if misalign < 30:
            score += 1.5
        elif misalign < 60:
            score += 0.5
        elif misalign > 120:
            score -= 1

    return score


async def tool_find_flights(
    *,
    departing:        str | None = None,
    arriving:         str | None = None,
    near:             str | None = None,
    radius_km:        float | None = None,
    phase:            str | None = None,
    min_alt_m:        float | None = None,
    max_alt_m:        float | None = None,
    heading_deg:      float | None = None,
    heading_tol_deg:  float | None = None,
    since_seconds:    float | None = None,
    confirm_route:    bool | None = None,
    order:            str | None = None,
    limit:            int | None = None,
) -> dict[str, Any]:
    """Discover live flights matching a route filter or heuristic.

    Composable pre-step for `/api/map/track`: the agent calls this to
    pick a flight ID, then calls `/api/map/track {flight: <id>}` with
    the result. Never tries to do all of the search + map control in
    one call — keeps each tool call atomic and lets the agent show
    candidate matches to the user before committing the camera.

    Filters are AND'd together. All are optional; a call with no
    filters is equivalent to a CONUS scan with no ordering preference.

    Heuristics applied when both `departing` and `arriving` are given
    (the most common shape):
      * Search bbox = circle of `radius_km` around the departing
        airport's lat/lon (so we don't scan the whole CONUS feed).
      * Default `phase` = "climb" (a plane "leaving IAD" is nearly
        always still in the climb-out phase).
      * Default `heading_deg` = great-circle bearing from departing
        to arriving; default tolerance ±35°.
      * Default `order` = "latest" (latest `last_seen` wins ties).
      * `confirm_route` defaults true: top candidates are enriched
        with /api/route in parallel and the response includes the
        adsbdb route data for cross-checking.
    """
    departing_a = find_airport(departing) if departing else None
    if departing and departing_a is None:
        return {"ok": False, "error": f"unknown departing airport {departing!r}"}
    arriving_a = find_airport(arriving) if arriving else None
    if arriving and arriving_a is None:
        return {"ok": False, "error": f"unknown arriving airport {arriving!r}"}
    near_a = find_airport(near) if near else None
    if near and near_a is None:
        return {"ok": False, "error": f"unknown near airport {near!r}"}

    radius = float(radius_km) if radius_km is not None else _FIND_DEFAULT_RADIUS_KM

    # Pick a search center for the bbox (and for distance scoring).
    center = near_a or departing_a
    if center is not None:
        bbox = bbox_from_center(center["lat"], center["lon"], radius)
    else:
        bbox = None

    feed = await fetch_flights(bbox=bbox)
    candidates: list[dict[str, Any]] = list(feed.get("flights") or [])

    # Phase filter — accepts our canonical four plus the natural-language
    # aliases the SKILL.md tells the agent it can pass through verbatim.
    phase_default = "climb" if (departing_a is not None and arriving_a is not None) else None
    phase_key: str | None = None
    if phase:
        key = (phase or "").strip().lower()
        phase_key = _FIND_PHASE_ALIASES.get(key, key) if key else None
    elif phase_default:
        phase_key = phase_default
    if phase_key:
        pred = _FIND_PHASE_PREDICATES.get(phase_key)
        if pred is None:
            return {
                "ok": False,
                "error": f"unknown phase {phase!r}",
                "valid_phases": sorted(set(_FIND_PHASE_PREDICATES) | set(_FIND_PHASE_ALIASES)),
            }
        candidates = [f for f in candidates if pred(f)]

    if min_alt_m is not None:
        candidates = [f for f in candidates if (f.get("alt_m") or 0) >= float(min_alt_m)]
    if max_alt_m is not None:
        candidates = [f for f in candidates if (f.get("alt_m") or 0) <= float(max_alt_m)]
    if since_seconds is not None:
        cutoff = time.time() - float(since_seconds)
        candidates = [f for f in candidates if (f.get("last_seen") or 0) >= cutoff]

    # Heading filter — explicit value wins; otherwise derive from
    # departing→arriving great-circle bearing if both are known.
    target_bearing: float | None = None
    if heading_deg is not None:
        target_bearing = float(heading_deg) % 360.0
    elif departing_a is not None and arriving_a is not None:
        target_bearing = bearing_deg(
            departing_a["lat"], departing_a["lon"],
            arriving_a["lat"],  arriving_a["lon"],
        )
    tol = float(heading_tol_deg) if heading_tol_deg is not None else _FIND_DEFAULT_HEADING_TOL_DEG
    if target_bearing is not None:
        scored: list[dict[str, Any]] = []
        for f in candidates:
            h = f.get("heading")
            if h is None:
                continue
            misalign = heading_misalignment_deg(float(h), target_bearing)
            if misalign <= tol:
                f = {**f, "_heading_misalign_deg": misalign}
                scored.append(f)
        candidates = scored

    # Distance from departing airport (or `near`). Useful for scoring
    # "is this plane actually leaving X or just transiting overhead?".
    if center is not None:
        for f in candidates:
            f["_distance_km"] = haversine_km(
                f["lat"], f["lon"], center["lat"], center["lon"],
            )

    # Geometric likelihood scores. Used as the primary sort key when
    # the requested direction (departing/arriving) couldn't be
    # confirmed via adsbdb — adsbdb has spotty coverage for shorter-
    # haul callsigns, so falling back to "latest last_seen" was
    # picking up high-altitude transit traffic instead of the real
    # takeoff. A real departure has:
    #   - low altitude (just lifted off)
    #   - positive vertical rate (still climbing)
    #   - close to the departing airport's lat/lon
    #   - heading away from the airport (radial vs tangential)
    # A real arrival is the same picture mirrored. Scores are
    # additive in [-10, +10]ish; we don't normalise — they just feed
    # the sort.
    if departing_a is not None:
        for f in candidates:
            f["_departure_score"] = _score_departure_likelihood(f, departing_a, radius)
    if arriving_a is not None:
        for f in candidates:
            f["_arrival_score"] = _score_arrival_likelihood(f, arriving_a, radius)

    # Route confirmation has TWO independent upstream sources, fanned
    # out in parallel:
    #
    #   1. OpenSky /flights/aircraft (icao24-keyed, authoritative). For
    #      a currently-airborne aircraft, returns a record describing
    #      the leg it's flying right now (or the most recent completed
    #      leg if the new one hasn't materialised yet). This is the
    #      same data the side drawer uses to caption "From: KBWI".
    #      Strongest evidence for *origin*.
    #
    #   2. adsbdb /v0/callsign (callsign-keyed, scheduled route). Tells
    #      us "UAL108 typically goes IAD->LAX". Less authoritative
    #      than OpenSky for origin (callsigns get reused for
    #      repositioning legs, charters, etc., and adsbdb has spotty
    #      coverage), but it's the only signal we have for *intended
    #      destination* of an in-progress flight, since OpenSky
    #      doesn't know the destination until landing.
    #
    # Layered classification (see _classify_route_match) prefers the
    # OpenSky signal and falls back to adsbdb, then geometric scoring.
    # The big win vs the previous adsbdb-only design: when adsbdb says
    # "not found" for a callsign and the geometric heuristic likes a
    # nearby plane, we no longer return a flight that OpenSky knows
    # took off from a different airport (the BWI-vs-IAD bug).
    do_route = (
        confirm_route if confirm_route is not None
        else (departing_a is not None or arriving_a is not None)
    )
    # Initialise these here so the geometric prefilter below can read
    # them even when the route-confirmation block didn't run (empty
    # candidate list, or do_route disabled).
    confirmed:    list[dict[str, Any]] = []
    weak:         list[dict[str, Any]] = []
    wrong_origin: list[dict[str, Any]] = []
    if do_route and candidates:
        prelim_sort = sorted(
            candidates,
            key=lambda f: (f.get("last_seen") or 0),
            reverse=True,
        )[: _FIND_MAX_ROUTE_CONFIRM]
        prelim_callsigns = [
            (f.get("callsign") or "").strip().upper() for f in prelim_sort
        ]
        # OpenSky lookup is heavier (host proxy + per-aircraft credits),
        # so cap it tighter and only run it when a departing/arriving
        # filter is in play — without one, the OpenSky origin signal
        # has nothing to match against.
        do_opensky = (departing_a is not None or arriving_a is not None)
        prelim_icao24 = [
            (f.get("id") or "").lower()
            for f in prelim_sort[: _FIND_MAX_OPENSKY_ORIGIN]
        ] if do_opensky else []

        async def _route(cs: str) -> tuple[str, dict[str, Any] | None]:
            if not cs:
                return cs, None
            try:
                res = await api_route(cs)
            except HTTPException:
                return cs, None
            return cs, res if res.get("found") else None

        async def _origin(icao: str) -> tuple[str, dict[str, Any] | None]:
            if not icao:
                return icao, None
            return icao, await _opensky_recent_flight(icao)

        route_task  = asyncio.gather(*[_route(cs)  for cs in prelim_callsigns])
        origin_task = asyncio.gather(*[_origin(ic) for ic in prelim_icao24])
        route_results, origin_results = await asyncio.gather(route_task, origin_task)

        route_by_cs: dict[str, dict[str, Any]] = {}
        for r in route_results:
            if isinstance(r, tuple):
                cs, payload = r
                if payload:
                    route_by_cs[cs] = payload
        origin_by_icao: dict[str, dict[str, Any]] = {}
        for o in origin_results:
            if isinstance(o, tuple):
                icao, rec = o
                if rec:
                    origin_by_icao[icao] = rec

        for f in candidates:
            cs = (f.get("callsign") or "").strip().upper()
            ic = (f.get("id") or "").lower()
            if cs in route_by_cs:
                f["route"] = route_by_cs[cs]
            if ic in origin_by_icao:
                f["_opensky_origin"] = origin_by_icao[ic]

        for f in candidates:
            label = _classify_route_match(f, departing_a, arriving_a)
            f["_route_match"] = label
            if label in ("confirmed", "confirmed-opensky"):
                confirmed.append(f)
            elif label == "wrong-airport-opensky":
                wrong_origin.append(f)
            else:
                weak.append(f)

        # When the user asked for a specific departing airport AND
        # OpenSky authoritatively says some candidates took off from
        # somewhere else, drop those candidates entirely — they were
        # geometric look-alikes (typical case: KBWI departures
        # climbing through KIAD's 150 km bubble heading west).
        # Keep them only as a last-resort fallback if there's nothing
        # else to return, in which case the agent disclaim string
        # will let the user know the match is questionable.
        if departing_a is not None and wrong_origin and (confirmed or weak):
            # Drop wrong_origin entirely.
            candidates = confirmed + weak
        elif confirmed:
            candidates = confirmed + weak
        elif wrong_origin and not weak:
            # Last-resort: nothing else, surface the wrong-airport
            # rows so the agent can at least say "best I could find,
            # but origin doesn't match".
            candidates = wrong_origin
        # else: leave candidates as-is, all geometric / not-confirmed.

    # Sort candidates by `order`. Default depends on the query shape.
    order_key = (order or "").strip().lower() or (
        "latest" if (departing_a or arriving_a) else "closest"
    )

    # Geometric prefilter when departure/arrival was requested but
    # adsbdb didn't confirm anything. Without this guard, "latest"
    # picks the most-recently-seen plane in the bubble — which is
    # often a 25,000 ft transit jet, not the actual takeoff. We
    # require a minimum departure/arrival score to count as a real
    # match. The threshold (3.0) was tuned to admit "low + climbing"
    # while rejecting "level cruise overhead".
    DEPARTURE_SCORE_FLOOR = 3.0
    if (
        do_route
        and not confirmed
        and (departing_a is not None or arriving_a is not None)
    ):
        if departing_a is not None:
            strong = [c for c in candidates
                      if (c.get("_departure_score") or 0) >= DEPARTURE_SCORE_FLOOR]
        else:
            strong = [c for c in candidates
                      if (c.get("_arrival_score") or 0) >= DEPARTURE_SCORE_FLOOR]
        if strong:
            # Mark the surviving rows so the agent can see WHY they
            # were chosen even though adsbdb didn't confirm.
            for c in strong:
                c["_route_match"] = "geometric-departure" if departing_a else "geometric-arrival"
            candidates = strong

    # Primary sort key prefers route-confirmed matches, then geometric
    # likelihood (when departing/arriving), then the user-requested
    # order_key as the secondary key. This way `order=latest` still
    # means "latest among the plausible matches", not "latest in the
    # whole bubble regardless of plausibility".
    def _confidence_rank(f: dict[str, Any]) -> int:
        m = f.get("_route_match")
        if m == "confirmed-opensky":     return 0  # ADS-B authoritative
        if m == "confirmed":             return 1  # adsbdb scheduled-route
        if m == "geometric-departure":   return 2
        if m == "geometric-arrival":     return 2
        if m == "not-confirmed":         return 3
        if m == "wrong-route":           return 4
        if m == "wrong-airport-opensky": return 5  # filtered above; defensive
        return 6

    def _direction_score(f: dict[str, Any]) -> float:
        # Larger = more departure-y / arrival-y. Negate when we sort
        # ascending so high scores come first.
        if departing_a is not None:
            return f.get("_departure_score") or 0.0
        if arriving_a is not None:
            return f.get("_arrival_score") or 0.0
        return 0.0

    if order_key == "latest":
        candidates.sort(key=lambda f: (
            _confidence_rank(f),
            -(_direction_score(f)),
            -(f.get("last_seen") or 0),
        ))
    elif order_key in ("closest", "closest_to_origin", "nearest"):
        candidates.sort(key=lambda f: (
            _confidence_rank(f),
            (f.get("_distance_km") if f.get("_distance_km") is not None else 1e9),
        ))
    elif order_key in ("lowest_alt", "low_alt", "lowest"):
        candidates.sort(key=lambda f: (
            _confidence_rank(f),
            (f.get("alt_m") if f.get("alt_m") is not None else 1e9),
        ))
    elif order_key in ("fastest_climb", "climb"):
        candidates.sort(key=lambda f: (
            _confidence_rank(f),
            -(f.get("vrate_mps") or 0),
        ))
    elif order_key in ("aligned", "best_aligned"):
        candidates.sort(key=lambda f: (
            _confidence_rank(f),
            f.get("_heading_misalign_deg", 999),
        ))
    else:
        return {
            "ok": False,
            "error": f"unknown order {order!r}",
            "valid_orders": ["latest", "closest", "lowest_alt", "fastest_climb", "aligned"],
        }

    lim = int(limit) if limit is not None else _FIND_DEFAULT_LIMIT
    candidates = candidates[: max(1, lim)]

    return {
        "ok": True,
        "count": len(candidates),
        "filters": {
            "departing": (departing_a or {}).get("code"),
            "arriving":  (arriving_a or {}).get("code"),
            "near":      (near_a or {}).get("code"),
            "radius_km": radius,
            "phase":     phase_key,
            "heading_deg":     target_bearing,
            "heading_tol_deg": tol if target_bearing is not None else None,
            "min_alt_m": min_alt_m,
            "max_alt_m": max_alt_m,
            "since_seconds": since_seconds,
            "order":     order_key,
            "limit":     lim,
            "confirm_route": bool(do_route),
        },
        "flights": [
            {
                "id":         (f.get("id") or "").lower(),
                "icao24":     (f.get("id") or "").lower(),
                "callsign":   (f.get("callsign") or "").strip(),
                "lat":        f.get("lat"),
                "lon":        f.get("lon"),
                "alt_m":      f.get("alt_m"),
                "vrate_mps":  f.get("vrate_mps"),
                "heading":    f.get("heading"),
                "ground_speed_mps": f.get("vel_mps"),
                "on_ground":  f.get("on_ground"),
                "country":    f.get("country"),
                "squawk":     f.get("squawk"),
                "last_seen":  f.get("last_seen"),
                "distance_from_center_km": f.get("_distance_km"),
                "heading_misalign_deg":    f.get("_heading_misalign_deg"),
                "route_match":             f.get("_route_match"),
                # Geometric likelihood scores. ~10 = textbook
                # departure/arrival; ~0 = inconclusive; <0 = looks
                # like the opposite (departure score for an arriving
                # plane). Surfaced so the agent can disclaim
                # appropriately when no route confirmation came back.
                "departure_score":         f.get("_departure_score"),
                "arrival_score":           f.get("_arrival_score"),
                "route":                   f.get("route"),
                # OpenSky's per-airframe history record (the same
                # source the side drawer uses to caption "From: KBWI").
                # Surfaced so the agent can ground its narration in
                # authoritative ADS-B-derived origin data instead of
                # making claims from geometric scores alone.
                "opensky_origin": (
                    {
                        "estDepartureAirport": (f.get("_opensky_origin") or {}).get("estDepartureAirport"),
                        "estArrivalAirport":   (f.get("_opensky_origin") or {}).get("estArrivalAirport"),
                        "first_seen":          (f.get("_opensky_origin") or {}).get("firstSeen"),
                        "last_seen":           (f.get("_opensky_origin") or {}).get("lastSeen"),
                    } if f.get("_opensky_origin") else None
                ),
            }
            for f in candidates
        ],
    }


# Aircraft colour modes the frontend understands. Kept in lock-step with
# COLOR_SCHEMES in app.js — adding a new mode means adding it on both
# sides. Aliases let the chat agent accept natural phrasings without
# making the user remember the exact key.
COLOR_MODES = {"phase", "altitude", "vrate", "squawk"}
COLOR_MODE_ALIASES = {
    "phase of flight": "phase",
    "flight phase": "phase",
    "default": "phase",
    "elevation": "altitude",
    "alt": "altitude",
    "fl": "altitude",
    "flight level": "altitude",
    "vertical rate": "vrate",
    "climb": "vrate",
    "descent": "vrate",
    "climb/descent": "vrate",
    "rate of climb": "vrate",
    "v/s": "vrate",
    "vs": "vrate",
    "emergency": "squawk",
    "emergencies": "squawk",
    "alerts": "squawk",
    "transponder": "squawk",
}


def _resolve_color_mode(value: str) -> str | None:
    if not value:
        return None
    key = value.strip().lower()
    if key in COLOR_MODES:
        return key
    return COLOR_MODE_ALIASES.get(key)


async def tool_set_color_mode(mode: str) -> dict[str, Any]:
    resolved = _resolve_color_mode(mode)
    if resolved is None:
        return {
            "ok": False,
            "error": f"unknown colour mode {mode!r}",
            "valid": sorted(COLOR_MODES),
        }
    payload = {"type": "color", "mode": resolved}
    delivered = await _bus.broadcast(payload)
    return {"ok": True, "delivered": delivered, **payload}


# ── METAR colour mode (weather station body) ────────────────────────────
# Mirror of COLOR_MODES but for the METAR overlay's circle body. The
# wind-vane arrow follows the same scheme so circle and arrow always
# agree. Aliases let the agent pass through the user's wording verbatim.
METAR_COLOR_MODES = {"flt_cat", "wind", "temp", "visibility"}
METAR_COLOR_MODE_ALIASES = {
    "flight category": "flt_cat",
    "category": "flt_cat",
    "vfr": "flt_cat",
    "ifr": "flt_cat",
    "wind speed": "wind",
    "winds": "wind",
    "temperature": "temp",
    "temp": "temp",
    "visibility": "visibility",
    "vis": "visibility",
}


def _resolve_metar_color_mode(value: str) -> str | None:
    if not value:
        return None
    key = value.strip().lower()
    if key in METAR_COLOR_MODES:
        return key
    return METAR_COLOR_MODE_ALIASES.get(key)


async def tool_set_metar_color_mode(mode: str) -> dict[str, Any]:
    resolved = _resolve_metar_color_mode(mode)
    if resolved is None:
        return {
            "ok": False,
            "error": f"unknown METAR colour mode {mode!r}",
            "valid": sorted(METAR_COLOR_MODES),
        }
    payload = {"type": "metar-color", "mode": resolved}
    delivered = await _bus.broadcast(payload)
    return {"ok": True, "delivered": delivered, **payload}


# ── Phase / squawk bucket filters (chip legend) ─────────────────────────
# The browser's IconLayer hides flights whose bucket is "off". Buckets
# are per-color-mode and orthogonal; this tool is the one chat hook to
# drive both. The user's natural-language ask ("only show emergency
# squawks", "hide everyone on the ground", "just landings") maps onto
# one of:
#   - buckets:  full replacement set (most explicit)
#   - include:  add these buckets to the current armed set
#   - exclude:  remove these buckets from the current armed set
#   - reset:    re-arm every bucket (== no filtering)
PHASE_BUCKETS  = {"climb", "level-slow", "level-fast", "descend", "ground"}
SQUAWK_BUCKETS = {"7500", "7600", "7700", "normal", "ground"}

# A few semantic shortcuts so the agent doesn't have to enumerate
# explicit bucket lists for the common asks. Each shortcut is keyed
# by mode → phrase and resolves to a `buckets` set.
FILTER_SHORTCUTS: dict[str, dict[str, set[str]]] = {
    "phase": {
        "airborne":         {"climb", "level-slow", "level-fast", "descend"},
        "in-flight":        {"climb", "level-slow", "level-fast", "descend"},
        "level":            {"level-slow", "level-fast"},
        "cruise":           {"level-slow", "level-fast"},
        "climbing":         {"climb"},
        "takeoff":          {"climb"},
        "departing":        {"climb"},
        "descending":       {"descend"},
        "landing":          {"descend"},
        "arriving":         {"descend"},
        "ground":           {"ground"},
        "parked":           {"ground"},
        "moving":           {"climb", "level-slow", "level-fast", "descend"},
    },
    "squawk": {
        "emergency":        {"7500", "7600", "7700"},
        "emergencies":      {"7500", "7600", "7700"},
        "non-normal":       {"7500", "7600", "7700"},
        "abnormal":         {"7500", "7600", "7700"},
        "alerts":           {"7500", "7600", "7700"},
        "hijack":           {"7500"},
        "comms-failure":    {"7600"},
        "general":          {"7700"},
        "normal":           {"normal"},
        "airborne":         {"7500", "7600", "7700", "normal"},
    },
}


def _filter_mode_buckets(mode: str) -> set[str]:
    if mode == "phase":  return set(PHASE_BUCKETS)
    if mode == "squawk": return set(SQUAWK_BUCKETS)
    return set()


async def tool_set_filter(
    mode: str,
    *,
    buckets: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    only: str | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Drive the chip-legend bucket filter on the browser.

    The browser owns the *current* filter state (it persists in
    localStorage). The server-side tool just broadcasts the desired
    delta as one of four shapes, and the browser resolves it against
    its current armed set. This keeps a stale chat command from
    fighting with what the user did manually after the fact.
    """
    mode_l = (mode or "").strip().lower()
    if mode_l not in {"phase", "squawk"}:
        return {
            "ok": False,
            "error": f"filter mode must be 'phase' or 'squawk' (got {mode!r})",
            "valid_modes": ["phase", "squawk"],
        }

    valid = _filter_mode_buckets(mode_l)
    msg: dict[str, Any] = {"type": "filter", "mode": mode_l}

    # Shortcut phrases ("only emergencies", "only landings") win first —
    # they're the highest-leverage path for natural language.
    if only is not None:
        key = (only or "").strip().lower()
        bucket_set = FILTER_SHORTCUTS.get(mode_l, {}).get(key)
        if bucket_set is None:
            return {
                "ok": False,
                "error": f"unknown shortcut {only!r} for mode {mode_l!r}",
                "valid_shortcuts": sorted(FILTER_SHORTCUTS.get(mode_l, {}).keys()),
            }
        msg["buckets"] = sorted(bucket_set)
    elif reset:
        msg["reset"] = True
    elif buckets is not None:
        bad = [b for b in buckets if b not in valid]
        if bad:
            return {
                "ok": False,
                "error": f"unknown buckets {bad!r} for mode {mode_l!r}",
                "valid_buckets": sorted(valid),
            }
        msg["buckets"] = list(buckets)
    else:
        # include/exclude deltas (one or both is fine)
        if include is not None:
            bad = [b for b in include if b not in valid]
            if bad:
                return {"ok": False, "error": f"unknown buckets {bad!r}", "valid_buckets": sorted(valid)}
            msg["include"] = list(include)
        if exclude is not None:
            bad = [b for b in exclude if b not in valid]
            if bad:
                return {"ok": False, "error": f"unknown buckets {bad!r}", "valid_buckets": sorted(valid)}
            msg["exclude"] = list(exclude)
        if "include" not in msg and "exclude" not in msg:
            return {
                "ok": False,
                "error": "specify one of: buckets, include, exclude, only, reset",
                "valid_buckets": sorted(valid),
            }

    delivered = await _bus.broadcast(msg)
    return {"ok": True, "delivered": delivered, **msg}


# ── Free-form camera control ────────────────────────────────────────────
# Useful when the user wants to angle the map without re-targeting an
# airport ("tilt the map", "go straight down", "spin north"). Any field
# left None passes through and the browser keeps its current value.
async def tool_set_view(
    *,
    lat: float | None = None,
    lon: float | None = None,
    zoom: float | None = None,
    pitch: float | None = None,
    bearing: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "view"}
    if lat     is not None: payload["lat"]     = float(lat)
    if lon     is not None: payload["lon"]     = float(lon)
    if zoom    is not None: payload["zoom"]    = float(zoom)
    if pitch   is not None: payload["pitch"]   = float(pitch)
    if bearing is not None: payload["bearing"] = float(bearing)
    if len(payload) == 1:
        return {"ok": False, "error": "provide at least one of lat,lon,zoom,pitch,bearing"}
    delivered = await _bus.broadcast(payload)
    return {"ok": True, "delivered": delivered, **payload}


# ── 3D airspace toggle ──────────────────────────────────────────────────
async def tool_set_airspace3d(enabled: bool) -> dict[str, Any]:
    payload = {"type": "airspace3d", "enabled": bool(enabled)}
    delivered = await _bus.broadcast(payload)
    return {"ok": True, "delivered": delivered, **payload}


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


# ── OpenClaw bridge ─────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """Frontend payload. We only need the latest user message and the
    OpenClaw session id from the previous turn (if any) — the agent itself
    keeps the full conversation history."""

    message: str
    session_id: str | None = None
    thinking: str = "off"  # off | minimal | low | medium | high


def _extract_reply(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Pull the agent's reply text + its session id out of the JSON shape
    `openclaw agent --json` produces.

    The CLI has emitted at least two shapes over its lifetime:

      Old (pre-2026): `{ "result": { "payloads": [...], "meta": {...} } }`
      New (current):  `{ "payloads": [...], "meta": {...} }` — flat

    We accept either by probing for `payloads` at top level first and
    falling back to the nested `result` envelope. Without this dual
    handling, a CLI bump that drops the `result` wrapper makes every
    chat reply collapse to "[no reply]" even though the agent actually
    produced a perfectly good answer.
    """
    container: dict[str, Any]
    if isinstance(payload.get("payloads"), list):
        container = payload
    else:
        container = payload.get("result") or {}

    payloads = container.get("payloads") or []
    parts: list[str] = []
    for p in payloads:
        text = (p or {}).get("text")
        if text:
            parts.append(text)
    reply = (
        "\n".join(parts).strip()
        or (payload.get("summary") or "").strip()
        or "[no reply]"
    )
    sid = (((container.get("meta") or {}).get("agentMeta") or {}).get("sessionId")) or None
    return reply, sid


# Cap on how much of a tool-result body we surface to the chat UI.
# Some tools (e.g. /api/flights without a bbox, /api/airports CONUS)
# return >500 KB of JSON which would balloon the chat payload and
# break the inline rendering. The agent itself sees the full output
# in its session — the trace is just a UI affordance.
_TRACE_TEXT_MAX = 1500


def _iso_to_ms(ts: Any) -> int | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _truncate(text: str, limit: int = _TRACE_TEXT_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def _normalize_jsonl_record(record: dict[str, Any], started_at_ms: int) -> list[dict[str, Any]]:
    """Reduce one parsed line of the agent's session JSONL into zero or
    more compact UI events ready to be sent to the browser. Returns
    `[]` for echoed user prompts, lines older than `started_at_ms`,
    and unrecognised shapes — so callers can feed every line through
    this helper and get only the interesting bits back.

    Used by both the post-turn batch reader (`_read_turn_events`) and
    the live NDJSON stream so the wire shape is identical regardless
    of whether the client is on the streaming or batch path.

    Special case — *planning text*. The Nemotron-3 model never emits
    explicit "thought" records, but when it's about to invoke a tool
    it routinely co-emits a short narration in the SAME assistant
    message, e.g.:

        role: assistant
        content: [
            { type: "text",     text: "I'll start by looking up X, then Y…" },
            { type: "toolCall", arguments: { command: "curl …" } },
        ]

    That text is the agent's plan (decision-making BEFORE the call),
    not a final reply. We surface it as `kind: "planning"` so the UI
    can render it just before the matching tool_call card. An
    assistant message that contains ONLY a text block is the FINAL
    reply — that one we drop here because it's already delivered to
    the client via the streaming endpoint's `done` event.
    """
    msg = record.get("message") or {}
    role = msg.get("role")
    ts_ms = msg.get("timestamp") or _iso_to_ms(record.get("timestamp"))
    if not isinstance(ts_ms, (int, float)) or ts_ms < started_at_ms:
        return []
    # Skip echoed user prompts — the chat UI already shows that bubble.
    if role == "user":
        return []
    out: list[dict[str, Any]] = []
    contents = msg.get("content") or []
    has_tool_call = any(
        isinstance(c, dict) and c.get("type") == "toolCall" for c in contents
    )
    for content in contents:
        if not isinstance(content, dict):
            continue
        t = content.get("type")
        if t == "text" and role == "assistant":
            text = (content.get("text") or "").strip()
            if not text:
                continue
            if msg.get("thoughtType"):
                kind = "thought"
            elif has_tool_call:
                kind = "planning"
            else:
                # Final-reply text — already delivered via the
                # subprocess's --json output. Don't double-render.
                continue
            out.append({
                "kind":  kind,
                "ts_ms": int(ts_ms),
                "text":  _truncate(text, 4000),
            })
        elif t == "toolCall":
            args = content.get("arguments") or {}
            # The exec tool wraps a shell command in arguments.command;
            # surface that verbatim since it's the most useful thing
            # for a human reading the trace to see.
            cmd = args.get("command") if isinstance(args, dict) else None
            out.append({
                "kind":    "tool_call",
                "ts_ms":   int(ts_ms),
                "tool":    content.get("name") or "tool",
                "call_id": content.get("id"),
                "command": _truncate(cmd, 2000) if isinstance(cmd, str) else None,
                "args":    args if not isinstance(cmd, str) else None,
            })
        elif t == "toolResult" or role == "toolResult":
            text = (content.get("text") or "").strip()
            out.append({
                "kind":     "tool_result",
                "ts_ms":    int(ts_ms),
                "call_id":  content.get("toolCallId") or content.get("id"),
                "is_error": bool(content.get("isError")),
                "text":     _truncate(text),
            })
    return out


def _resolve_session_path(session_id: str | None, started_at_ms: int) -> Path | None:
    """Return the JSONL file the agent is writing to for THIS turn,
    or `None` if it hasn't appeared yet.

    With a known `session_id` (i.e. follow-up turn), we point straight
    at `<sessions_dir>/<session_id>.jsonl`. For the first turn of a
    session the id is unknown ahead of time, so we scan the sessions
    directory for any `*.jsonl` whose mtime is >= `started_at_ms`,
    minus a small grace, and pick the most recently modified — that's
    the file `openclaw agent` just created.
    """
    sessions_dir = Path(OPENCLAW_SESSIONS_DIR)
    if session_id:
        p = sessions_dir / f"{session_id}.jsonl"
        return p if p.exists() else None
    if not sessions_dir.exists():
        return None
    cutoff_s = (started_at_ms - 1500) / 1000.0  # 1.5 s grace
    best: tuple[float, Path] | None = None
    try:
        for p in sessions_dir.glob("*.jsonl"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt < cutoff_s:
                continue
            if best is None or mt > best[0]:
                best = (mt, p)
    except OSError:
        return None
    return best[1] if best else None


def _read_new_events(
    path: Path,
    started_at_ms: int,
    offsets: dict[str, int],
) -> list[dict[str, Any]]:
    """Read whatever new bytes have appeared in `path` since the last
    call, parse them as line-delimited JSON, and return normalised UI
    events. `offsets` is updated in-place so callers can poll this
    function in a tight loop without re-emitting old lines.

    Partial trailing lines (the agent flushed half a record) are left
    in the file for next pass — we only advance the offset past the
    last newline we saw.
    """
    out: list[dict[str, Any]] = []
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return out
    last = offsets.get(key, 0)
    if size <= last:
        return out
    try:
        with path.open("rb") as fh:
            fh.seek(last)
            chunk = fh.read(size - last)
    except OSError:
        return out
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        # No complete line yet — wait for next poll.
        return out
    consumed = last_nl + 1
    offsets[key] = last + consumed
    text = chunk[:consumed].decode("utf-8", errors="replace")
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        out.extend(_normalize_jsonl_record(rec, started_at_ms))
    return out


def _read_turn_events(session_id: str | None, started_at_ms: int) -> list[dict[str, Any]]:
    """Read EVERY event for this turn at once. Used by the non-streaming
    `/api/chat` endpoint after the subprocess returns."""
    if not session_id:
        return []
    path = Path(OPENCLAW_SESSIONS_DIR) / f"{session_id}.jsonl"
    if not path.exists():
        return []
    offsets: dict[str, int] = {}
    out = _read_new_events(path, started_at_ms, offsets)
    out.sort(key=lambda r: r.get("ts_ms") or 0)
    return out


async def call_openclaw_agent(
    message: str,
    session_id: str | None = None,
    thinking: str = "off",
) -> dict[str, Any]:
    """Run one agent turn through OpenClaw. The agent has the flight-tracking
    skill installed (deployed by install.sh) and uses inference via the
    gateway-managed route, so we don't need any inference credentials of our
    own."""
    if not Path(OPENCLAW_BIN).exists():
        raise HTTPException(
            status_code=503,
            detail=f"openclaw binary not found at {OPENCLAW_BIN}",
        )

    cmd = [
        OPENCLAW_BIN, "agent",
        "--agent", OPENCLAW_AGENT,
        "--message", message,
        "--json",
        "--thinking", thinking,
        "--timeout", str(OPENCLAW_TIMEOUT_S),
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])

    # Record turn-start before spawning so the JSONL tail can isolate
    # exactly the events that belong to THIS turn (the session file
    # accumulates every previous turn for the same session).
    started_at_ms = int(time.time() * 1000)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=OPENCLAW_TIMEOUT_S + 30
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="openclaw agent timed out") from None

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace")[-1500:]
        raise HTTPException(
            status_code=502,
            detail=f"openclaw agent exited {proc.returncode}: {err.strip()}",
        )

    raw = (stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        raise HTTPException(status_code=502, detail="openclaw agent produced no output")

    # The CLI prints a few diagnostic lines to stdout before the JSON
    # document on some versions; isolate the JSON by finding the first '{'.
    first_brace = raw.find("{")
    if first_brace < 0:
        raise HTTPException(status_code=502, detail=f"non-JSON agent output: {raw[:240]}")
    try:
        payload = json.loads(raw[first_brace:])
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"could not parse agent JSON: {exc}",
        ) from exc

    reply, sid = _extract_reply(payload)
    events = _read_turn_events(sid, started_at_ms)
    return {
        "reply":      reply,
        "session_id": sid,
        "status":     payload.get("status"),
        "summary":    payload.get("summary"),
        # Per-turn trace: tool calls, tool results, thought blocks. UI
        # only renders these if the user has the corresponding toggle on.
        "events":     events,
    }


# ── App + lifespan ──────────────────────────────────────────────────────────


async def _fetch_one(url: str, params: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    r = await _http.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "features" not in data:
        data = {"type": "FeatureCollection", "features": data.get("features", [])}
    data.pop("crs", None)
    return data


async def fetch_airspace(name: str) -> dict[str, Any]:
    """Return cached GeoJSON for a FAA dataset, refreshing past TTL.

    For datasets with a `fanout` list, we issue one upstream request per
    fanout entry in parallel and merge the FeatureCollections. This is the
    workaround for FAA layers where `IN`/`OR` queries time out but
    single-value `=` queries are quick.
    """
    spec = FAA_DATASETS.get(name)
    if spec is None:
        raise KeyError(name)
    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    async with _airspace_locks[name]:
        cached = _airspace_cache.get(name)
        if cached and (time.time() - cached[0]) < spec["ttl_s"]:
            return cached[1]
        try:
            base = dict(spec.get("params", {}))
            fanout = spec.get("fanout")
            if fanout:
                # Tolerate per-shard failures — the FAA endpoint is flaky on
                # certain class queries and we'd rather show a partial map
                # than nothing. Each shard gets a generous timeout because
                # individual queries have been observed to take 30-45s.
                results = await asyncio.gather(
                    *[_fetch_one(spec["url"], {**base, **shard}) for shard in fanout],
                    return_exceptions=True,
                )
                merged_features: list[dict[str, Any]] = []
                ok_count = 0
                for s in results:
                    if isinstance(s, Exception):
                        continue
                    merged_features.extend(s.get("features") or [])
                    ok_count += 1
                if ok_count == 0:
                    raise results[0] if isinstance(results[0], Exception) else \
                        RuntimeError("all fanout shards failed")
                data = {"type": "FeatureCollection", "features": merged_features}
            else:
                data = await _fetch_one(spec["url"], base)
        except httpx.HTTPError as exc:
            if cached:
                # Stale-but-correct beats an outage on the chart.
                return cached[1]
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
        _airspace_cache[name] = (time.time(), data)
        return data


async def fetch_airspace_bbox(name: str, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """Query a *bbox-only* FAA layer (taxiways/obstacles/ats).

    bbox is (south, north, west, east) consistent with fetch_flights().
    Results are cached per (name, rounded-bbox) for BBOX_CACHE_TTL seconds.
    """
    spec = FAA_BBOX_DATASETS.get(name)
    if spec is None:
        raise KeyError(name)
    if _http is None:
        raise RuntimeError("HTTP client not initialised")

    s, n, w, e = bbox
    # Round to ~0.05° (~5 km) so panning by a city block doesn't bust the
    # cache. ArcGIS takes the bbox as minLon,minLat,maxLon,maxLat.
    rs, rn = round(s, 2), round(n, 2)
    rw, re_ = round(w, 2), round(e, 2)
    cache_key = f"{name}:{rw},{rs},{re_},{rn}"

    async with _bbox_cache_lock:
        cached = _bbox_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < BBOX_CACHE_TTL:
            return cached[1]

    params = {
        "where": spec.get("where_extra", "1=1"),
        "geometry": f"{rw},{rs},{re_},{rn}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": spec["outFields"],
        "f": "geojson",
        "resultRecordCount": spec["max_records"],
    }
    try:
        data = await _fetch_one(spec["url"], params, timeout=30.0)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    async with _bbox_cache_lock:
        _bbox_cache[cache_key] = (time.time(), data)
        # Bound memory: prune oldest if we're past the cap.
        if len(_bbox_cache) > BBOX_CACHE_MAX:
            for k, _ in sorted(_bbox_cache.items(), key=lambda kv: kv[1][0])[
                : len(_bbox_cache) - BBOX_CACHE_MAX
            ]:
                _bbox_cache.pop(k, None)
    return data


def _polygon_bbox(coords: list[Any]) -> tuple[float, float, float, float] | None:
    """Return (minLon, minLat, maxLon, maxLat) for any GeoJSON polygon ring set."""
    if not coords:
        return None
    minLon = minLat = float("inf")
    maxLon = maxLat = float("-inf")

    def walk(arr: list[Any]) -> None:
        nonlocal minLon, minLat, maxLon, maxLat
        for item in arr:
            if isinstance(item, list) and item and isinstance(item[0], (int, float)):
                lon, lat = item[0], item[1]
                if lon < minLon: minLon = lon
                if lon > maxLon: maxLon = lon
                if lat < minLat: minLat = lat
                if lat > maxLat: maxLat = lat
            elif isinstance(item, list):
                walk(item)

    walk(coords)
    if minLon == float("inf"):
        return None
    return (minLon, minLat, maxLon, maxLat)


def _ring_contains(ring: list[list[float]], lat: float, lon: float) -> bool:
    """Standard ray-casting point-in-polygon test (longitude=x, latitude=y)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _feature_contains(feature: dict[str, Any], lat: float, lon: float) -> bool:
    geom = feature.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        rings = [coords]
    elif gtype == "MultiPolygon":
        rings = coords
    else:
        return False
    for poly in rings:
        if not poly:
            continue
        if _ring_contains(poly[0], lat, lon):
            # A point inside any hole disqualifies it.
            for hole in poly[1:]:
                if _ring_contains(hole, lat, lon):
                    return False
            return True
    return False


def _feature_within_radius(feature: dict[str, Any], lat: float, lon: float,
                            radius_km: float) -> bool:
    """Cheap: does the feature's bbox come within radius_km of the point?"""
    bb = _polygon_bbox((feature.get("geometry") or {}).get("coordinates") or [])
    if not bb:
        return False
    minLon, minLat, maxLon, maxLat = bb
    # Clamp the point to the bbox and measure distance to that corner.
    clampedLon = max(minLon, min(maxLon, lon))
    clampedLat = max(minLat, min(maxLat, lat))
    return haversine_km(lat, lon, clampedLat, clampedLon) <= radius_km


# Coded-value domains lifted from the FAA AM_Runway / AM_Taxiway feature
# service descriptors. Decoding them here means the chat agent and the
# detail drawer both get human-readable strings instead of raw codes.
RWY_OPER_NAMES = {
    "1": "closed indefinitely",
    "2": "open",
    "3": "under construction",
    "4": "repurposed as taxiway",
    "5": "unknown",
    "7": "closed",
}
TWY_OPER_NAMES = {
    "2": "open",
    "5": "unknown",
    "7": "closed",
}
SURFACE_NAMES = {
    "1": "hard/paved",
    "2": "metal",
    "5": "other than hard surface",
}


def _summarize(feature: dict[str, Any], dataset: str) -> dict[str, Any]:
    """Flatten feature properties into a chat-friendly card."""
    p = feature.get("properties") or {}
    out: dict[str, Any] = {"dataset": dataset}
    if dataset in ("sua", "classes"):
        out["name"] = p.get("NAME") or p.get("LOCAL_TYPE") or p.get("TYPE_CODE")
        out["type"] = p.get("TYPE_CODE")
        out["class"] = p.get("CLASS")
        upper = p.get("UPPER_VAL")
        upper_uom = p.get("UPPER_UOM") or "FT"
        upper_code = p.get("UPPER_CODE") or ""
        lower = p.get("LOWER_VAL")
        lower_uom = p.get("LOWER_UOM") or "FT"
        lower_code = p.get("LOWER_CODE") or ""
        if upper is not None:
            out["upper"] = f"{upper} {upper_uom} {upper_code}".strip()
        if lower is not None:
            out["lower"] = f"{lower} {lower_uom} {lower_code}".strip()
        if p.get("CITY"):
            out["location"] = ", ".join(x for x in [p.get("CITY"), p.get("STATE")] if x)
        if p.get("TIMESOFUSE"):
            out["times_of_use"] = p.get("TIMESOFUSE")
    elif dataset == "tfrs":
        out["name"] = p.get("TITLE") or p.get("NOTAM_KEY")
        out["state"] = p.get("STATE")
        out["notam"] = p.get("NOTAM_KEY")
        if p.get("LAST_MODIFICATION_DATETIME"):
            out["updated"] = p.get("LAST_MODIFICATION_DATETIME")
    elif dataset == "runways":
        out["airport"] = p.get("ICAO_ID") or p.get("FAA_ID")
        out["runway"] = p.get("DESIGNATOR") or p.get("RWY_ID")
        out["surface"] = SURFACE_NAMES.get(str(p.get("SURFACE")), p.get("SURFACE"))
        out["status"] = RWY_OPER_NAMES.get(str(p.get("RWY_OPER")), p.get("RWY_OPER"))
    elif dataset == "taxiways":
        out["airport"] = p.get("ICAO_ID") or p.get("FAA_ID")
        out["taxiway"] = p.get("DESIGNATOR")
        out["surface"] = SURFACE_NAMES.get(str(p.get("SURFACE")), p.get("SURFACE"))
        out["status"] = TWY_OPER_NAMES.get(str(p.get("TWY_OPER")), p.get("TWY_OPER"))
    elif dataset == "obstacles":
        out["type"] = p.get("Type_Code")
        out["agl_ft"] = p.get("AGL")
        out["msl_ft"] = p.get("AMSL")
        out["lighting"] = p.get("Lighting")
        out["location"] = ", ".join(x for x in [p.get("City"), p.get("State")] if x)
        out["verified"] = p.get("Verified")
    elif dataset == "ats":
        out["ident"] = p.get("IDENT")
        out["type"] = p.get("TYPE_CODE")
        out["level"] = p.get("LEVEL_")
        if p.get("MAA_VAL"):
            out["max_authorized_alt"] = f"{p.get('MAA_VAL')} {p.get('MAA_UOM') or 'FT'}"
        if p.get("WKHR_CODE"):
            out["hours"] = p.get("WKHR_CODE")
    elif dataset == "artcc":
        out["ident"] = p.get("IDENT")            # e.g. "ZID"
        out["name"] = p.get("NAME")              # e.g. "INDIANAPOLIS"
        out["type"] = p.get("LOCAL_TYPE")        # ARTCC_L / ARTCC_H
    elif dataset == "navaids":
        out["ident"] = p.get("IDENT")
        out["name"] = p.get("NAME_TXT")
        out["class"] = p.get("CLASS_TXT")        # H-VORTAC, L-VOR/DME, etc.
        out["channel"] = p.get("CHANNEL")
        out["status"] = p.get("STATUS")
        out["location"] = ", ".join(x for x in [p.get("CITY"), p.get("STATE")] if x)
    return out


def _feature_centroid(feature: dict[str, Any]) -> tuple[float, float] | None:
    """Cheap mean-of-coords centroid; good enough for distance triage."""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []

    pts: list[tuple[float, float]] = []

    def walk(arr: Any) -> None:
        if isinstance(arr, list) and arr and isinstance(arr[0], (int, float)):
            if len(arr) >= 2:
                pts.append((arr[0], arr[1]))
        elif isinstance(arr, list):
            for item in arr:
                walk(item)

    walk(coords)
    if not pts:
        return None
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return (lon, lat)


async def _prewarm_airspace() -> None:
    """Populate the FAA cache at boot so the first browser toggle is fast.

    The Class_Airspace upstream is genuinely slow (60–120s cold) and the
    `openshell forward` between host and sandbox times out at 30s, so we
    do this work once at startup. Failures are logged and swallowed —
    the per-endpoint handlers will retry on demand if the cache is empty.
    """
    for name in FAA_DATASETS:
        try:
            await fetch_airspace(name)
        except Exception as exc:
            print(f"[prewarm] {name}: {exc!r}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _http
    _http = httpx.AsyncClient(http2=False)
    # Kick off prewarm in the background so app startup isn't blocked.
    prewarm_task = asyncio.create_task(_prewarm_airspace())
    try:
        yield
    finally:
        prewarm_task.cancel()
        try:
            await prewarm_task
        except (asyncio.CancelledError, Exception):
            pass
        await _http.aclose()
        _http = None


app = FastAPI(
    title="Flight Tracking Integration",
    description="Live aircraft tracking with deck.gl + OpenClaw skill bridge.",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    if OPENSKY_PROXY_URL:
        # Tier-1: keys live exclusively on the host inside the proxy.
        # The sandbox process has no credential material to report.
        opensky_auth = "host-proxy"
    elif _opensky_tokens.configured:
        opensky_auth = "oauth2"
    elif OPENSKY_USER and OPENSKY_PASS:
        opensky_auth = "basic"
    else:
        opensky_auth = "anonymous"
    return {
        "ok": True,
        "airports_loaded": len(AIRPORTS),
        "opensky_auth": opensky_auth,
        "opensky_authenticated": opensky_auth != "anonymous",
        "opensky_proxy_url": OPENSKY_PROXY_URL or None,
        "faa_proxy_url": FAA_PROXY_URL or None,
        "nas_via": "host-proxy" if FAA_PROXY_URL else "direct",
        "metar_via": "host-proxy" if FAA_PROXY_URL else "direct",
        "openclaw_bin": OPENCLAW_BIN,
        "openclaw_available": Path(OPENCLAW_BIN).exists(),
        "openclaw_agent": OPENCLAW_AGENT,
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


@app.get("/api/flights/find")
async def api_flights_find(
    departing:        str   | None = Query(default=None, description="airport code/name (origin filter)"),
    arriving:         str   | None = Query(default=None, description="airport code/name (destination filter)"),
    near:             str   | None = Query(default=None, description="airport code/name (search center)"),
    radius_km:        float | None = Query(default=None, ge=10, le=2000,
                                            description="search radius around departing/near (default 150)"),
    phase:            str   | None = Query(default=None,
                                            description="climb|cruise|descent|ground|airborne; aliases: departing, arriving, level…"),
    min_alt_m:        float | None = Query(default=None, ge=0,    description="minimum altitude in metres"),
    max_alt_m:        float | None = Query(default=None, ge=0,    description="maximum altitude in metres"),
    heading_deg:      float | None = Query(default=None, ge=0, lt=360,
                                            description="explicit heading filter (otherwise derived from departing→arriving great-circle bearing)"),
    heading_tol_deg:  float | None = Query(default=None, ge=0, le=180,
                                            description="tolerance for heading filter (default ±35°)"),
    since_seconds:    float | None = Query(default=None, ge=0,
                                            description="only flights with last_seen within the last N seconds"),
    confirm_route:    bool  | None = Query(default=None,
                                            description="cross-check matching candidates against /api/route (defaults to true when departing or arriving is given)"),
    order:            str   | None = Query(default=None,
                                            description="latest | closest | lowest_alt | fastest_climb | aligned"),
    limit:            int   | None = Query(default=None, ge=1, le=50, description="max results (default 10)"),
):
    """Discover live flights matching a route filter or heuristic.

    Companion to `/api/map/track`: the agent calls this first to pick a
    flight ID, then calls `/api/map/track` with the result. Doing the
    matching server-side is the whole point — it stops the agent from
    looping over `/api/route/<callsign>` per live flight, which used
    to take hundreds of HTTP calls and time out the chat turn.
    """
    return await tool_find_flights(
        departing=departing, arriving=arriving, near=near,
        radius_km=radius_km, phase=phase,
        min_alt_m=min_alt_m, max_alt_m=max_alt_m,
        heading_deg=heading_deg, heading_tol_deg=heading_tol_deg,
        since_seconds=since_seconds, confirm_route=confirm_route,
        order=order, limit=limit,
    )


@app.get("/api/flight/{icao24}")
async def api_flight(
    icao24: str,
    lookback_hours: int = Query(24, ge=1, le=168),
) -> dict[str, Any]:
    """Combined "tell me everything you know about this aircraft" endpoint.

    Used by the side drawer when the user clicks an aircraft, *and* by
    the chat skill when the user asks "what's flight a1b2c3 doing?" or
    "where did UAL123 come from?". Returns:

      - `latest`: the most recent flight (or None) — the one to caption
        in the drawer as "from / to" / "departed at" / "arrived at".
      - `recent_flights`: the most recent flights flown by this icao24
        in the lookback window, decorated with the full airport record
        when we recognise the ICAO code (so chat can say "Denver, CO"
        instead of "KDEN").
      - `lookback_hours`: how far back the search window reached. Useful
        when the chat agent wants to widen / narrow the lookup.
    """
    flights = await fetch_aircraft_flights(icao24, lookback_hours * 3600)
    decorated: list[dict[str, Any]] = []
    for f in flights[:20]:
        decorated.append({
            "callsign": (f.get("callsign") or "").strip() or None,
            "first_seen": f.get("firstSeen"),
            "last_seen": f.get("lastSeen"),
            "departure": _airport_summary(f.get("estDepartureAirport")),
            "arrival": _airport_summary(f.get("estArrivalAirport")),
            "departure_candidates": f.get("departureAirportCandidatesCount") or 0,
            "arrival_candidates": f.get("arrivalAirportCandidatesCount") or 0,
        })
    return {
        "icao24": icao24.lower(),
        "latest": decorated[0] if decorated else None,
        "recent_flights": decorated,
        "lookback_hours": lookback_hours,
    }


@app.get("/api/flight/{icao24}/track")
async def api_flight_track(
    icao24: str,
    time: int = Query(
        0, ge=0,
        description="0 = most recent flight; otherwise unix seconds inside that flight's window",
    ),
) -> dict[str, Any]:
    """Return the recent waypoint track for an aircraft.

    OpenSky's /tracks/all returns each waypoint as a flat array
    `[t, lat, lon, alt_m, heading_deg, on_ground]`. We re-shape that
    into JSON objects so the JS client doesn't have to remember
    indexes, and we normalise altitude to feet to match the rest of
    the UI. When OpenSky has no track for this aircraft (or the
    endpoint is unavailable) we return `available=false` so the front
    end can fall back to the locally collected breadcrumb.
    """
    track = await fetch_aircraft_track(icao24, time)
    if track is None:
        return {
            "icao24": icao24.lower(),
            "available": False,
            "reason": "OpenSky tracks endpoint returned no data for this aircraft.",
            "waypoints": [],
        }
    raw = track.get("path") or []
    waypoints: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 6:
            continue
        ts, lat, lon, alt_m, hdg, on_ground = row[:6]
        if lat is None or lon is None:
            continue
        waypoints.append({
            "ts": ts,
            "lat": float(lat),
            "lon": float(lon),
            "alt_ft": round(float(alt_m) * 3.28084) if alt_m is not None else None,
            "heading": float(hdg) if hdg is not None else None,
            "on_ground": bool(on_ground),
        })
    return {
        "icao24": icao24.lower(),
        "available": True,
        "callsign": (track.get("callsign") or "").strip() or None,
        "start_time": track.get("startTime"),
        "end_time": track.get("endTime"),
        "waypoints": waypoints,
    }


# ── METAR / NAS Status / Aircraft Registry ─────────────────────────────────
# Three small operational-data overlays the chart layers on top of the FAA
# AIS feeds. Each gets its own in-process cache because the upstream APIs
# are friendly but rate-limited, and a busy chart can hammer them otherwise.

_metar_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_metar_lock = asyncio.Lock()
_nas_cache: tuple[float, list[dict[str, Any]]] | None = None
_nas_lock = asyncio.Lock()
_registry_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_registry_lock = asyncio.Lock()
_route_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_route_lock = asyncio.Lock()


def _metar_fl_cat(record: dict[str, Any]) -> str:
    """Compute the VFR/MVFR/IFR/LIFR category from a METAR record.

    AWC publishes `fltCat` directly when it can decode the report, but
    older or partial METARs come through with an empty/None category.
    Fall back to the FAA's published thresholds:
      - LIFR if visibility < 1 SM or ceiling < 500 ft
      - IFR  if visibility < 3 SM or ceiling < 1000 ft
      - MVFR if visibility < 5 SM or ceiling < 3000 ft
      - VFR  otherwise
    """
    cat = (record.get("fltCat") or "").strip().upper()
    if cat in ("VFR", "MVFR", "IFR", "LIFR"):
        return cat
    visib = record.get("visib")
    try:
        if isinstance(visib, str):
            # AWC uses "10+" for 10+ SM and bare numbers otherwise.
            visib = float(visib.replace("+", ""))
    except (TypeError, ValueError):
        visib = None
    # Ceiling = lowest BKN/OVC/VV layer; AWC ships a `clouds` array.
    ceiling = None
    for layer in record.get("clouds") or []:
        cover = (layer.get("cover") or "").upper()
        base = layer.get("base")
        if cover in ("BKN", "OVC", "VV") and isinstance(base, (int, float)):
            ceiling = base if ceiling is None else min(ceiling, base)
    v_lifr = isinstance(visib, (int, float)) and visib < 1
    c_lifr = ceiling is not None and ceiling < 500
    v_ifr  = isinstance(visib, (int, float)) and visib < 3
    c_ifr  = ceiling is not None and ceiling < 1000
    v_mvfr = isinstance(visib, (int, float)) and visib < 5
    c_mvfr = ceiling is not None and ceiling < 3000
    if v_lifr or c_lifr:
        return "LIFR"
    if v_ifr or c_ifr:
        return "IFR"
    if v_mvfr or c_mvfr:
        return "MVFR"
    return "VFR"


@app.get("/api/weather/metar")
async def api_weather_metar(
    bbox: str | None = Query(
        default=None,
        description="west,south,east,north — defaults to CONUS if omitted",
    ),
) -> dict[str, Any]:
    """Latest METAR observations for stations inside `bbox`.

    Each station is returned as `{lat, lon, station, fltCat, raw, ...}`
    so the front end can drop a single dot per airport coloured by VFR
    category. Cached server-side for METAR_CACHE_TTL seconds because
    AWC publishes new observations only on the hour and our chart
    refetches on every map move.
    """
    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    if bbox:
        try:
            w, s, e, n = (float(x) for x in bbox.split(","))
        except ValueError as exc:
            raise HTTPException(400, "bbox must be 'west,south,east,north'") from exc
    else:
        # CONUS-ish default so chat-only callers still get something.
        w, s, e, n = -125.0, 24.0, -66.0, 50.0
    # Round to 1° so successive moveends inside a city share a cache slot.
    rw, rs, re_, rn = round(w), round(s), round(e), round(n)
    cache_key = f"{rw},{rs},{re_},{rn}"
    now = time.time()
    async with _metar_lock:
        cached = _metar_cache.get(cache_key)
        if cached and (now - cached[0]) < METAR_CACHE_TTL:
            return cached[1]

    try:
        r = await _http.get(
            AWC_METAR_URL,
            params={
                # AWC bbox ordering is lat0,lon0,lat1,lon1
                # (i.e. minLat,minLon,maxLat,maxLon — south,west,north,east).
                # Our wire format (and the rest of this file) uses GeoJSON
                # ordering (west,south,east,north), so swap when calling out.
                "bbox": f"{rs},{rw},{rn},{re_}",
                "format": "json",
            },
            headers={"User-Agent": "FlightOps-NemoClaw/1.0"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"AWC upstream error: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"AWC returned {r.status_code}")
    try:
        records = r.json()
    except Exception:
        records = []
    if not isinstance(records, list):
        records = []

    stations: list[dict[str, Any]] = []
    for rec in records:
        lat = rec.get("lat")
        lon = rec.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        stations.append({
            "station": rec.get("icaoId") or rec.get("metarSiteId"),
            "name": rec.get("name"),
            "lat": float(lat),
            "lon": float(lon),
            "elev_m": rec.get("elev"),
            "obs_time": rec.get("obsTime"),
            "report_time": rec.get("reportTime"),
            "flt_cat": _metar_fl_cat(rec),
            "temp_c": rec.get("temp"),
            "dewp_c": rec.get("dewp"),
            "wind_dir": rec.get("wdir"),
            "wind_kt": rec.get("wspd"),
            "wind_gust_kt": rec.get("wgst"),
            "visib_sm": rec.get("visib"),
            "altim_hpa": rec.get("altim"),
            "wx_string": rec.get("wxString"),
            "raw": rec.get("rawOb"),
        })
    payload = {"bbox": cache_key, "count": len(stations), "stations": stations}
    async with _metar_lock:
        _metar_cache[cache_key] = (now, payload)
        # Keep memory bounded — 32 bbox slots is more than a single
        # session ever produces.
        if len(_metar_cache) > 32:
            for k, _ in sorted(_metar_cache.items(), key=lambda kv: kv[1][0])[
                : len(_metar_cache) - 32
            ]:
                _metar_cache.pop(k, None)
    return payload


def _normalize_nas_event(rec: dict[str, Any]) -> dict[str, Any]:
    """Flatten a NAS Status airport-event record into a chart-friendly shape.

    The upstream payload has lots of optional sub-objects — groundStop,
    groundDelay, airportClosure, arrivalDelay, departureDelay, airportConfig,
    deicing, freeForm — most of them None for any given airport. We
    extract the ones the chart cares about (Ground Stop, GDP, Closure,
    delays) and tag a single `severity` so the front end can colour the
    airport dot without re-implementing the priority rules.
    """
    aid = (rec.get("airportId") or "").strip().upper()
    lat = rec.get("latitude")
    lon = rec.get("longitude")
    try:
        lat = float(lat) if lat not in (None, "") else None
        lon = float(lon) if lon not in (None, "") else None
    except (TypeError, ValueError):
        lat, lon = None, None
    # NAS Status often ships the events without the airport coordinates
    # (most records expect the consumer to know where each FAA 3-letter
    # airport sits). Backfill from our local airports DB so the chart
    # can drop a dot at the right place.
    if lat is None or lon is None:
        ref = AIRPORT_BY_IATA.get(aid) or AIRPORT_BY_ICAO.get(aid)
        if ref is None and len(aid) == 3:
            ref = AIRPORT_BY_ICAO.get(f"K{aid}")
        if ref is not None:
            lat = lat if lat is not None else ref.get("lat")
            lon = lon if lon is not None else ref.get("lon")
    out: dict[str, Any] = {
        "airport": aid,
        "name": rec.get("airportLongName"),
        "lat": lat,
        "lon": lon,
        "events": [],
        "severity": "info",   # info < advisory < delay < ground_stop < closure
    }
    severity_rank = {
        "info": 0, "advisory": 1, "delay": 2, "ground_stop": 3, "closure": 4,
    }

    def bump(level: str) -> None:
        if severity_rank[level] > severity_rank[out["severity"]]:
            out["severity"] = level

    gs = rec.get("groundStop") or {}
    if gs:
        out["events"].append({
            "kind": "ground_stop",
            "reason": gs.get("reason"),
            "end_time": gs.get("endTime"),
            "include": gs.get("include"),
            "exclude": gs.get("exclude"),
        })
        bump("ground_stop")
    gdp = rec.get("groundDelay") or {}
    if gdp:
        out["events"].append({
            "kind": "ground_delay",
            "reason": gdp.get("reason"),
            "avg_delay_min": gdp.get("avgDelay"),
            "max_delay_min": gdp.get("maxDelay"),
            "end_time": gdp.get("endTime"),
        })
        bump("delay")
    closure = rec.get("airportClosure") or {}
    if closure:
        out["events"].append({
            "kind": "closure",
            "reason": closure.get("reason"),
            "start_time": closure.get("startTime"),
            "end_time": closure.get("endTime"),
        })
        bump("closure")
    for key, kind in (("arrivalDelay", "arrival_delay"), ("departureDelay", "departure_delay")):
        d = rec.get(key) or {}
        if d:
            out["events"].append({
                "kind": kind,
                "min_delay": d.get("min"),
                "max_delay": d.get("max"),
                "trend": d.get("trend"),
                "reason": d.get("reason"),
            })
            bump("delay")
    deicing = rec.get("deicing") or {}
    if deicing:
        out["events"].append({"kind": "deicing", **deicing})
        bump("advisory")
    cfg = rec.get("airportConfig") or {}
    if cfg:
        out["events"].append({
            "kind": "config",
            "departure": cfg.get("departureRunway"),
            "arrival": cfg.get("arrivalRunway"),
        })
        bump("info")
    free = rec.get("freeForm") or {}
    if free:
        out["events"].append({
            "kind": "advisory",
            "text": free.get("text"),
            "simple_text": free.get("simpleText"),
            "start_time": free.get("startTime"),
            "end_time": free.get("endTime"),
        })
        bump("advisory")
    return out


async def fetch_nas_status(force: bool = False) -> list[dict[str, Any]]:
    """Pull the current NAS Status airport-events feed (cached)."""
    global _nas_cache
    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    now = time.time()
    async with _nas_lock:
        if not force and _nas_cache and (now - _nas_cache[0]) < NAS_CACHE_TTL:
            return _nas_cache[1]
    try:
        r = await _http.get(
            NAS_STATUS_URL,
            headers={"Accept": "application/json", "User-Agent": "FlightOps-NemoClaw/1.0"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        # Don't fail the chart — fall back to the last cached payload.
        if _nas_cache:
            return _nas_cache[1]
        raise HTTPException(502, f"NAS Status upstream error: {exc}") from exc
    if r.status_code >= 400:
        if _nas_cache:
            return _nas_cache[1]
        raise HTTPException(r.status_code, f"NAS Status returned {r.status_code}")
    try:
        raw = r.json()
    except Exception:
        raw = []
    if not isinstance(raw, list):
        raw = []
    events = [_normalize_nas_event(rec) for rec in raw if isinstance(rec, dict)]
    async with _nas_lock:
        _nas_cache = (now, events)
    return events


@app.get("/api/nas/status")
async def api_nas_status() -> dict[str, Any]:
    """All currently-active NAS airport advisories (Ground Stops, GDPs, closures…).

    Returns a flat list keyed by airport plus a tiny summary so the front
    end can colour airport dots and the chat skill can answer questions
    like "any ground stops at JFK?" without having to inspect each event.
    """
    events = await fetch_nas_status()
    by_severity: dict[str, int] = {}
    for ev in events:
        by_severity[ev["severity"]] = by_severity.get(ev["severity"], 0) + 1
    return {
        "fetched_at": int(time.time()),
        "count": len(events),
        "by_severity": by_severity,
        "events": events,
    }


@app.get("/api/nas/airport/{code}")
async def api_nas_airport(code: str) -> dict[str, Any]:
    """Per-airport NAS advisory lookup (FAA 3-letter or ICAO 4-letter)."""
    target = (code or "").strip().upper()
    if not target:
        raise HTTPException(400, "airport code required")
    # The NAS feed uses the FAA 3-letter id; if we got an ICAO we trim
    # the leading 'K' for CONUS so callers can pass either.
    candidates = {target, target.lstrip("K")} if len(target) == 4 else {target}
    events = await fetch_nas_status()
    for ev in events:
        if ev["airport"] in candidates:
            return {"ok": True, **ev}
    return {"ok": True, "airport": target, "events": [], "severity": "none"}


async def _adsbdb_get(url: str) -> dict[str, Any] | None:
    """GET an adsbdb.com endpoint, returning the inner `response` payload."""
    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    try:
        r = await _http.get(
            url,
            headers={"User-Agent": "FlightOps-NemoClaw/1.0"},
            timeout=12.0,
        )
    except httpx.HTTPError:
        return None
    if r.status_code in (404, 410):
        return None
    if r.status_code >= 400:
        return None
    try:
        body = r.json()
    except Exception:
        return None
    return (body or {}).get("response")


async def _hexdb_get(icao24: str) -> dict[str, Any] | None:
    """Fallback: hexdb.io aircraft lookup. Same data, simpler shape."""
    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    try:
        r = await _http.get(
            f"{HEXDB_AIRCRAFT_URL}/{icao24.upper()}",
            headers={"User-Agent": "FlightOps-NemoClaw/1.0"},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


@app.get("/api/registry/{icao24}")
async def api_registry(icao24: str) -> dict[str, Any]:
    """Aircraft registry lookup by 24-bit ICAO hex.

    Combines two free public registries (adsbdb.com primary, hexdb.io
    fallback) into a single normalised response: registration ('N12345'
    / 'G-XXXX'), manufacturer, type, ICAO type code, registered owner,
    country, and a photo URL when one is on file. Used by the side
    drawer when the user clicks a plane *and* by the NemoClaw chat
    skill when the user asks "who flies a8ae7e?" or "what's N12345?".
    """
    icao = (icao24 or "").strip().lower()
    if not icao or len(icao) > 8:
        raise HTTPException(400, "icao24 hex required")
    cache_key = f"reg:{icao}"
    now = time.time()
    async with _registry_lock:
        cached = _registry_cache.get(cache_key)
        if cached and (now - cached[0]) < REGISTRY_CACHE_TTL:
            payload = cached[1]
            return payload if payload is not None else {"icao24": icao, "found": False}

    primary = await _adsbdb_get(f"{ADSBDB_AIRCRAFT_URL}/{icao}")
    out: dict[str, Any] | None = None
    if primary and primary.get("aircraft"):
        a = primary["aircraft"]
        out = {
            "icao24": icao,
            "found": True,
            "source": "adsbdb",
            "registration": a.get("registration"),
            "manufacturer": a.get("manufacturer"),
            "type": a.get("type"),
            "icao_type": a.get("icao_type"),
            "owner": a.get("registered_owner"),
            "owner_country": a.get("registered_owner_country_name"),
            "owner_country_iso": a.get("registered_owner_country_iso_name"),
            "operator_flag": a.get("registered_owner_operator_flag_code"),
            "photo_url": a.get("url_photo"),
            "photo_thumb_url": a.get("url_photo_thumbnail"),
        }
    else:
        fallback = await _hexdb_get(icao)
        if fallback and fallback.get("Registration"):
            out = {
                "icao24": icao,
                "found": True,
                "source": "hexdb",
                "registration": fallback.get("Registration"),
                "manufacturer": fallback.get("Manufacturer"),
                "type": fallback.get("Type"),
                "icao_type": fallback.get("ICAOTypeCode"),
                "owner": fallback.get("RegisteredOwners"),
                "owner_country": None,
                "owner_country_iso": None,
                "operator_flag": fallback.get("OperatorFlagCode"),
                "photo_url": None,
                "photo_thumb_url": None,
            }

    async with _registry_lock:
        _registry_cache[cache_key] = (now, out)
        if len(_registry_cache) > 512:
            for k, _ in sorted(_registry_cache.items(), key=lambda kv: kv[1][0])[
                : len(_registry_cache) - 512
            ]:
                _registry_cache.pop(k, None)
    return out if out else {"icao24": icao, "found": False}


@app.get("/api/route/{callsign}")
async def api_route(callsign: str) -> dict[str, Any]:
    """Origin/destination + airline lookup for a flight callsign.

    Wraps adsbdb's `/v0/callsign/<id>` endpoint, which combines OpenSky
    /flights data with airline + airport metadata. Lets the chat skill
    answer "where is UAL123 going?" with a real origin/destination
    even when OpenSky's per-aircraft `flights/aircraft` endpoint hasn't
    yet linked the in-progress flight to a departure airport.
    """
    cs = (callsign or "").strip().upper()
    if not cs:
        raise HTTPException(400, "callsign required")
    cache_key = f"rt:{cs}"
    now = time.time()
    async with _route_lock:
        cached = _route_cache.get(cache_key)
        if cached and (now - cached[0]) < REGISTRY_CACHE_TTL:
            payload = cached[1]
            return payload if payload is not None else {"callsign": cs, "found": False}

    raw = await _adsbdb_get(f"{ADSBDB_CALLSIGN_URL}/{cs}")
    out: dict[str, Any] | None = None
    if raw and raw.get("flightroute"):
        fr = raw["flightroute"]
        out = {
            "callsign": cs,
            "found": True,
            "callsign_iata": fr.get("callsign_iata"),
            "callsign_icao": fr.get("callsign_icao"),
            "airline": fr.get("airline"),
            "origin": fr.get("origin"),
            "destination": fr.get("destination"),
        }
    async with _route_lock:
        _route_cache[cache_key] = (now, out)
        if len(_route_cache) > 512:
            for k, _ in sorted(_route_cache.items(), key=lambda kv: kv[1][0])[
                : len(_route_cache) - 512
            ]:
                _route_cache.pop(k, None)
    return out if out else {"callsign": cs, "found": False}


_AIRPORT_TYPE_ALIASES = {
    "large": "large_airport",
    "medium": "medium_airport",
    "small": "small_airport",
    "large_airport": "large_airport",
    "medium_airport": "medium_airport",
    "small_airport": "small_airport",
}


@app.get("/api/airports")
async def api_airports(
    bbox: str | None = Query(
        default=None,
        description="west,south,east,north — only airports inside this box are returned.",
    ),
    types: str | None = Query(
        default=None,
        description=(
            "Comma-separated airport-type filter. Accepts long form "
            "(`large_airport,medium_airport,small_airport`) or short "
            "(`large,medium,small`). Default keeps all types — useful when "
            "the client wants every dot at high zoom."
        ),
    ),
    limit: int = Query(default=500, ge=1, le=20000),
):
    """Return airports inside `bbox` (or the global list when bbox is omitted).

    The OurAirports-derived dataset has ~11k entries. To keep the wire
    payload bounded the client tiers its requests by zoom: at low zoom it
    asks for `types=large`, mid zoom `types=large,medium`, high zoom drops
    the filter so all dots show up. AIRPORTS is pre-sorted large→medium→
    small so the bbox loop hits the most important airports first when
    `limit` truncates the result.
    """
    parsed = _parse_bbox(bbox)

    type_filter: set[str] | None = None
    if types:
        wanted = {_AIRPORT_TYPE_ALIASES.get(t.strip().lower()) for t in types.split(",") if t.strip()}
        wanted.discard(None)
        if not wanted:
            raise HTTPException(
                status_code=400,
                detail="types must be a subset of large, medium, small",
            )
        type_filter = wanted  # type: ignore[assignment]

    def _accept(a: dict[str, Any]) -> bool:
        return type_filter is None or a.get("type") in type_filter

    out: list[dict[str, Any]] = []
    if parsed is None:
        for a in AIRPORTS:
            if _accept(a):
                out.append(a)
                if len(out) >= limit:
                    break
    else:
        s, n, w, e = parsed
        for a in AIRPORTS:
            if not _accept(a):
                continue
            if s <= a["lat"] <= n and w <= a["lon"] <= e:
                out.append(a)
                if len(out) >= limit:
                    break
    return {
        "airports": out,
        "count": len(out),
        "total": len(AIRPORTS),
        "truncated": len(out) >= limit,
    }


@app.get("/api/airport/{code}")
async def api_airport(code: str):
    a = find_airport(code)
    if a is None:
        raise HTTPException(status_code=404, detail="airport not found")
    return a


@app.get("/api/airspace/lookup")
async def api_airspace_lookup(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(default=50.0, gt=0, le=2000),
    datasets: str = Query(
        default="sua,tfrs,runways",
        description="comma-separated list. global: sua,classes,tfrs,runways. "
                    "bbox-only: taxiways,obstacles,ats."
    ),
) -> dict[str, Any]:
    """What aviation features are at or near this point?

    Used by the OpenClaw chat agent when it's reasoning about a specific
    flight or airport ("is N12345 cutting through restricted airspace?",
    "any tall obstacles within 5 km of KIAD?", "what runways at KSAN").

    Global datasets (sua/classes/tfrs/runways) are point-in-polygon tested
    against the cached GeoJSON. Bbox-only datasets (taxiways/obstacles/ats)
    are queried with a square bbox of half-side `radius_km`. Returns:
      - `containing`: features whose polygon contains the point (polygon
                      datasets only — for points/lines we use the centroid).
      - `nearby`:     features whose centroid/bbox is within `radius_km`.
    """
    wanted = {d.strip() for d in datasets.split(",") if d.strip()}
    known = set(FAA_DATASETS) | set(FAA_BBOX_DATASETS)
    unknown = wanted - known
    if unknown:
        raise HTTPException(400, f"unknown datasets: {sorted(unknown)}")

    containing: list[dict[str, Any]] = []
    nearby: list[dict[str, Any]] = []

    # bbox for the bbox-only datasets — square of side 2*radius_km centred
    # on (lat, lon). Margin is generous since the FAA service does the
    # filtering server-side anyway.
    s, n, w, e = bbox_from_center(lat, lon, radius_km)

    for name in wanted:
        try:
            if name in FAA_DATASETS:
                data = await fetch_airspace(name)
            else:
                data = await fetch_airspace_bbox(name, (s, n, w, e))
        except HTTPException:
            continue

        for feat in data.get("features", []):
            geom_type = (feat.get("geometry") or {}).get("type") or ""
            inside = False
            distance_km: float | None = None

            if geom_type in ("Polygon", "MultiPolygon"):
                if _feature_contains(feat, lat, lon):
                    inside = True
                elif _feature_within_radius(feat, lat, lon, radius_km):
                    distance_km = 0.0  # bbox-near is "close enough"
            else:
                # Point/Line — use centroid distance.
                centroid = _feature_centroid(feat)
                if centroid:
                    distance_km = haversine_km(lat, lon, centroid[1], centroid[0])
                    if distance_km <= 0.5:
                        inside = True
                    elif distance_km > radius_km:
                        continue
            if inside:
                containing.append(_summarize(feat, name))
            elif distance_km is not None:
                summary = _summarize(feat, name)
                summary["distance_km"] = round(distance_km, 1) if distance_km else 0.0
                nearby.append(summary)

    # Sort nearby by distance so the agent sees the closest first, then
    # cap so the LLM context stays reasonable.
    nearby.sort(key=lambda x: x.get("distance_km", 0.0))
    return {
        "lat": lat, "lon": lon, "radius_km": radius_km,
        "containing": containing[:60],
        "nearby": nearby[:60],
        "counts": {"containing": len(containing), "nearby": len(nearby)},
    }


@app.get("/api/airspace/{name}")
async def api_airspace(
    name: str,
    bbox: str | None = Query(
        default=None,
        description="west,south,east,north — required for taxiways/obstacles/ats",
    ),
) -> JSONResponse:
    """Cached GeoJSON proxy for one of the FAA datasets.

    Global datasets (sua, classes, tfrs, runways) are returned in full.
    Bbox-only datasets (taxiways, obstacles, ats) require a `bbox` query
    parameter — the FAA layers are too large to ship globally.
    """
    if name in FAA_DATASETS:
        data = await fetch_airspace(name)
        cached = _airspace_cache.get(name)
        age = int(time.time() - cached[0]) if cached else 0
        ttl = FAA_DATASETS[name]["ttl_s"]
        label = FAA_DATASETS[name]["label"]
        return JSONResponse(
            content=data,
            headers={
                "Cache-Control": f"public, max-age={max(60, ttl - age)}",
                "X-Dataset-Label": label,
                "X-Dataset-Age-S": str(age),
            },
        )
    if name in FAA_BBOX_DATASETS:
        parsed = _parse_bbox(bbox)
        if parsed is None:
            raise HTTPException(
                400,
                f"dataset '{name}' is bbox-only — pass ?bbox=west,south,east,north",
            )
        data = await fetch_airspace_bbox(name, parsed)
        return JSONResponse(
            content=data,
            headers={
                "Cache-Control": f"public, max-age={BBOX_CACHE_TTL}",
                "X-Dataset-Label": FAA_BBOX_DATASETS[name]["label"],
            },
        )
    raise HTTPException(
        404,
        f"unknown dataset '{name}'. Try one of: "
        f"{sorted(set(FAA_DATASETS) | set(FAA_BBOX_DATASETS))}",
    )


@app.get("/api/analyze")
async def api_analyze(airport: str, radius_km: float = DEFAULT_ANALYSIS_RADIUS_KM):
    return await tool_analyze_traffic(airport, radius_km)


class GotoBody(BaseModel):
    target: str
    zoom: float | None = None
    pitch: float | None = None      # 0–70°  (0 = top-down, 60 ≈ "looking across")
    bearing: float | None = None    # 0–360° (compass heading the camera faces)


@app.post("/api/map/goto")
async def api_map_goto(body: GotoBody):
    return await tool_goto(body.target, body.zoom, body.pitch, body.bearing)


class ArcBody(BaseModel):
    airport: str
    radius_km: float = DEFAULT_ANALYSIS_RADIUS_KM
    tilt: bool = True               # auto-angle the camera so arcs read as 3D parabolas


@app.post("/api/map/arcs")
async def api_map_arcs(body: ArcBody):
    return await tool_show_arcs_to_airport(body.airport, body.radius_km, tilt=body.tilt)


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


class TrackBody(BaseModel):
    """Combined plane-lookup + highlight + camera-move.

    Either of `flight`, `callsign`, or `icao24` identifies the target.
    `flight` is auto-detected: 6 hex chars → ICAO24, anything else →
    callsign. The optional pose fields override the defaults the
    server picks (zoom 10, no pitch/bearing change).
    """
    flight:   str | None = None
    callsign: str | None = None
    icao24:   str | None = None
    zoom:     float | None = None
    pitch:    float | None = None
    bearing:  float | None = None


@app.post("/api/map/track")
async def api_map_track(body: TrackBody):
    cs  = body.callsign
    hex24 = body.icao24
    # Auto-classify a single `flight` token. ICAO24 transponder hex IDs
    # are six hex digits (e.g. a2ca5d); flight callsigns are
    # alphanumeric, usually 4–8 chars (e.g. UAL108, AAL2429). A 6-hex
    # input could in theory collide with a hex-only callsign, but that's
    # vanishingly rare in the real-world callsign space and ICAO24 is
    # the more useful default for a tracking flow.
    if body.flight and not (cs or hex24):
        v = body.flight.strip()
        if len(v) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in v):
            hex24 = v
        else:
            cs = v
    return await tool_track_flight(
        callsign=cs,
        icao24=hex24,
        zoom=body.zoom,
        pitch=body.pitch,
        bearing=body.bearing,
    )


class ColorBody(BaseModel):
    mode: str


@app.post("/api/map/color")
async def api_map_color(body: ColorBody):
    """Switch the aircraft colour scheme on every connected browser.

    Accepts either a canonical mode key (`phase`, `altitude`, `vrate`,
    `squawk`) or one of the aliases in COLOR_MODE_ALIASES so the chat
    skill can pass through user phrasing like "altitude" or
    "rate of climb" verbatim."""
    return await tool_set_color_mode(body.mode)


class MetarColorBody(BaseModel):
    mode: str


@app.post("/api/map/metar-color")
async def api_map_metar_color(body: MetarColorBody):
    """Switch the METAR overlay's circle colour mode on every browser.

    Accepts `flt_cat`, `wind`, `temp`, `visibility`, plus aliases like
    `flight category`, `wind speed`, `temperature`, `vis`.
    """
    return await tool_set_metar_color_mode(body.mode)


class FilterBody(BaseModel):
    """Bucket filter for the chip legend.

    Exactly one of `buckets`, `include`, `exclude`, `only`, `reset`
    should be supplied. `buckets` is the most explicit and the path
    you want for chat-driven flows that need to be deterministic.
    """
    mode: str                          # "phase" | "squawk"
    buckets: list[str] | None = None   # full replacement of the armed set
    include: list[str] | None = None   # add these to the current armed set
    exclude: list[str] | None = None   # remove these from the current armed set
    only:    str        | None = None  # shortcut name (e.g. "emergency", "landing")
    reset:   bool              = False # re-arm every bucket (= no filtering)


@app.post("/api/map/filter")
async def api_map_filter(body: FilterBody):
    return await tool_set_filter(
        body.mode,
        buckets=body.buckets,
        include=body.include,
        exclude=body.exclude,
        only=body.only,
        reset=body.reset,
    )


class ViewBody(BaseModel):
    lat: float | None = None
    lon: float | None = None
    zoom: float | None = None
    pitch: float | None = None
    bearing: float | None = None


@app.post("/api/map/view")
async def api_map_view(body: ViewBody):
    """Free-form camera control. Any field left null is preserved.

    Useful for angling the map without re-targeting an airport — e.g.
    "tilt the map to 60 degrees" → {"pitch":60}, or "spin north" →
    {"bearing":0}. Pair with `/api/map/goto` when you also want to
    pan; `/api/map/view` is the no-pan-only-pose version.
    """
    return await tool_set_view(
        lat=body.lat, lon=body.lon, zoom=body.zoom,
        pitch=body.pitch, bearing=body.bearing,
    )


class Airspace3DBody(BaseModel):
    enabled: bool


@app.post("/api/map/airspace3d")
async def api_map_airspace3d(body: Airspace3DBody):
    """Toggle 3D extrusion of airspace polygons + plane-altitude lift."""
    return await tool_set_airspace3d(body.enabled)


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
    return await call_openclaw_agent(
        message=body.message,
        session_id=body.session_id,
        thinking=body.thinking,
    )


# ── /api/chat/stream — live tool-call & thinking trace ────────────────────
#
# The agent CLI itself doesn't have a streaming output mode (only
# `--json` for batch), but it writes its session JSONL incrementally
# during the turn. We piggy-back on that file: spawn the subprocess,
# poll the JSONL while the subprocess is alive, and emit each new
# event as a line of NDJSON to the client. The final assistant reply
# (parsed from the subprocess's stdout JSON when it exits) is sent as
# a `done` event.
#
# Wire shape (one JSON object per line, `\n` delimited):
#   {"type":"event","kind":"tool_call","tool":"exec","command":"curl …"}
#   {"type":"event","kind":"tool_result","is_error":false,"text":"{ … }"}
#   {"type":"event","kind":"thought","text":"…"}
#   {"type":"done","reply":"…","session_id":"…","status":"ok"}
#   {"type":"error","error":"…"}                        (on failure)
#
# The client buffers events until `done` and renders them under the
# assistant bubble live, regardless of whether the user has the
# trace-toggles on (off = events arrive but get dropped).
@app.post("/api/chat/stream")
async def api_chat_stream(body: ChatRequest):
    if not Path(OPENCLAW_BIN).exists():
        raise HTTPException(
            status_code=503,
            detail=f"openclaw binary not found at {OPENCLAW_BIN}",
        )

    started_at_ms = int(time.time() * 1000)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    cmd = [
        OPENCLAW_BIN, "agent",
        "--agent", OPENCLAW_AGENT,
        "--message", body.message,
        "--json",
        "--thinking", body.thinking or "off",
        "--timeout", str(OPENCLAW_TIMEOUT_S),
    ]
    if body.session_id:
        cmd.extend(["--session-id", body.session_id])

    async def run_and_tail() -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Drain stdout/stderr concurrently into byte buffers so
        # the subprocess can never block on a full pipe while we're
        # busy polling the session file.
        stdout_buf = bytearray()
        stderr_buf = bytearray()

        async def drain(reader: asyncio.StreamReader, buf: bytearray) -> None:
            while True:
                chunk = await reader.read(8192)
                if not chunk:
                    return
                buf.extend(chunk)

        drain_out = asyncio.create_task(drain(proc.stdout, stdout_buf))   # type: ignore[arg-type]
        drain_err = asyncio.create_task(drain(proc.stderr, stderr_buf))   # type: ignore[arg-type]

        offsets: dict[str, int] = {}
        deadline = time.time() + OPENCLAW_TIMEOUT_S + 30
        try:
            while True:
                path = _resolve_session_path(body.session_id, started_at_ms)
                if path is not None:
                    for ev in _read_new_events(path, started_at_ms, offsets):
                        await queue.put({"type": "event", **ev})
                if proc.returncode is not None:
                    break
                if time.time() > deadline:
                    proc.kill()
                    break
                await asyncio.sleep(0.20)

            # One last drain in case the subprocess wrote the closing
            # JSONL records right before exit.
            await asyncio.gather(drain_out, drain_err)
            path = _resolve_session_path(body.session_id, started_at_ms)
            if path is not None:
                for ev in _read_new_events(path, started_at_ms, offsets):
                    await queue.put({"type": "event", **ev})

            if proc.returncode != 0:
                err = bytes(stderr_buf).decode("utf-8", errors="replace")[-1500:]
                await queue.put({
                    "type":  "error",
                    "error": f"agent exited {proc.returncode}: {err.strip()[:500]}",
                })
                return

            raw = bytes(stdout_buf).decode("utf-8", errors="replace").strip()
            first = raw.find("{")
            if first < 0:
                await queue.put({"type": "error", "error": "non-JSON agent output"})
                return
            try:
                payload = json.loads(raw[first:])
            except json.JSONDecodeError as exc:
                await queue.put({"type": "error", "error": f"bad agent JSON: {exc}"})
                return

            reply, sid = _extract_reply(payload)
            await queue.put({
                "type":       "done",
                "reply":      reply,
                "session_id": sid,
                "status":     payload.get("status"),
                "summary":    payload.get("summary"),
            })
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_and_tail())

    async def gen() -> AsyncIterator[bytes]:
        try:
            while True:
                msg = await queue.get()
                if msg is None:
                    return
                yield (json.dumps(msg) + "\n").encode("utf-8")
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


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
    # The HTML carries `?v=NN` cache-busters for app.js / styles.css. If the
    # browser caches index.html itself, it'll keep loading old asset URLs and
    # the cache-busters become useless. no-cache forces revalidation on every
    # navigate while still allowing 304 Not Modified for unchanged HTML.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


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
