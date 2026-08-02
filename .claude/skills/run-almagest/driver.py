#!/usr/bin/env python3
"""Launch and drive Almagest — API, PWA and bench station — from one command.

Standard library only, and deliberately runnable with the system `python3`: the
three uv projects each have their own venv and none of them is the right place
for a thing that has to start all of them.

    driver.py up                     # migrate + seed a throwaway DB, serve API + PWA
    driver.py api GET /api/system/health
    driver.py shot /search out.png
    driver.py station                # run the bench station and print its event stream
    driver.py smoke                  # all of the above, then tear down
    driver.py down

Everything runs against an **isolated database** under the state directory, on
ports that are not 8000/5173 — those belong to the maintainer's own dev loop on
the main checkout, and writing through them would land ledger rows in the live
database. See SKILL.md.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = Path(
    os.environ.get("ALMAGEST_DRIVER_DIR")
    or os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "almagest-driver")
)
STATE = STATE_DIR / "state.json"
DB = STATE_DIR / "almagest.db"
API_PORT = int(os.environ.get("ALMAGEST_DRIVER_API_PORT", "8009"))
WEB_PORT = int(os.environ.get("ALMAGEST_DRIVER_WEB_PORT", "5199"))

# The station's WebSocket port is fixed in .env (DEVICEAGENT_WS_PORT) and the
# agent refuses a non-loopback host, so there is nothing to allocate here.
WS_PORT = int(os.environ.get("DEVICEAGENT_WS_PORT", "8765"))


def log(msg: str) -> None:
    print(f"[driver] {msg}", flush=True)


def die(msg: str) -> "None":
    print(f"[driver] FATAL {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# the browser
# --------------------------------------------------------------------------


def find_chrome() -> str:
    """Playwright's cached `chrome-headless-shell` — and it must be that one.

    Nothing browser-shaped is on PATH here, so the cache is the only browser.
    Within it, the *full* Chromium is useless for this: `--screenshot` is a
    legacy-headless flag, and legacy headless was removed from Chrome. The full
    binary accepts the flag, renders nothing, never exits, and keeps retrying GCM
    registration until something kills it. `chrome-headless-shell` is the
    continuation of old headless and still honours it.
    """
    for pattern in (
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "~/.cache/ms-playwright/chromium_headless_shell-*/*/headless_shell",
    ):
        hits = sorted(glob.glob(os.path.expanduser(pattern)))
        if hits:
            return hits[-1]
    found = shutil.which("chrome-headless-shell")
    if found:
        return found
    die("no chrome-headless-shell; expected it under ~/.cache/ms-playwright/"
        "chromium_headless_shell-*/ (the full chromium-* build cannot screenshot)")
    raise AssertionError  # unreachable, keeps type checkers quiet


# --------------------------------------------------------------------------
# the PWA server: static `dist` + SPA fallback + a proxy for /api and /s/
# --------------------------------------------------------------------------

PROXY_PREFIXES = ("/api/", "/s/", "/openapi.json", "/docs")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx back to the browser instead of chasing it.

    `/s/{short_id}` — the entire tag scheme — answers `302` with a **relative**
    `Location`, resolved by whoever receives it. `urlopen` follows redirects by
    default, so this proxy used to fetch `/locations/4` from the API, find it is
    not an API route, and hand back FastAPI's `404` under the tag's own URL. The
    one journey the tags exist for could not be driven with this tool, and an
    agent that tried would report the app broken.

    `deploy/station/station_web.py` has had this since it was written, with nine
    tests; the driver never got the same treatment.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


class PwaHandler(BaseHTTPRequestHandler):
    """Serves the built PWA and forwards the API calls it makes.

    This exists instead of `vite dev` because vite's proxy target is *hardcoded*
    to 127.0.0.1:8000 in frontend/vite.config.ts, which is the maintainer's live
    dev API. Rewriting that file to point elsewhere means either editing tracked
    source or dropping a temp config into the repo; serving `dist` avoids both
    and screenshots the bundle that actually ships.
    """

    dist: Path
    api: str
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # silence
        pass

    def _proxy(self) -> None:
        body = None
        length = int(self.headers.get("content-length") or 0)
        if length:
            body = self.rfile.read(length)
        req = urllib.request.Request(
            self.api + self.path, data=body, method=self.command
        )
        for header in ("content-type", "accept", "idempotency-key"):
            if self.headers.get(header):
                req.add_header(header, self.headers[header])
        location = None
        try:
            with _NO_REDIRECT.open(req) as upstream:
                payload, status = upstream.read(), upstream.status
                ctype = upstream.headers.get("content-type", "application/json")
        except urllib.error.HTTPError as exc:
            # 3xx lands here now that redirects are not followed, which is the
            # point: `exc.headers` carries the Location the browser needs.
            payload, status = exc.read(), exc.code
            ctype = exc.headers.get("content-type", "application/json")
            location = exc.headers.get("location")
        except OSError as exc:
            payload = json.dumps({"detail": f"driver proxy: {exc}"}).encode()
            status, ctype = 502, "application/json"
        self.send_response(status)
        if location is not None:
            self.send_header("location", location)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _static(self) -> None:
        path, _, query = self.path.partition("?")
        target = (self.dist / path.lstrip("/")).resolve()
        if not (target.is_file() and str(target).startswith(str(self.dist))):
            target = self.dist / "index.html"  # SPA fallback: /parts/3 is a route
        data = target.read_bytes()
        if target.name == "index.html" and "_theme=" in query:
            # The only reliable way to pick a theme headlessly. The app resolves
            # `auto` from prefers-color-scheme, and chrome-headless-shell ignores
            # `--blink-settings=preferredColorScheme` — it renders light either
            # way. index.html applies a stored override before first paint, so
            # seeding that key ahead of its inline script is what actually works.
            theme = "dark" if "_theme=dark" in query else "light"
            seed = (
                f'<script>localStorage.setItem("almagest.theme","{theme}")</script>'
            ).encode()
            data = data.replace(b"<head>", b"<head>" + seed, 1)
        types = {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".wasm": "application/wasm",
            ".png": "image/png",
            ".webmanifest": "application/manifest+json",
        }
        self.send_response(200)
        self.send_header("content-type", types.get(target.suffix, "application/octet-stream"))
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self) -> None:
        if self.path.startswith(PROXY_PREFIXES):
            self._proxy()
        elif self.command == "GET":
            self._static()
        else:
            self.send_error(405)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch


def serve_pwa(dist: Path, api: str, port: int) -> None:
    PwaHandler.dist = dist.resolve()
    PwaHandler.api = api
    ThreadingHTTPServer(("127.0.0.1", port), PwaHandler).serve_forever()


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for(port: int, what: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return
        time.sleep(0.3)
    die(f"{what} never came up on :{port} — see {STATE_DIR}/*.log")


def read_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {}


def uv(project: str, *args: str, env: dict | None = None, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", *args], cwd=ROOT / project, env=env, check=False, **kw
    )


def backend_env() -> dict:
    env = dict(os.environ)
    env["ALMAGEST_DB_PATH"] = str(DB)
    # Printed/NDEF payloads point at the driver's own PWA, so a screenshot of a
    # label QR resolves against this stack rather than the maintainer's :8000.
    env["ALMAGEST_BASE_URL"] = f"http://127.0.0.1:{WEB_PORT}"
    env["ALMAGEST_DATASHEET_DIR"] = str(STATE_DIR / "datasheets")
    env["ALMAGEST_LABEL_OUTPUT_DIR"] = str(STATE_DIR / "labels")
    return env


def cmd_up(args: argparse.Namespace) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB) + suffix).unlink(missing_ok=True)
    if port_open(API_PORT) and not args.force:
        log(f"something already answers on :{API_PORT}; `driver.py down` first")
        return

    env = backend_env()
    log(f"database {DB}")
    if uv("backend", "alembic", "upgrade", "head", env=env,
          stdout=subprocess.DEVNULL).returncode:
        die("alembic upgrade failed")
    uv("backend", "python", "-m", "app.scripts.seed_demo", env=env,
       stdout=subprocess.DEVNULL)
    log("migrated + seeded (seed_demo is idempotent)")

    api_log = open(STATE_DIR / "api.log", "wb")
    api = subprocess.Popen(
        ["uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(API_PORT)],
        cwd=ROOT / "backend", env=env, stdout=api_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    wait_for(API_PORT, "the API")
    log(f"API      http://127.0.0.1:{API_PORT}")

    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").is_file() or args.build:
        log("building the PWA (pnpm build)…")
        if subprocess.run(["pnpm", "build"], cwd=ROOT / "frontend").returncode:
            die("pnpm build failed")

    # A half-torn-down stack is the normal state after a timeout killed the API
    # but not the static server. Reusing the survivor beats binding a second
    # socket to a port that is already taken and never noticing.
    web_pid = read_state().get("web_pid")
    if port_open(WEB_PORT):
        log(f"PWA      http://127.0.0.1:{WEB_PORT} (already serving; reused)")
    else:
        web_log = open(STATE_DIR / "web.log", "wb")
        web_pid = subprocess.Popen(
            [sys.executable, __file__, "_serve", str(dist),
             f"http://127.0.0.1:{API_PORT}", str(WEB_PORT)],
            stdout=web_log, stderr=subprocess.STDOUT, start_new_session=True,
        ).pid
        wait_for(WEB_PORT, "the PWA server")
        log(f"PWA      http://127.0.0.1:{WEB_PORT}")

    STATE.write_text(json.dumps(
        {"api_pid": api.pid, "web_pid": web_pid, "api_port": API_PORT,
         "web_port": WEB_PORT, "db": str(DB)}, indent=1))


def cmd_down(_: argparse.Namespace) -> None:
    state = read_state()
    for key in ("api_pid", "web_pid"):
        pid = state.get(key)
        if not pid:
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            log(f"stopped {key}={pid}")
        except (ProcessLookupError, PermissionError):
            pass
    STATE.unlink(missing_ok=True)


def cmd_status(_: argparse.Namespace) -> None:
    state = read_state()
    print(json.dumps({
        **state,
        "api_up": port_open(state.get("api_port", API_PORT)),
        "web_up": port_open(state.get("web_port", WEB_PORT)),
        "state_dir": str(STATE_DIR),
    }, indent=1))


# --------------------------------------------------------------------------
# driving it
# --------------------------------------------------------------------------


def cmd_api(args: argparse.Namespace) -> None:
    port = read_state().get("api_port", API_PORT)
    data = args.body.encode() if args.body else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{args.path}", data=data, method=args.method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(), exc.code
    try:
        print(json.dumps(json.loads(body), indent=1))
    except ValueError:
        print(body.decode(errors="replace"))
    if status >= 400:
        print(f"[driver] HTTP {status}", file=sys.stderr)
        raise SystemExit(1)


def cmd_shot(args: argparse.Namespace) -> None:
    port = read_state().get("web_port", WEB_PORT)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    sep = "&" if "?" in args.route else "?"
    url = f"http://127.0.0.1:{port}{args.route}{sep}_theme={args.theme}"
    cmd = [
        find_chrome(), "--headless=new", "--no-sandbox", "--disable-gpu",
        "--hide-scrollbars", "--force-device-scale-factor=2",
        f"--window-size={args.width},{args.height}",
        f"--virtual-time-budget={args.wait}",
        f"--user-data-dir={STATE_DIR / 'chrome'}",
        f"--screenshot={out}", url,
    ]
    out.unlink(missing_ok=True)
    try:
        # A hard timeout on principle: a browser that will not exit is the single
        # most likely way this command wedges a session, and it has done so here.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        stderr = "chrome-headless-shell did not exit within 90s"
    if not out.is_file():
        print(stderr[-2000:], file=sys.stderr)
        die(f"no screenshot written for {args.route}")
    log(f"{args.route} -> {out} ({out.stat().st_size} bytes)")


STATION_WATCH = r"""
import asyncio, json, os, sys
import websockets

