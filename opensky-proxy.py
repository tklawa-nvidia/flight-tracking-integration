#!/usr/bin/env python3
"""Host-side API proxy for FlightOps' live-aircraft integration.

Runs on the host (the brev VM, outside the sandbox) and gives the
sandbox a single, stable OpenSky-shaped endpoint for live aircraft —
regardless of where the data actually comes from. Mirrors the tier-1
pattern: any secrets live exclusively on the host, the sandbox only
knows the proxy URL.

Two data sources are supported, selected with FLIGHT_ADSB_SOURCE:

  * "community" (DEFAULT) — fetch live state vectors from the free,
    no-auth community ADS-B aggregators (adsb.fi / airplanes.live /
    adsb.lol) and translate them into OpenSky's /states/all schema on
    the fly. This is the reliable path from cloud VMs: OpenSky
    blackholes datacenter/cloud IP ranges (AWS/GCP/Azure), so a GCP
    brev box simply cannot reach opensky-network.org — the SYN is
    dropped inside OpenSky's network (verified via tcptraceroute).
    The community feeds have no such block and need no credentials.

  * "opensky" — legacy behaviour: forward to opensky-network.org with
    an OAuth2 Bearer token minted from ~/.nemoclaw/credentials.json.
    Kept for on-prem / non-cloud hosts whose egress OpenSky still
    accepts.

Because both sources are normalised to OpenSky's positional state-vector
format, the sandbox app (app/server.py `_decode_state`) and the browser
UI work unchanged.

Routes (unchanged contract with the sandbox):
    GET  /api/states/all          -> live state vectors {"time", "states"[]}
    GET  /api/flights/aircraft    -> per-aircraft flight history
    GET  /api/tracks/all          -> per-aircraft waypoint track
    GET  /health                  -> 200 "ok"
    GET  /                        -> 200 with route map (handy for debugging)

In "community" mode /api/flights/aircraft and /api/tracks/all return a
graceful 404 (the aggregators don't expose historical flights/tracks);
the sandbox already treats 404 as "no data" and degrades cleanly
(planes still render, only origin/track enrichment is skipped).

Usage:
    python3 opensky-proxy.py [--port 9202]

Environment:
    FLIGHT_ADSB_SOURCE      "community" (default) | "opensky"
    FLIGHT_ADSB_PROVIDERS   comma list, default "adsb.fi,airplanes.live,adsb.lol"
    FLIGHT_ADSB_MAX_NM      max query radius in nautical miles (default 250)
    FLIGHT_ADSB_DEFAULT_LAT fallback center lat when no bbox is given (default 39.5)
    FLIGHT_ADSB_DEFAULT_LON fallback center lon when no bbox is given (default -98.35)
"""

from __future__ import annotations

import http.server
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CREDS_PATH = os.path.expanduser("~/.nemoclaw/credentials.json")

OPENSKY_API_BASE = "https://opensky-network.org"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)

ROUTES = {
    # The sandbox sends requests rooted at these prefixes (no leading host).
    # We forward them to the matching OpenSky upstream path verbatim, with
    # query string preserved.
    "/api/states/all":       OPENSKY_API_BASE + "/api/states/all",
    "/api/flights/aircraft": OPENSKY_API_BASE + "/api/flights/aircraft",
    "/api/tracks/all":       OPENSKY_API_BASE + "/api/tracks/all",
}

# ── Data-source configuration ───────────────────────────────────────────────

# Optional outbound proxy for OpenSky ONLY. OpenSky blackholes cloud/
# datacenter IP ranges (AWS/GCP/Azure) at the TCP layer — the SYN is
# dropped *before* TLS, so the connection never opens and OpenSky never
# sees your OAuth2 credentials. No amount of authentication fixes that:
# the block is on the source IP, applied pre-auth. The only way to reach
# OpenSky from a cloud VM is to egress through a NON-datacenter hop.
#
# Set OPENSKY_EGRESS_PROXY to route just the OpenSky token-mint + API
# calls (NOT the community feeds, which work fine directly) through such
# a hop while keeping the credentials on the host. Examples:
#     http://127.0.0.1:8080        (Squid/tinyproxy/Cloudflare-WARP-proxy)
#     http://user:pass@host:port   (authenticated forward proxy)
#     socks5h://127.0.0.1:1080     (e.g. `ssh -D 1080 you@home-box`; needs PySocks)
# For socks5h:// the DNS is resolved at the proxy exit (recommended so
# opensky-network.org resolves from the non-cloud side). Leave unset to
# hit OpenSky directly (the default, correct for non-cloud hosts).
EGRESS_PROXY = os.getenv("OPENSKY_EGRESS_PROXY", "").strip()

