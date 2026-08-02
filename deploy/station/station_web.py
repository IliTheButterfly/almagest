"""The station's one origin: the built PWA plus a pass-through to the API.

This is `frontend/nginx.conf` reduced to what a bench machine needs and written
in the standard library, because the station is a Jetson Nano running Ubuntu
18.04 with no root available and nginx is not on it. The routing is deliberately
the same as the nginx config's, for the reason that file states at length: the
PWA builds its client with `baseUrl: currentOrigin()`, and `/s/{short_id}` is
answered by the *backend* with a **relative** redirect. Serve the two on
different ports and a tag tap lands nowhere.

Three differences from the cluster's nginx, all of them because this listens on
loopback only:

- **No TLS.** `http://127.0.0.1` is a "potentially trustworthy origin" per the
  secure-context spec, so `getUserMedia` works without a certificate. That is
  the whole reason the kiosk points at loopback rather than at `almagest.lan`:
  the station gets a camera without the private CA (ADR 0001) being installed
  first. Web NFC still does not exist here — kiosk Chromium has no `NDEFReader`
  on any origin — which is exactly the gap the device bridge (ADR 0014) fills.
- **Redirects are passed through, never followed.** `urllib` follows them by
  default, which would turn a `/s/{short_id}` tap into this process fetching the
  part page and handing back its HTML under the tag's URL. The browser would
  never see the 302, the address bar would keep saying `/s/...`, and the SPA
  would never route. `_NoRedirect` below is what stops that, and it is the one
  piece of this file that is easy to delete by accident.
- **It binds 127.0.0.1 and nothing else.** Same rule as the bridge's
  `_refuse_a_non_loopback_bind`: a station that answers on the LAN is an
  unauthenticated copy of the inventory on a bench.

    python3 station_web.py --dist ../../frontend/dist --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Everything the backend owns. `/s/` is the tag payload's path and must be here;
# `/docs` and `/openapi.json` are convenience for someone debugging at the bench.
PROXY_PREFIXES = ("/api/", "/s/", "/openapi.json", "/docs", "/redoc")

CONTENT_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".ico": "image/x-icon",
    ".js": "text/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}

# Forwarded in both directions. Small and explicit rather than "everything minus
# a deny list": a hop-by-hop header copied through a proxy that does not honour
# it (`Transfer-Encoding`, `Connection`) is a hang that only shows up under load.
REQUEST_HEADERS = ("accept", "accept-language", "content-type", "idempotency-key", "if-none-match")
RESPONSE_HEADERS = ("content-type", "location", "etag", "cache-control", "content-disposition")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx back to the browser instead of chasing it.

    `/s/{short_id}` answers `302 Location: /scan?unknown=...` — relative, and
    resolved by whoever received it. That has to be the browser.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class StationHandler(BaseHTTPRequestHandler):
    dist: Path
    api: str
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # The station runs unattended under systemd; per-request logging would
        # fill the journal with the decode loop's polling and nothing else.
        pass

    # -- the API half ------------------------------------------------------

    def _proxy(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else None
        request = urllib.request.Request(self.api + self.path, data=body, method=self.command)
        for header in REQUEST_HEADERS:
            value = self.headers.get(header)
            if value is not None:
                request.add_header(header, value)

        try:
            with _opener.open(request) as upstream:
                self._relay(upstream.status, upstream.headers, upstream.read())
        except urllib.error.HTTPError as exc:
            # 3xx arrives here now that redirects are not followed, which is the
            # point: `exc.headers` carries the Location the browser needs.
            self._relay(exc.code, exc.headers, exc.read())
        except OSError as exc:
            # The API is not up yet, or has been restarted under us. 502 with a
            # readable body beats a connection reset the PWA reports as
            # "something went wrong".
            self._relay_bytes(
                502,
                "application/json",
                b'{"detail": "the station API is not answering: %s"}'
                % str(exc).encode("utf-8", "replace"),
            )

    def _relay(self, status: int, headers: object, payload: bytes) -> None:
        self.send_response(status)
        get = getattr(headers, "get", None)
        content_type = "application/json"
        for header in RESPONSE_HEADERS:
            value = get(header) if get is not None else None
            if value is None:
                continue
            if header == "content-type":
                content_type = value
                continue
            self.send_header(header, value)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _relay_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -- the PWA half ------------------------------------------------------

    def _static(self) -> None:
        path = self.path.partition("?")[0]
        target = (self.dist / path.lstrip("/")).resolve()
        # The containment check is not paranoia about a hostile LAN — there is no
        # LAN here — it is about `..` in a URL turning a typo into `/etc/passwd`.
        inside = target.is_file() and str(target).startswith(str(self.dist))
        if not inside:
            # SPA fallback: `/locations/4` is a client route, not a file.
            target = self.dist / "index.html"
        if not target.is_file():
            self._relay_bytes(
                503,
                "text/plain",
                b"The PWA has not been built into frontend/dist on this machine.\n",
            )
            return
        data = target.read_bytes()
        self._relay_bytes(
            200, CONTENT_TYPES.get(target.suffix, "application/octet-stream"), data
        )

    def _dispatch(self) -> None:
        if self.path.startswith(PROXY_PREFIXES):
            self._proxy()
        elif self.command in ("GET", "HEAD"):
            self._static()
        else:
            self.send_error(405)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path, help="the built PWA")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="the station's API")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    StationHandler.dist = args.dist.resolve()
    StationHandler.api = args.api.rstrip("/")
    # Loopback, never 0.0.0.0. See the module docstring.
    ThreadingHTTPServer(("127.0.0.1", args.port), StationHandler).serve_forever()


if __name__ == "__main__":
    main()
