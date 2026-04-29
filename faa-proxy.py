#!/usr/bin/env python3
"""
faa-proxy.py — host-side forwarder for public FAA / NWS feeds.

Why this exists:
  Some `.gov` weather + flow-control feeds (notably nasstatus.faa.gov and
  aviationweather.gov) refuse requests from the cloud ASN that the
  openshell sandbox egresses through, returning 403 Forbidden. The same
  feeds work fine from the host VM. So we forward sandbox requests
  through the host the same way `opensky-proxy.py` does, except these
  feeds are *public* — there's no token to mint, no credential to hide.
  The proxy is just an IP-rewrap.

URL shape (sandbox sees this):
    GET http://<HOST_IP>:9203/nas/api/airport-events
        → https://nasstatus.faa.gov/api/airport-events
    GET http://<HOST_IP>:9203/awc/api/data/metar?bbox=…
        → https://aviationweather.gov/api/data/metar?bbox=…
    GET http://<HOST_IP>:9203/health
        (diagnostic — no upstream fetch)

Adding a new upstream is a one-line addition to UPSTREAMS below.

Run with:
    python3 faa-proxy.py [--host 0.0.0.0] [--port 9203]

install.sh launches this under nohup at install time. Logs:
    /tmp/faa-proxy.log
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as ue
from urllib import request as ur
from urllib.parse import urlsplit, urlunsplit

# Each entry: route prefix → (upstream base URL, browser-like UA to send).
# A browser UA isn't strictly required — these feeds work with curl
# defaults — but FAA's WAF tends to be friendlier to recognised UAs and
# it costs us nothing.
UPSTREAMS: dict[str, tuple[str, str]] = {
    "nas": (
        "https://nasstatus.faa.gov",
        "FlightOps-NemoClaw/1.0 (+https://github.com/tklawa-nvidia/flight-tracking-integration)",
    ),
    "awc": (
        "https://aviationweather.gov",
        "FlightOps-NemoClaw/1.0 (+https://github.com/tklawa-nvidia/flight-tracking-integration)",
    ),
}

PROXY_TIMEOUT = 15.0
SERVICE_NAME = "faa-proxy"


def _resolve(path: str) -> tuple[str, str] | None:
    """Map an incoming request path like `/nas/api/airport-events` to the
    upstream `https://nasstatus.faa.gov/api/airport-events`. Returns
    `(upstream_url, user_agent)` or None if the prefix isn't allowed."""
    if not path or not path.startswith("/"):
        return None
    parts = path.lstrip("/").split("/", 1)
    if not parts:
        return None
    key = parts[0]
    if key not in UPSTREAMS:
        return None
    base, ua = UPSTREAMS[key]
    suffix = parts[1] if len(parts) > 1 else ""
    sp = urlsplit(base)
    upstream = urlunsplit((sp.scheme, sp.netloc, "/" + suffix, "", ""))
    return upstream, ua


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVICE_NAME}/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{SERVICE_NAME}] {self.address_string()} {fmt % args}\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/health"):
            self._send_json(200, {
                "ok": True,
                "service": SERVICE_NAME,
                "upstreams": sorted(UPSTREAMS.keys()),
            })
            return

        sp = urlsplit(self.path)
        resolved = _resolve(sp.path)
        if resolved is None:
            self._send_json(
                404,
                {
                    "ok": False,
                    "error": "unknown upstream",
                    "valid_prefixes": sorted(f"/{k}/" for k in UPSTREAMS),
                },
            )
            return
        upstream_base_url, user_agent = resolved
        # Reattach the original query string verbatim.
        upstream_url = upstream_base_url
        if sp.query:
            upstream_url = f"{upstream_url}?{sp.query}"

        req = ur.Request(
            upstream_url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
            },
            method="GET",
        )
        t0 = time.time()
        try:
            with ur.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
                body = resp.read()
                status = resp.status
                content_type = resp.headers.get("Content-Type", "application/json")
        except ue.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            sys.stderr.write(
                f"[{SERVICE_NAME}] upstream {upstream_url} HTTP {e.code} ({time.time()-t0:.2f}s)\n"
            )
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Proxy-Upstream", upstream_url)
            self.end_headers()
            self.wfile.write(body)
            return
        except (ue.URLError, TimeoutError) as e:
            sys.stderr.write(f"[{SERVICE_NAME}] upstream error {upstream_url}: {e}\n")
            self._send_json(502, {
                "ok": False,
                "error": f"upstream unreachable: {e}",
                "upstream": upstream_url,
            })
            return

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Proxy-Upstream", upstream_url)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.getenv("FAA_PROXY_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.getenv("FAA_PROXY_PORT", "9203")))
    args = p.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        f"[{SERVICE_NAME}] listening on {args.host}:{args.port} "
        f"upstreams={sorted(UPSTREAMS.keys())}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write(f"\n[{SERVICE_NAME}] stopped.\n")


if __name__ == "__main__":
    main()