RAW_SOURCE = (os.getenv("FLIGHT_ADSB_SOURCE", "community") or "community").strip().lower()
MAX_RADIUS_NM = float(os.getenv("FLIGHT_ADSB_MAX_NM", "250") or "250")
DEFAULT_LAT = float(os.getenv("FLIGHT_ADSB_DEFAULT_LAT", "39.5") or "39.5")
DEFAULT_LON = float(os.getenv("FLIGHT_ADSB_DEFAULT_LON", "-98.35") or "-98.35")

# Community ADS-B aggregators. All speak the readsb/tar1090 "aircraft.json"
# dialect; they differ only in URL shape and the top-level array key.
_PROVIDER_TABLE = {
    "adsb.fi": {
        "url": "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}",
        "key": "aircraft",
    },
    "airplanes.live": {
        "url": "https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
        "key": "ac",
    },
    "adsb.lol": {
        "url": "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}",
        "key": "ac",
    },
}

_ALL_PROVIDERS = ["adsb.fi", "airplanes.live", "adsb.lol"]
_PROVIDERS_ENV = [
    p.strip() for p in os.getenv("FLIGHT_ADSB_PROVIDERS", "").split(",")
    if p.strip() in _PROVIDER_TABLE
]

# FLIGHT_ADSB_SOURCE is a single, friendly selector:
#   "opensky"                         -> legacy OpenSky OAuth2 passthrough
#   "adsb.fi"|"airplanes.live"|"adsb.lol" -> that ONE community vendor
#   "community" | "auto" | anything else  -> community with fallback across
#                                            FLIGHT_ADSB_PROVIDERS (or all 3)
if RAW_SOURCE == "opensky":
    MODE = "opensky"
    PROVIDERS: list[str] = []
elif RAW_SOURCE in _PROVIDER_TABLE:
    MODE = "community"
    PROVIDERS = [RAW_SOURCE]
else:
    MODE = "community"
    PROVIDERS = _PROVIDERS_ENV or list(_ALL_PROVIDERS)

# What each source can actually answer — surfaced on GET / so a user can
# see, at a glance, what a feed supports.
CAPABILITIES = {
    "opensky": {
        "live_positions": True,
        "prebuilt_track_history": True,   # /tracks/all — where a plane has been
        "origin_destination": True,       # /flights/aircraft (icao24-keyed)
        "needs_credentials": True,
        "works_from_cloud_ip": False,     # blackholes AWS/GCP/Azure ranges
    },
    "community": {
        "live_positions": True,
        "prebuilt_track_history": False,  # no per-aircraft historical track API
        "origin_destination": "via callsign route lookup (adsbdb), not icao24",
        "needs_credentials": False,
        "works_from_cloud_ip": True,
    },
}

USER_AGENT = "FlightOps-NemoClaw-demo/1.0 (+https://github.com/brevdev/nemoclaw-demos)"

# Negative cache: once OpenSky is found unreachable (blackholed cloud IP),
# skip the slow probe on subsequent polls and serve community directly for
# this many seconds. Keeps opensky-mode refreshes snappy after the first
# fallback instead of eating a ~14s timeout every cycle.
_OPENSKY_DOWN_SECS = 120.0
_opensky_down_until = 0.0

# Unit conversions to OpenSky's SI state vector.
_FT_TO_M = 0.3048
_KT_TO_MS = 0.514444
_FTMIN_TO_MS = 0.3048 / 60.0


# ── Credential loading (opensky mode only) ──────────────────────────────────


def _load_creds() -> tuple[str, str]:
    """Return (client_id, client_secret) from credentials.json.

    Returns ("", "") if the file is missing or the keys aren't set —
    the caller treats that as "no auth available" and forwards
    anonymously, which OpenSky still answers (just at the lower tier).
    """
    try:
        with open(CREDS_PATH) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ("", "")
    return (
        (d.get("OPENSKY_CLIENT_ID") or "").strip(),
        (d.get("OPENSKY_CLIENT_SECRET") or "").strip(),
    )


# ── Token cache (opensky mode only) ─────────────────────────────────────────


