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
- OpenSky is reachable anonymously at ~400 credits/day. If
  OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET are set we run the OAuth2
  client-credentials flow against auth.opensky-network.org and use the
  resulting bearer token for /states/all calls — that lifts the daily
  budget to ~4,000 credits. (HTTP Basic was removed in March 2026.)
  Responses are cached briefly in-process to avoid hammering the API when
  several browsers are open.
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
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ── Constants ───────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = STATIC_DIR / "data"

OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
OPENSKY_TRACKS_URL = "https://opensky-network.org/api/tracks/all"
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

# Aviation Weather Center — METARs / TAFs / station data. The bbox order
# expected by AWC is `lonW,latS,lonE,latN`. `format=json` returns one
# object per station with `lat`, `lon`, `fltCat` (VFR/MVFR/IFR/LIFR),
# `temp`, `dewp`, `wdir`, `wspd`, `visib`, `altim`, `rawOb`, `wxString`.
AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
METAR_CACHE_TTL = 5 * 60  # AWC publishes hourly; 5 min keeps the chart fresh

# FAA NAS Status — Air Traffic Control System Command Center publishes
# every active airport-level event (Ground Stop, Ground Delay Program,
# Airport Closure, AFP, deicing, etc.) as a single JSON payload. The
# response is one object per affected airport with sub-objects for each
# event type (groundStop, groundDelay, airportClosure, freeForm, …).
NAS_STATUS_URL = "https://nasstatus.faa.gov/api/airport-events"
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
      1. OAuth2 client_credentials (the only auth OpenSky supports as of
         March 2026 for new installs).
      2. Legacy HTTP Basic, kept for backwards compatibility with internal
         mirrors / older deployments. Only used if OAuth2 isn't configured.
    """
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
    await _bus.broadcast(payload)
    return {"ok": True, "count": len(arcs), "airport": a["code"], "tilted": bool(tilt)}


async def tool_set_layer(layer: str, visible: bool) -> dict[str, Any]:
    payload = {"type": "layer", "layer": layer, "visible": bool(visible)}
    await _bus.broadcast(payload)
    return {"ok": True, **payload}


async def tool_highlight_flight(flight: str) -> dict[str, Any]:
    payload = {"type": "highlight", "flight": flight.strip().upper()}
    await _bus.broadcast(payload)
    return {"ok": True, **payload}


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
    await _bus.broadcast(payload)
    return {"ok": True, **payload}


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
    await _bus.broadcast(payload)
    return {"ok": True, **payload}


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

    await _bus.broadcast(msg)
    return {"ok": True, **msg}


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
    await _bus.broadcast(payload)
    return {"ok": True, **payload}


# ── 3D airspace toggle ──────────────────────────────────────────────────
async def tool_set_airspace3d(enabled: bool) -> dict[str, Any]:
    payload = {"type": "airspace3d", "enabled": bool(enabled)}
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
    return {
        "reply": reply,
        "session_id": sid,
        "status": payload.get("status"),
        "summary": payload.get("summary"),
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
    if _opensky_tokens.configured:
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
