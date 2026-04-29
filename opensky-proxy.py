#!/usr/bin/env python3
"""Host-side API proxy for FlightOps' OpenSky integration.

Runs on the host (the brev VM, outside the sandbox), reads OpenSky
OAuth2 credentials from ~/.nemoclaw/credentials.json, and proxies the
sandbox's anonymous-looking calls to opensky-network.org with the
Bearer token injected. Mirrors the Planet-integration tier-1 pattern:
keys live exclusively on the host, the sandbox only knows the proxy URL.

Token mint flow: the proxy keeps an in-memory cache of the access_token
plus its expires_at, refreshing 60s before expiry. On a 401 from
upstream we evict the cache and retry once, which covers cases where
OpenSky rotates signing keys mid-token-lifetime. The credential
fingerprint is stored alongside the cached token so editing
credentials.json invalidates the cache without a restart.

Routes:
    GET  /api/states/all          -> https://opensky-network.org/api/states/all
    GET  /api/flights/aircraft    -> https://opensky-network.org/api/flights/aircraft
    GET  /api/tracks/all          -> https://opensky-network.org/api/tracks/all
    GET  /health                  -> 200 "ok"
    GET  /                        -> 200 with route map (handy for debugging)

Usage:
    python3 opensky-proxy.py [--port 9202]
"""

from __future__ import annotations

import http.server
import json
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


# ── Credential loading ──────────────────────────────────────────────────────


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


# ── Token cache ─────────────────────────────────────────────────────────────


class TokenCache:
    """Thread-safe OAuth2 token cache with credential-change detection.

    Single mutex protects the cached fields; mint operations themselves
    happen inside the lock so we never run two simultaneous token mints
    against OpenSky's auth server. Token mints take ~200ms, so the
    contention window is negligible for our request rate.
    """

    LEAD_SECONDS = 60  # refresh this many seconds before expiry

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._fingerprint: str = ""  # client_id+secret hash, for change detection

    @staticmethod
    def _fp(cid: str, secret: str) -> str:
        # Cheap fingerprint — we only need to detect change, not be
        # cryptographically secure (the secret never leaves the host).
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
            with urllib.request.urlopen(req, timeout=15) as resp:
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


# ── HTTP handler ────────────────────────────────────────────────────────────


def _resolve_target(path: str) -> str | None:
    """Match the request path (sans query) against the route table.

    Query string is preserved when forwarding so callers' filters
    (icao24, lamin/lamax/lomin/lomax, begin/end, time) pass through
    unchanged.
    """
    parsed = urllib.parse.urlparse(path)
    base_path = parsed.path
    target = ROUTES.get(base_path)
    if target is None:
        return None
    if parsed.query:
        return f"{target}?{parsed.query}"
    return target


def _send_json(handler: http.server.BaseHTTPRequestHandler, code: int, obj: dict) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _passthrough(
    handler: http.server.BaseHTTPRequestHandler,
    target: str,
) -> None:
    """Forward the request to OpenSky, optionally with Bearer auth.

    Auth strategy: try with Bearer first if we have one. If upstream
    returns 401 we drop the cached token and retry once — OpenSky
    occasionally rotates signing keys mid-token-lifetime and our cache
    can outlive a key rotation.
    """
    for attempt in (1, 2):
        token = _tokens.get()
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(target, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
                handler.send_response(resp.status)
                ct = resp.headers.get("Content-Type", "application/json")
                handler.send_header("Content-Type", ct)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
                return
        except urllib.error.HTTPError as e:
            # Single retry on 401 — likely token rotation or expiry race.
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
                "routes": list(ROUTES.keys()),
                "creds_loaded": bool(cid and secret),
                "creds_path": CREDS_PATH,
            })
            return

        target = _resolve_target(self.path)
        if target is None:
            _send_json(self, 404, {
                "error": "Unknown route",
                "routes": list(ROUTES.keys()) + ["/health"],
                "received_path": self.path,
            })
            return
        _passthrough(self, target)

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
        # Keep the log line shape the same as planet-proxy: timestamp +
        # client IP + status. Default BaseHTTPRequestHandler logging
        # spews to stderr which is fine — install.sh redirects it to
        # /tmp/opensky-proxy.log.
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

    # Surface a friendly hint at startup if creds aren't present, but
    # don't refuse to start — anonymous forwarding still works at the
    # lower OpenSky tier and lets the rest of the stack come up cleanly.
    cid, secret = _load_creds()
    if not cid or not secret:
        sys.stderr.write(
            f"[opensky-proxy] WARNING: no OPENSKY_CLIENT_ID/SECRET in "
            f"{CREDS_PATH}; running anonymously (~400 credits/day)\n"
        )

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(
        f"[opensky-proxy] listening on 0.0.0.0:{port} "
        f"(creds: {CREDS_PATH})\n"
        f"  /api/states/all       -> {OPENSKY_API_BASE}/api/states/all\n"
        f"  /api/flights/aircraft -> {OPENSKY_API_BASE}/api/flights/aircraft\n"
        f"  /api/tracks/all       -> {OPENSKY_API_BASE}/api/tracks/all\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[opensky-proxy] stopped.\n")
        server.shutdown()


if __name__ == "__main__":
    main()