class TokenCache:
    """Thread-safe OAuth2 token cache with credential-change detection."""

    LEAD_SECONDS = 60  # refresh this many seconds before expiry

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._fingerprint: str = ""  # client_id+secret hash, for change detection

    @staticmethod
    def _fp(cid: str, secret: str) -> str:
        return f"{len(cid)}:{cid[:4]}:{len(secret)}:{secret[-4:]}"

    def get(self) -> str | None:
        cid, secret = _load_creds()
        if not cid or not secret:
            return None
        fp = self._fp(cid, secret)
        with self._lock:
            if (
                self._token
                and self._fingerprint == fp
                and time.time() < self._expires_at - self.LEAD_SECONDS
            ):
                return self._token
            token, ttl = self._mint(cid, secret)
            if token is None:
                return None
            self._token = token
            self._expires_at = time.time() + ttl
            self._fingerprint = fp
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token. Called after a 401 from upstream."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    @staticmethod
    def _mint(cid: str, secret: str) -> tuple[str | None, float]:
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret,
            }
        ).encode()
        req = urllib.request.Request(
            OPENSKY_TOKEN_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with _OPENSKY_OPENER.open(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            sys.stderr.write(
                f"[opensky-proxy] token mint failed: {e.code} {e.reason}\n"
            )
            return (None, 0.0)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"[opensky-proxy] token mint error: {e}\n")
            return (None, 0.0)
        token = payload.get("access_token")
        ttl = float(payload.get("expires_in", 1800))
        return (token, ttl)


_tokens = TokenCache()


# ── Community ADS-B source ──────────────────────────────────────────────────


def _bbox_to_circle(qs: dict[str, list[str]]) -> tuple[float, float, int]:
    """Map an OpenSky bbox query (lamin/lamax/lomin/lomax) to (lat, lon, nm).

    The community endpoints are radius queries, so we take the bbox centre
    and a radius that covers the far corner, clamped to the provider max.
    Falls back to a configured default centre + max radius when no bbox is
    supplied (the UI always sends one, so this is just belt-and-braces).
    """
    def _get(name: str) -> float | None:
        v = qs.get(name, [None])[0]
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lamin, lamax = _get("lamin"), _get("lamax")
    lomin, lomax = _get("lomin"), _get("lomax")

    if None in (lamin, lamax, lomin, lomax):
        return (DEFAULT_LAT, DEFAULT_LON, int(MAX_RADIUS_NM))

    clat = (lamin + lamax) / 2.0
    clon = (lomin + lomax) / 2.0
    # Radius = centre → NE corner (nautical miles), clamped.
    radius_nm = _haversine_nm(clat, clon, lamax, lomax)
    radius_nm = max(1.0, min(MAX_RADIUS_NM, radius_nm))
    return (clat, clon, int(round(radius_nm)))


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065  # Earth radius in nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(min(1.0, math.sqrt(a)))


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _translate_aircraft(ac: dict, now: float) -> list | None:
    """Convert one readsb/tar1090 aircraft record into an OpenSky state row.

    OpenSky positional schema (indices used by the sandbox decoder):
      0 icao24, 1 callsign, 2 origin_country, 3 time_position,
      4 last_contact, 5 lon, 6 lat, 7 baro_alt(m), 8 on_ground,
      9 velocity(m/s), 10 true_track, 11 vertical_rate(m/s),
      12 sensors, 13 geo_alt(m), 14 squawk, 15 spi, 16 position_source
    """
    hex_id = str(ac.get("hex") or "").strip().lstrip("~").lower()
    if len(hex_id) != 6:
        return None
    lat, lon = _num(ac.get("lat")), _num(ac.get("lon"))
    if lat is None or lon is None:
        return None

    callsign = (ac.get("flight") or "").strip() or None

    alt_baro_raw = ac.get("alt_baro")
    on_ground = (isinstance(alt_baro_raw, str) and alt_baro_raw.lower() == "ground")
    baro_alt_m = None if on_ground else (
        _num(alt_baro_raw) * _FT_TO_M if _num(alt_baro_raw) is not None else None
    )
    geom_alt = _num(ac.get("alt_geom"))
    geo_alt_m = geom_alt * _FT_TO_M if geom_alt is not None else None

    gs = _num(ac.get("gs"))
    vel_ms = gs * _KT_TO_MS if gs is not None else None

    track = ac.get("track")
    if not isinstance(track, (int, float)):
        for alt_key in ("true_heading", "mag_heading", "nav_heading"):
            if isinstance(ac.get(alt_key), (int, float)):
                track = ac[alt_key]
                break
    track = track if isinstance(track, (int, float)) else None

    rate = ac.get("baro_rate")
    if not isinstance(rate, (int, float)):
        rate = ac.get("geom_rate")
    vrate_ms = rate * _FTMIN_TO_MS if isinstance(rate, (int, float)) else None

    squawk = ac.get("squawk")
    squawk = str(squawk) if squawk is not None else None

    seen = _num(ac.get("seen"))
    last_contact = int(now - seen) if seen is not None else int(now)

    return [
        hex_id,                       # 0 icao24
        callsign,                     # 1 callsign
        (ac.get("flag") or ""),       # 2 origin_country (aggregators don't provide; blank)
        last_contact,                 # 3 time_position
        last_contact,                 # 4 last_contact
        lon,                          # 5 longitude
        lat,                          # 6 latitude
        baro_alt_m,                   # 7 baro_altitude (m)
        on_ground,                    # 8 on_ground
        vel_ms,                       # 9 velocity (m/s)
        track,                        # 10 true_track
        vrate_ms,                     # 11 vertical_rate (m/s)
        None,                         # 12 sensors
        geo_alt_m,                    # 13 geo_altitude (m)
        squawk,                       # 14 squawk
        False,                        # 15 spi
        0,                            # 16 position_source
    ]


def _fetch_provider(name: str, lat: float, lon: float, nm: int) -> list | None:
    """Fetch raw aircraft list from one provider, or None on failure."""
    spec = _PROVIDER_TABLE[name]
    url = spec["url"].format(lat=f"{lat:.5f}", lon=f"{lon:.5f}", nm=nm)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    ac = payload.get(spec["key"])
    return ac if isinstance(ac, list) else []


def _effective_source(qs: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Resolve the source for THIS request.

    A `provider` query param (set by the UI's source selector, forwarded by
    the sandbox app) overrides the process default for a single request:
      opensky                          -> ("opensky", [])
      adsb.fi|airplanes.live|adsb.lol  -> ("community", [that one])
      community|auto|all               -> ("community", all three)
      (absent/unknown)                 -> the process default (MODE/PROVIDERS)
    """
    p = (qs.get("provider", [None])[0] or "").strip().lower()
    if not p:
        return (MODE, PROVIDERS if MODE == "community" else [])
    if p == "opensky":
        return ("opensky", [])
    if p in _PROVIDER_TABLE:
        return ("community", [p])
    if p in ("community", "auto", "all"):
        return ("community", list(_ALL_PROVIDERS))
    return (MODE, PROVIDERS if MODE == "community" else [])


def _serve_states_community(
    handler: "Handler",
    qs: dict[str, list[str]],
    providers: list[str],
    requested: str = "community",
    fallback: bool = False,
) -> None:
    """Answer /api/states/all from the community aggregators.

    `requested` is the source the caller asked for (e.g. "opensky" when we
    are here because OpenSky was unreachable and we fell back). `fallback`
    marks that this community answer is standing in for a failed OpenSky
    request, so the app/UI can say "OpenSky unavailable — community feed".
    """
    lat, lon, nm = _bbox_to_circle(qs)
    now = time.time()

    meta_headers = {
        "X-Flightops-Source": "community",
        "X-Flightops-Requested": requested,
        "X-Flightops-Fallback": "1" if fallback else "0",
    }

    last_err: Exception | None = None
    for name in providers:
        try:
            raw = _fetch_provider(name, lat, lon, nm)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError, ValueError) as e:
            last_err = e
            sys.stderr.write(f"[opensky-proxy] provider {name} failed: {e}\n")
            continue

        states = []
        for ac in (raw or []):
            if isinstance(ac, dict):
                row = _translate_aircraft(ac, now)
                if row is not None:
                    states.append(row)
        sys.stderr.write(
            f"[opensky-proxy] states via {name}: {len(states)} aircraft "
            f"({lat:.2f},{lon:.2f} r={nm}nm)"
            f"{' [opensky-fallback]' if fallback else ''}\n"
        )
        _send_json(
            handler, 200,
            {
                "time": int(now), "states": states,
                "source": "community", "requested": requested,
                "fallback": bool(fallback), "provider": name,
            },
            extra_headers={**meta_headers, "X-Flightops-Provider": name},
        )
        return

    _send_json(
        handler, 502,
        {
            "error": f"all ADS-B providers failed; last error: {last_err}",
            "states": [], "source": "community", "requested": requested,
            "fallback": bool(fallback),
        },
        extra_headers=meta_headers,
    )


def _fetch_opensky_states(query: str) -> tuple[dict | None, str]:
    """Fetch /api/states/all from OpenSky. Returns (payload, err_message).

    On success payload is the parsed JSON dict; on any failure it is None and
    err_message explains why (used to trigger the community fallback). A short
    timeout is used deliberately so a blackholed cloud IP falls back fast
    rather than leaving the map blank for many seconds.
    """
    target = ROUTES["/api/states/all"]
    url = f"{target}?{query}" if query else target
    for attempt in (1, 2):
        token = _tokens.get()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return (json.loads(resp.read().decode("utf-8")), "")
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1 and token:
                _tokens.invalidate()
                continue
            return (None, f"opensky HTTP {e.code} {e.reason}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                ValueError) as e:
            return (None, f"opensky unreachable: {e}")
    return (None, "opensky retries exhausted")


# ── HTTP helpers ────────────────────────────────────────────────────────────


def _build_opener() -> urllib.request.OpenerDirector:
    """Build the urllib opener used for OpenSky calls.

    Without OPENSKY_EGRESS_PROXY this is a plain opener (direct egress).
    With it, all OpenSky traffic is routed through the configured proxy so
    a cloud VM can reach OpenSky via a non-datacenter hop. http(s):// proxies
    need no extra dependencies; socks5(h):// requires PySocks (`pip install
    PySocks`) and degrades gracefully to direct (→ community fallback) if it
    isn't installed, with a one-line warning.
    """
    if not EGRESS_PROXY:
        return urllib.request.build_opener()
    if EGRESS_PROXY.lower().startswith("socks"):
        try:
            import socks  # type: ignore  # from PySocks
            from sockshandler import SocksiPyHandler  # type: ignore
        except ImportError:
            sys.stderr.write(
                "[opensky-proxy] OPENSKY_EGRESS_PROXY is a SOCKS URL but "
                "PySocks is not installed (`pip install PySocks`); falling "
                "back to DIRECT egress — OpenSky will likely be unreachable "
                "and community fallback will serve instead.\n"
            )
            return urllib.request.build_opener()
        parsed = urllib.parse.urlparse(EGRESS_PROXY)
        # socks5h:// → resolve DNS at the proxy exit (rdns=True); socks5:// → local.
        rdns = parsed.scheme.lower() in ("socks5h", "socks4a")
        stype = socks.SOCKS4 if parsed.scheme.lower().startswith("socks4") else socks.SOCKS5
        return urllib.request.build_opener(
            SocksiPyHandler(
                stype, parsed.hostname, parsed.port or 1080, rdns,
                parsed.username, parsed.password,
            )
        )
    # http:// or https:// forward proxy (CONNECT tunnelling for https targets).
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": EGRESS_PROXY, "https": EGRESS_PROXY})
    )


# Built once at import; OpenSky call sites use this instead of the module-level
# urllib.request.urlopen so the egress proxy (if any) is applied. Community
# aggregator calls deliberately keep using plain urlopen — they work directly
# and there's no reason to tunnel them.
_OPENSKY_OPENER = _build_opener()


def _resolve_target(path: str) -> str | None:
    """Match the request path (sans query) against the OpenSky route table."""
    parsed = urllib.parse.urlparse(path)
    target = ROUTES.get(parsed.path)
    if target is None:
        return None
    if parsed.query:
        return f"{target}?{parsed.query}"
    return target


def _send_json(
    handler: http.server.BaseHTTPRequestHandler,
    code: int,
    obj: dict,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _passthrough(
    handler: http.server.BaseHTTPRequestHandler,
    target: str,
) -> None:
    """Forward the request to OpenSky, optionally with Bearer auth (opensky mode)."""
    for attempt in (1, 2):
        token = _tokens.get()
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(target, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read()
                handler.send_response(resp.status)
                ct = resp.headers.get("Content-Type", "application/json")
                handler.send_header("Content-Type", ct)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
                return
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1 and token:
                _tokens.invalidate()
                continue
            body = e.read() or b""
            handler.send_response(e.code)
            ct = e.headers.get("Content-Type", "application/json")
            handler.send_header("Content-Type", ct)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        except (urllib.error.URLError, OSError) as e:
            _send_json(handler, 502, {"error": f"upstream unreachable: {e}"})
            return

    _send_json(handler, 502, {"error": "exhausted retries against opensky"})


class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        # Health probe — used by install.sh + systemd unit smoke tests.
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        # Diagnostic root page — handy when poking the daemon manually.
        if self.path == "/" or self.path == "":
            cid, secret = _load_creds()
            _send_json(self, 200, {
                "service": "opensky-proxy",
                "source_requested": RAW_SOURCE,
                "mode": MODE,
                "active_providers": PROVIDERS if MODE == "community" else ["opensky"],
                "capabilities": CAPABILITIES[MODE],
                "routes": list(ROUTES.keys()),
                "creds_loaded": bool(cid and secret),
                "creds_path": CREDS_PATH,
            })
            return

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        eff_mode, eff_providers = _effective_source(qs)

        if parsed.path == "/api/states/all":
            if eff_mode == "opensky":
                global _opensky_down_until
                now = time.time()
                # Skip the slow OpenSky probe if we recently found it down.
                if now < _opensky_down_until:
                    _serve_states_community(
                        self, qs, list(_ALL_PROVIDERS),
                        requested="opensky", fallback=True,
                    )
                    return
                data, err = _fetch_opensky_states(parsed.query)
                if data is not None:
                    _opensky_down_until = 0.0
                    data.setdefault("source", "opensky")
                    data.setdefault("requested", "opensky")
                    data.setdefault("fallback", False)
                    _send_json(self, 200, data, extra_headers={
                        "X-Flightops-Source": "opensky",
                        "X-Flightops-Requested": "opensky",
                        "X-Flightops-Fallback": "0",
                    })
                    return
                # OpenSky failed/blackholed — remember it (negative cache) and
                # fall back to community so the map keeps showing planes.
                _opensky_down_until = now + _OPENSKY_DOWN_SECS
                sys.stderr.write(
                    f"[opensky-proxy] opensky states failed ({err}); serving "
                    f"community for {int(_OPENSKY_DOWN_SECS)}s\n"
                )
                _serve_states_community(
                    self, qs, list(_ALL_PROVIDERS),
                    requested="opensky", fallback=True,
                )
            else:
                _serve_states_community(self, qs, eff_providers)
            return

        if parsed.path in ("/api/flights/aircraft", "/api/tracks/all"):
            if eff_mode == "opensky":
                self._passthrough_or_404(parsed.path, parsed.query)
                return
            # Aggregators don't expose historical flights/tracks; the
            # sandbox treats 404 as "no data" and degrades gracefully.
            _send_json(self, 404, {
                "error": "history not available from community ADS-B source",
                "mode": eff_mode,
                "active_providers": eff_providers,
            })
            return

        _send_json(self, 404, {
            "error": "Unknown route",
            "routes": list(ROUTES.keys()) + ["/health"],
            "received_path": self.path,
        })

    def _passthrough_or_404(self, base_path: str, query: str) -> None:
        target = ROUTES.get(base_path)
        if target is None:
            _send_json(self, 404, {"error": "Unknown route", "received_path": self.path})
            return
        # Forward the original query (lamin/lamax/…); OpenSky ignores the
        # extra `provider` param harmlessly.
        _passthrough(self, f"{target}?{query}" if query else target)

    def do_HEAD(self) -> None:
        # Some health probes use HEAD; treat it like GET /health.
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            return
        self.send_response(405)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {self.address_string()} "
            f"{fmt % args}\n"
        )


# ── Entrypoint ──────────────────────────────────────────────────────────────


def main() -> None:
    port = 9202
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            port = int(args[i + 1])

    if MODE == "community":
        sys.stderr.write(
            f"[opensky-proxy] listening on 0.0.0.0:{port} "
            f"(source={RAW_SOURCE}, mode=community, providers={','.join(PROVIDERS)})\n"
            f"  /api/states/all       -> community ADS-B (translated to OpenSky schema)\n"
            f"  /api/flights/aircraft -> 404 (history unavailable from community feeds)\n"
            f"  /api/tracks/all       -> 404 (history unavailable from community feeds)\n"
        )
    else:
        cid, secret = _load_creds()
        if not cid or not secret:
            sys.stderr.write(
                f"[opensky-proxy] WARNING: no OPENSKY_CLIENT_ID/SECRET in "
                f"{CREDS_PATH}; running anonymously (~400 credits/day)\n"
            )
        sys.stderr.write(
            f"[opensky-proxy] listening on 0.0.0.0:{port} "
            f"(source=opensky, creds: {CREDS_PATH})\n"
            f"  /api/states/all       -> {OPENSKY_API_BASE}/api/states/all\n"
            f"  /api/flights/aircraft -> {OPENSKY_API_BASE}/api/flights/aircraft\n"
            f"  /api/tracks/all       -> {OPENSKY_API_BASE}/api/tracks/all\n"
        )

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[opensky-proxy] stopped.\n")
        server.shutdown()


if __name__ == "__main__":
    main()
