---
name: run-almagest
description: Build, launch, screenshot and drive Almagest — the FastAPI backend, the React PWA and the bench-station device agent. Use when asked to run or start the app, take a screenshot of a screen, check a change in the real running app rather than in tests, exercise an API route by hand, or replay a station/NFC session.
---

# Running Almagest

Three surfaces, one driver. Paths below are relative to the **repo root**.

```
.claude/skills/run-almagest/driver.py
```

Standard library only, run with the system `python3` — it starts the three uv
projects, so it cannot live inside any one of their venvs.

```bash
python3 .claude/skills/run-almagest/driver.py smoke     # the whole thing, then tear down
```

Everything runs against a **throwaway database** under
`$CLAUDE_JOB_DIR/almagest-driver/` (override with `ALMAGEST_DRIVER_DIR`) on ports
**8009** and **5199**. That is deliberate: **8000 and 5173 are the maintainer's own
`make run` + `vite dev` on the main checkout**, and a write through those lands
real ledger rows in the live database. Never point the driver at them.

## Prerequisites

Already present on this machine; nothing needed to install.

- `uv` (manages the pinned Python 3.12 — no system interpreter has to match)
- `node` 22 + `pnpm` 11
- Playwright's browser cache — see Gotchas, the binary choice is not obvious

## Setup (once per clone or worktree)

```bash
git submodule update --init --recursive
cp -n .env.example .env
cd idcodec     && uv sync --all-extras --dev && cd ..
cd backend     && uv sync --all-extras --dev && cd ..
cd deviceagent && uv sync --all-extras --dev && cd ..
cd frontend    && pnpm install && cd ..
```

(`make bootstrap` does all of this in one target.)

## Run (agent path)

```bash
D=".claude/skills/run-almagest/driver.py"

python3 $D up                 # migrate + seed a fresh DB, serve API and PWA
python3 $D status             # ports, pids, database path
python3 $D down               # stop both
```

`up` builds the PWA if `frontend/dist` is missing (`--build` forces it), runs
Alembic to head, and runs `app.scripts.seed_demo` — which is idempotent, so
re-running `up` on an existing database adds nothing. `--fresh` deletes the
database first.

### Call the API

```bash
python3 $D api GET  /api/system/health
python3 $D api GET  '/api/search/parts?q=capacitor&limit=3'
python3 $D api POST /api/stock/receive \
  '{"part_id":5,"location_id":4,"qty_milli":42000,"client_op_id":"drv-1","device_id":"driver"}'
```

Non-2xx prints the body and exits 1. Every route in `openapi.json` is reachable.

### Screenshot a screen

```bash
python3 $D shot /              out/home.png
python3 $D shot /tree          out/storage.png
python3 $D shot /locations/4   out/drawer.png --height 1100
python3 $D shot /search        out/dark.png   --theme dark
```

Defaults are a 430x932 phone viewport at 2x — the PWA is mobile-first. `--width`,
`--height`, `--theme light|dark` and `--wait <ms>` are the knobs. **Open the PNG
with Read afterwards.** A screenshot that got written is not a screenshot that
rendered.

### Drive the bench station

The station has no hardware here; `--fake` replays a 31-poll scripted session.

```bash
python3 $D provision            # bind the demo tag UID to a real drawer first
python3 $D station              # run it and print the WebSocket event stream
```

`station` prints what the kiosk PWA would see:

```
 13 tag.error              {"message": "PN532 did not ACK: SAMConfig timed out on /dev/ttyAMA0"}
 14 tag.identified         {"short_id": null, "tag_uid": "04AABBCCDDEE10", "ndef_url": null, "via": "uid"}
 15 station.ready          {"state": "ready", "session_id": "...", "client_op_id": "..."}
 17 station.aborted        {"state": "idle", "reason": "swapped", "discarded": null}
```

`provision` is what makes `station.ready` reachable — see Gotchas.

## Run (human path)

```bash
make run        # API, autoreload, :8000
make fe-dev     # vite dev server, :5173, proxying /api to :8000
make agent-run  # the device agent against the fake reader
```

This is the maintainer's loop and it is the one the driver stays out of the way
of. Useless for an agent: `fe-dev` needs a browser, and the station logs nothing
but faults — its state changes only exist on the WebSocket.