async def main():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agent.main", "--fake",
        "--max-polls", os.environ["DRIVER_POLLS"],
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    await asyncio.sleep(1.5)
    seen = 0
    try:
        async with websockets.connect(os.environ["DRIVER_WS"]) as ws:
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                seen += 1
                print(f"{event.get('seq'):>3} {event.get('type'):<22} "
                      f"{json.dumps(event.get('data'))[:120]}", flush=True)
    except Exception as exc:
        print(f"[stream closed: {type(exc).__name__}]", flush=True)
    err = (await proc.communicate())[1]
    print(f"[agent exit={proc.returncode} events={seen}]", flush=True)
    if proc.returncode:
        sys.stderr.write(err.decode(errors="replace")[-2000:])
        raise SystemExit(1)

asyncio.run(main())
"""


def cmd_station(args: argparse.Namespace) -> None:
    """Run the bench station against the fake reader and print what it emits.

    The station logs almost nothing — every state change goes out over the
    WebSocket that the kiosk PWA subscribes to. Watching that socket is the only
    way to see the session state machine work.
    """
    port = read_state().get("api_port", API_PORT)
    script = STATE_DIR / "_station_watch.py"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    script.write_text(STATION_WATCH)
    env = dict(os.environ)
    env.update(
        DEVICEAGENT_API_BASE_URL=f"http://127.0.0.1:{port}",
        DRIVER_POLLS=str(args.polls),
        DRIVER_WS=f"ws://127.0.0.1:{WS_PORT}",
    )
    raise SystemExit(
        uv("deviceagent", "python", str(script), env=env).returncode
    )


def cmd_provision(args: argparse.Namespace) -> None:
    """Bind the fake reader's UID-only demo tag to a real drawer.

    Without this every scripted tag lands in `station.unidentified`, which is
    correct but shows none of the interesting half of the state machine.
    """
    port = read_state().get("api_port", API_PORT)

    def post(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
            method="POST", headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            die(f"POST {path} -> {exc.code} {exc.read().decode()[:300]}")
            raise AssertionError

    started = post(f"/api/locations/{args.location}/provisioning-sessions",
                   {"device_kind": "station_pn532", "device_id": "driver"})
    session_id = started["state"]["session"]["id"]
    bound = post(f"/api/provisioning-sessions/{session_id}/bind",
                 {"tag_uid": args.uid, "device_id": "driver", "move": True})
    tag = bound["tag"]
    log(f"{args.uid} -> {tag['label_path']}  ndef={tag['ndef_url']}")


def cmd_smoke(args: argparse.Namespace) -> None:
    shots = STATE_DIR / "shots"
    cmd_up(argparse.Namespace(fresh=True, force=False, build=False))
    try:
        for method, path in (("GET", "/api/system/health"),
                             ("GET", "/api/search/parts?limit=3"),
                             ("GET", "/api/locations/tree")):
            log(f"{method} {path}")
            cmd_api(argparse.Namespace(method=method, path=path, body=None))
        cmd_provision(argparse.Namespace(location=3, uid="04AABBCCDDEE10"))
        for route, name in (("/", "home"), ("/search", "search"),
                            ("/tree", "tree"), ("/parts/5", "part")):
            cmd_shot(argparse.Namespace(
                route=route, out=str(shots / f"{name}.png"), theme="light",
                width=430, height=932, wait=6000))
        cmd_station(argparse.Namespace(polls=31))
    finally:
        if not args.keep:
            cmd_down(args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="migrate, seed and serve the API + PWA")
    up.add_argument("--fresh", action="store_true", help="delete the database first")
    up.add_argument("--build", action="store_true", help="rebuild the PWA even if dist/ exists")
    up.add_argument("--force", action="store_true")
    up.set_defaults(func=cmd_up)

    sub.add_parser("down", help="stop everything").set_defaults(func=cmd_down)
    sub.add_parser("status", help="what is running").set_defaults(func=cmd_status)

    api = sub.add_parser("api", help="one API call")
    api.add_argument("method")
    api.add_argument("path")
    api.add_argument("body", nargs="?")
    api.set_defaults(func=cmd_api)

    shot = sub.add_parser("shot", help="screenshot a PWA route")
    shot.add_argument("route")
    shot.add_argument("out")
    shot.add_argument("--theme", choices=("light", "dark"), default="light")
    shot.add_argument("--width", type=int, default=430)
    shot.add_argument("--height", type=int, default=932)
    shot.add_argument("--wait", type=int, default=6000, help="virtual time budget, ms")
    shot.set_defaults(func=cmd_shot)

    station = sub.add_parser("station", help="run the bench station, print its events")
    station.add_argument("--polls", type=int, default=31)
    station.set_defaults(func=cmd_station)

    prov = sub.add_parser("provision", help="bind a demo tag UID to a drawer")
    prov.add_argument("--location", type=int, default=3)
    prov.add_argument("--uid", default="04AABBCCDDEE10")
    prov.set_defaults(func=cmd_provision)

    smoke = sub.add_parser("smoke", help="up, exercise everything, down")
    smoke.add_argument("--keep", action="store_true", help="leave the stack running")
    smoke.set_defaults(func=cmd_smoke)

    if len(sys.argv) > 1 and sys.argv[1] == "_serve":  # internal: the PWA server
        serve_pwa(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
        return
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
