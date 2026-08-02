"""What `station_web.py` must not stop doing.

Every behaviour pinned here fails *silently* when it breaks, which is why the
file's own docstring calls `_NoRedirect` "the one piece of this file that is easy
to delete by accident". Delete it and `/s/{short_id}` still answers 200, the page
still renders, and the only symptom is that the address bar keeps saying `/s/...`
while the SPA never routes — a tag tap that quietly goes nowhere.

Standard library only, no fixtures, no network beyond loopback: this runs
anywhere the station runs.

    uv run --no-project --python 3.12 -- python -m pytest deploy/station/test_station_web.py
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from station_web import StationHandler


class _Upstream(BaseHTTPRequestHandler):
    """A stand-in API. Answers the few shapes the proxy has to carry."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/s/"):
            # Exactly what the backend does: a *relative* Location the browser
            # has to resolve against the origin that served it.
            self.send_response(302)
            self.send_header("location", "/scan?unknown=ZZZZ")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if self.path == "/api/boom":
            body = b'{"detail": "upstream said no"}'
            self.send_response(503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture(scope="module")
def station(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    dist = tmp_path_factory.mktemp("dist")
    (dist / "index.html").write_text("<!doctype html><title>almagest</title>")
    (dist / "app.js").write_text("console.log(1)")
    # A sibling directory whose name shares the dist prefix — the exact shape a
    # `startswith` containment check waves through and `is_relative_to` does not.
    outside = dist.parent / (dist.name + ".bak")
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("not servable")

    upstream, upstream_port = _serve(_Upstream)
    StationHandler.dist = dist.resolve()
    StationHandler.api = f"http://127.0.0.1:{upstream_port}"
    server, port = _serve(StationHandler)
    yield port
    server.shutdown()
    upstream.shutdown()


def request(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_a_tag_tap_is_relayed_as_a_redirect_not_followed(station: int) -> None:
    """The control for `_NoRedirect`, and the reason this file exists.

    Remove that handler and this becomes `200` with the target page's HTML served
    under the tag's own URL — no exception, no log line, and a tag that appears to
    do nothing.
    """
    status, headers, _ = request(station, "GET", "/s/4K7T92M8")
    assert status == 302
    # Relative, and passed through byte for byte: the browser resolves it against
    # the station's origin, which is the whole reason one origin is required.
    assert headers["location"] == "/scan?unknown=ZZZZ"


def test_head_carries_no_body(station: int) -> None:
    """A HEAD with a body corrupts the *next* response on a keep-alive connection,
    so the visible symptom is a garbage status line on an unrelated request."""
    status, headers, body = request(station, "HEAD", "/index.html")
    assert status == 200
    assert int(headers["content-length"]) > 0
    assert body == b""


def test_a_pipelined_get_after_a_head_still_parses(station: int) -> None:
    """The failure the guard actually prevents, reproduced end to end."""
    conn = http.client.HTTPConnection("127.0.0.1", station, timeout=10)
    try:
        conn.request("HEAD", "/index.html")
        conn.getresponse().read()
        conn.request("GET", "/index.html")
        second = conn.getresponse()
        assert second.status == 200
        assert b"almagest" in second.read()
    finally:
        conn.close()


def test_an_unknown_path_falls_back_to_the_spa(station: int) -> None:
    status, _, body = request(station, "GET", "/locations/4")
    assert status == 200
    assert b"<!doctype html>" in body.lower()


def test_traversal_cannot_escape_the_bundle(station: int) -> None:
    status, _, body = request(station, "GET", "/../../../../etc/passwd")
    assert status == 200
    assert b"root:" not in body
    assert b"almagest" in body


def test_a_sibling_directory_sharing_the_prefix_is_not_servable(station: int) -> None:
    """`dist.bak/` and `dist-old/` sit next to `dist/` after any rsync, and a
    `startswith` containment check serves straight out of them."""
    status, _, body = request(station, "GET", "/../dist.bak/secret.txt")
    assert status == 200
    assert b"not servable" not in body


def test_an_upstream_error_is_relayed_with_its_body(station: int) -> None:
    """A 503 from the API must reach the PWA as a 503 with its detail, not as a
    proxy-invented failure — the PWA renders `detail` and would otherwise show
    nothing useful."""
    status, _, body = request(station, "GET", "/api/boom")
    assert status == 503
    assert json.loads(body)["detail"] == "upstream said no"


def test_a_wedged_upstream_answers_504_rather_than_hanging(
    tmp_path: Path,
) -> None:
    """No timeout means a kiosk tab that spins for ever and a thread leaked per
    retap. Distinct from 502, because "not running" and "running and stuck" send
    an operator to different places."""

    class _Hangs(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            threading.Event().wait(30)

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    hung, hung_port = _serve(_Hangs)

    class Impatient(StationHandler):
        upstream_timeout_s = 0.5

    Impatient.dist = dist.resolve()
    Impatient.api = f"http://127.0.0.1:{hung_port}"
    server, port = _serve(Impatient)
    try:
        status, _, body = request(port, "GET", "/api/slow")
        assert status == 504
        assert "did not answer" in json.loads(body)["detail"]
    finally:
        server.shutdown()
        hung.shutdown()


def test_an_api_that_is_not_running_answers_502_not_504(tmp_path: Path) -> None:
    """The distinction is the point, and only half of it had a test.

    "Not running" and "running and stuck" send an operator to different places:
    one starts a service, the other looks at what is holding the SQLite writer.
    A proxy that reported the same code for both would make the 504 above
    meaningless.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")

    # A port with nothing on it. Bound and closed so the number is certainly free.
    with __import__("socket").socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    class Orphaned(StationHandler):
        pass

    Orphaned.dist = dist.resolve()
    Orphaned.api = f"http://127.0.0.1:{dead_port}"
    server, port = _serve(Orphaned)
    try:
        status, _, body = request(port, "GET", "/api/anything")
        assert status == 502
        assert "not answering" in json.loads(body)["detail"]
    finally:
        server.shutdown()