## Test

```bash
make check                     # ruff + mypy --strict + pytest: idcodec 46, backend 1823, deviceagent 233 — ~11 min
cd frontend && pnpm test       # 70 files, 819 tests, ~30 s (not part of `make check`)
cd backend  && uv run pytest tests/unit/test_value_parser.py -q
```

## Gotchas

- **`chrome-headless-shell`, not `chromium`.** Both are in
  `~/.cache/ms-playwright/`. The full `chromium-*/chrome-linux64/chrome` **cannot
  screenshot**: `--screenshot` is a legacy-headless flag and legacy headless is
  gone from Chrome. It accepts the flag, writes no file, never exits, and loops
  on GCM registration until something kills it — a ten-minute hang with no error.
  `--disable-background-networking` does not help. Use
  `chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell`,
  which is what `driver.py` picks. The screenshot call has a 90 s hard timeout
  for exactly this reason.
- **`--theme dark` works by seeding `localStorage`, not by a Chrome flag.**
  `--blink-settings=preferredColorScheme=2` is silently ignored by
  `chrome-headless-shell`: both themes come out light, and the screenshot looks
  plausible enough that it is easy to miss. The app resolves `auto` from
  `prefers-color-scheme` but honours a stored `almagest.theme` override applied
  by an inline script in `index.html` before first paint, so the driver's server
  injects `localStorage.setItem("almagest.theme", …)` ahead of it, keyed off a
  `_theme=` query parameter it adds to the URL. Confirm dark actually rendered by
  looking at the moon in the header toggle.
- **The driver serves `frontend/dist` itself instead of using vite.** Vite's
  proxy target is hardcoded to `127.0.0.1:8000` in `frontend/vite.config.ts` —
  the live dev API. Repointing it means editing tracked source or dropping a temp
  config into the repo. The driver's own server does static + SPA fallback +
  `/api` and `/s/` proxy, so `/parts/5` and `/locations/4` resolve as routes. Cost:
  no HMR. Re-run `up --build` after a frontend change.
- **`--fake` logging "PN532 did not ACK: SAMConfig timed out on /dev/ttyAMA0" is
  not a hardware problem.** Poll 20 of `deviceagent/agent/fixtures/scripted_session.json`
  is a *scripted* reader fault. No PN532 has ever been attached to this codebase;
  the fixture is hand-written and says so.
- **Without `provision`, every scripted tag is `station.unidentified`.** Correct
  behaviour — the fixture's short IDs (`4K7T92M8`, `NX6C5ZTQ`) do not exist in a
  freshly seeded database, and short IDs are minted, so you cannot make one match.
  The way in is the UID-only tag at polls 23–24: `provision` binds
  `04AABBCCDDEE10` to a drawer through the real provisioning-session API, and the
  station then reaches `station.ready` via the UID fallback.
- **`device_kind` is `station_pn532`, not `station`.** The enum is
  `phone_webnfc | station_pn532 | manual`; the short form 422s.
- **`ALMAGEST_DB_PATH` is the env var.** `ALMAGEST_DATABASE_URL` is ignored.
- **`POST /api/locations/{id}/provisioning-sessions` returns the session id at
  `state.session.id`**, not at a top-level `session_id`.
- **`git checkout` in the parent resets submodule worktrees** (`submodule.recurse`
  is on). Commit inside a submodule before touching the parent.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `shot` hangs, then "did not exit within 90s" | You are on the full chromium. See the first gotcha. |
| `up` says something already answers on :8009 | `python3 $D down`, or `up --force` |
| `[driver] FATAL the API never came up` | `cat $CLAUDE_JOB_DIR/almagest-driver/api.log` |
| PWA loads but every panel is empty | The API died — same log. The proxy answers `{"detail": "driver proxy: <urlopen error [Errno 111] Connection refused>"}` while static routes still return 200, so the shell renders and the data does not. |
| `station` prints `OSError: [Errno 98] ... bind on address ('127.0.0.1', 8765)` | Another agent run still holds the event socket: `pkill -f agent.main` |
| `mypy` fails on `segno`/`PIL` in a fresh worktree | `uv sync --all-extras` (not plain `uv sync`) in `backend/` |
