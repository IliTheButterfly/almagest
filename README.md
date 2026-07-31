# Almagest

A self-hosted electronic-component inventory system: track what parts exist,
where they physically are, and how many remain. DigiKey-style parametric search
("through-hole 20–30 µF ceramic capacitor"), cached datasheets, an expandable
physical addressing scheme, NFC-tagged containers, and a bench station that
identifies, weighs and counts a container placed on it.

The full design is **[docs/PLAN.md](docs/PLAN.md)** — the source of truth for
architecture and phasing. Repo naming is settled in
**[docs/NAMING.md](docs/NAMING.md)**. Invariants that must not be violated are
summarised in **[CLAUDE.md](CLAUDE.md)**.

> **Status: Phase 1, early.** The data model, ledger and search are being built.
> Nothing here is usable as an inventory system yet.

## Layout

| Path | What |
|---|---|
| `backend/` | FastAPI + SQLAlchemy 2.0 + Alembic over SQLite (WAL, FTS5) |
| `frontend/` | React + Vite PWA, mobile-first *(not started)* |
| `deviceagent/` | Runs on the Pi: PN532 NFC, camera, ESP32 serial |
| `idcodec/` | `almagest-idcodec` — the short-ID codec and tag payload rules, **stdlib only**. Shared by the backend and the agent |
| `mcpserver/` | `almagest-mcp` — the inventory as tools an agent can call: 25 curated tools over the HTTP API |
| `mensa/` | Submodule — bench station firmware (ESP-IDF) |
| `circinus/` | Submodule — CAD (OpenSCAD) |
| `ecia-barcode/` | Submodule — MH10.8.2 / EIGP-114 barcode parser |
| `elec-value-parser/` | Submodule — `4k7` / `0R22` electronics shorthand grammar |

`backend`, `frontend`, `deviceagent` and `idcodec` deliberately share one repo:
the first three are bound by the API contract, so a route signature change is one
atomic commit rather than a three-repo dance, and `idcodec` is the identity rule
the backend and the agent must fold *identically* — a split would let the two
versions drift by a release. API clients are **generated** from the OpenAPI schema
(`make openapi`), never hand-written. `idcodec` keeps its own venv and declares no
dependencies, so depending on it does not put the API's runtime on the Pi.

`mcpserver` is in the same repo for the same reason and enforces it harder: every
`openapi.json` operation must have a line in `mcpserver/almagest_mcp/coverage.py`
saying whether an agent may call it, so **adding a backend route turns `make check`
red until somebody decides**. `Excluded` is a fine answer — most routes are — but
the decision cannot be skipped, which is what stops the agent-facing surface
quietly going stale. See [ADR 0012](docs/adr/0012-the-mcp-server-and-a-forced-coverage-decision.md).

## Getting started

Requires [uv](https://docs.astral.sh/uv/) — it manages the pinned Python 3.12
toolchain, so no system Python version needs to match.

```bash
make bootstrap      # submodules, venv, deps, .env from .env.example
make migrate        # apply Alembic migrations
make run            # API with autoreload on :8000
make check          # everything CI runs: lint, typecheck, tests
```

`make help` lists every target. Docker Compose is the desktop/NAS deployment
path; Kubernetes is the cluster target, with **exactly one API replica and
`strategy: Recreate`** because SQLite tolerates exactly one writer.

## Testing

```bash
make test                                              # full suite
cd backend && uv run pytest tests/unit/test_value_parser.py -q
cd backend && uv run pytest -k "test_4k7" -q
cd backend && uv run pytest -m live                    # network; skipped by default
```

Integration tests run against a temp SQLite file with **real Alembic migrations
applied**, never `create_all()` — that is the only way model/migration drift and
the ledger's append-only triggers are exercised at all.
