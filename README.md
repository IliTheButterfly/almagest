# Almagest

A self-hosted electronic-component inventory system: track what parts exist,
where they physically are, and how many remain. DigiKey-style parametric search
("through-hole 20–30 µF ceramic capacitor"), cached datasheets, an expandable
physical addressing scheme, NFC-tagged containers, and a bench station that
identifies a container placed on it.

The full design is **[docs/PLAN.md](docs/PLAN.md)** — the source of truth for
architecture and phasing, and old enough now that its header lists which parts of
it were later changed. Decisions taken since are in
**[docs/adr/](docs/adr/README.md)**, and they win where they disagree with the
plan. Repo naming is settled in **[docs/NAMING.md](docs/NAMING.md)**. Invariants
that must not be violated are summarised in **[CLAUDE.md](CLAUDE.md)**, which also
carries the honest, repeatedly-corrected account of what is actually built.

> **Status: Phase 1 nearly through, plus pieces of 2 and 4.** The schema, the
> append-only ledger, parametric search, the storage tree, layout authoring, tag
> provisioning and its verification walk, projects and builds, intake and the
> review queue, the datasheet store with full-text search, an MCP server, and a
> mobile-first PWA over all of it.
>
> **Not built:** the scale and vision counting — the load cell was never bought
> ([ADR 0003](docs/adr/0003-hardware-locked-and-the-scale-deferred.md)) — external
> distributor providers, LLM extraction, and thermal label printing. **And no
> tags may be provisioned yet:** a tag carries a portless URL and nothing answers
> on 443 ([ADR 0009](docs/adr/0009-cluster-deployment-and-the-443-problem.md)).

## Layout

| Path | What |
|---|---|
| `backend/` | FastAPI + SQLAlchemy 2.0 + Alembic over SQLite (WAL, FTS5) — 49 tables, 142 API operations |
| `frontend/` | React + Vite PWA, mobile-first — search, storage, containers, projects, builds, intake, review, the scanner and captures. Barcode decode is `zxing-wasm` in the browser |
| `deviceagent/` | The readers a browser cannot reach: PN532 (UART), RC522 (SPI) and a Flipper Zero over RPC, the station session, and the **device bridge** the PWA talks to over loopback. Every driver has a fake, so none of it needs hardware to run or test |
| `idcodec/` | `almagest-idcodec` — the short-ID codec and tag payload rules, **stdlib only**. Shared by the backend and the agent |
| `mcpserver/` | `almagest-mcp` — the inventory as tools an agent can call: 26 curated tools over the HTTP API |
| `deploy/` | Kubernetes manifests (`base/`, `overlays/`, `jobs/`) plus `deploy/station/` — the bench machine's one-origin server and its systemd units |
| `docs/` | `PLAN.md`, `NAMING.md`, and `adr/` |
| `mensa/` | Submodule — bench station firmware (ESP-IDF). **Not started**; the scale it exists to drive is deferred |
| `antlia/` | Submodule, public — Flipper Zero app: reads a container tag and types its short ID as a USB keyboard, and serves the device bridge |
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
red until somebody decides**. `Excluded` is a fine answer — 116 of the 142
operations are — but the decision cannot be skipped, which is what stops the
agent-facing surface quietly going stale. See
[ADR 0012](docs/adr/0012-the-mcp-server-and-a-forced-coverage-decision.md).

## Getting started

Requires [uv](https://docs.astral.sh/uv/) — it manages the pinned Python 3.12
toolchain, so no system Python version needs to match — and `pnpm` for the
frontend.

```bash
make bootstrap      # submodules, venv, deps, .env from .env.example
make migrate        # apply Alembic migrations
make run            # API with autoreload on :8000
make fe-install     # frontend dependencies
make fe-dev         # Vite dev server, proxying /api to the backend
make check          # everything CI runs except the frontend and the image build
make fe-check       # the frontend half: lint, typecheck, tests, build
```

`make help` lists every target. Docker Compose is the desktop/NAS deployment
path; Kubernetes is the cluster target, with **exactly one API replica and
`strategy: Recreate`** because SQLite tolerates exactly one writer. The bench
machine is a third target — see [deploy/station/](deploy/station/README.md).

## Testing

Each component has its own venv and its own gate, and all but the frontend are
folded into `make check`:

```bash
make check                                             # as CI runs it
make test                                              # backend only
make idcodec-check agent-check mcp-check station-check  # the other gates, individually
make fe-check                                          # its own CI job, not part of `make check`

cd backend && uv run pytest tests/unit/test_value_parser.py -q
cd backend && uv run pytest -k "test_4k7" -q
cd backend && uv run pytest -m live                    # network; skipped by default
cd deviceagent && uv run pytest -m live                # needs a real reader; skipped by default
```

Integration tests run against a temp SQLite file with **real Alembic migrations
applied**, never `create_all()` — that is the only way model/migration drift and
the ledger's append-only triggers are exercised at all. Every hardware path has a
fake, so a full `make check` needs nothing plugged in.
