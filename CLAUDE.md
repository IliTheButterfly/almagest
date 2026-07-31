# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: Phase 1, backend underway

The full design lives in **[docs/PLAN.md](docs/PLAN.md)** — treat it as the source of truth for architecture and phasing, and read the relevant section before implementing anything. Repo names are settled in **[docs/NAMING.md](docs/NAMING.md)**; decisions taken since are in **[docs/adr/](docs/adr/)**.

**Built and green** (`make check` passes: ruff, `mypy --strict`, pytest):

- `backend/` scaffolding, Alembic, CI, Docker build
- the core schema — 23 tables, append-only ledger enforced by DB triggers
- `idcodec/` — the short-ID codec and tag payload rules, standard library only
- `services/shortid.py` (the session-taking half), `services/tree.py`, `services/parameters.py`
- `services/search/` — the value-parser adapter and the parametric filter executor
- `/api/search/parts`, `/api/resolve/{short_id}`, `/s/{short_id}`, `/api/system/health`
- both submodule libraries (`elec-value-parser`, `ecia-barcode`), tagged and pinned
- `deviceagent/` — the `TagSource` protocol, the fake that replays a scripted
  session, NDEF decoding, NDEF-first/UID-fallback resolution, tag presence, the
  **station session** (PLAN.md workflow 5: identify → ready → propose → confirm →
  commit, looping while the tag stays put), the API client, and the loopback
  WebSocket. `Pn532TagSource` is written but **has never run**: no reader exists,
  so its contract test is `live`-marked

**Not built yet:** `frontend/`, the scan resolver chain and alias learning, layout
authoring, tag provisioning, label sheets, FTS5. The station's **scale half is
deferred, not pending** — see `docs/adr/0003`, which supersedes PLAN.md's
weight-triggered state machine: continuous PN532 polling is the trigger, and
nothing weight-related exists (no `weighings`, no `WeightSource`, no `weight.*`
event, and deliberately no feature flag for their absence). Workflow 5's
`CONTAINER_DETECTED` and `WEIGHED` states are **gone rather than stubbed** — see
`deviceagent/README.md` for the diagram as built and what each PLAN.md state
became.

The station **commits over HTTP through the existing `/api/stock/...` routes** and
mints its idempotency key when the container is identified, not at commit. Removing
a container before COMMIT aborts and writes nothing; `deviceagent/tests/
test_session_ledger.py` asserts that against real migrations and real rows.

The commands below **work**. If one fails, that is a real failure — do not
attribute it to the project being unbuilt.

## What this is

**Almagest** — a self-hosted electronic-component inventory system: track what parts exist, where they physically are, and how many remain. DigiKey-style parametric search ("through-hole 20–30 µF ceramic capacitor"), cached datasheets, an expandable physical addressing scheme, NFC-tagged containers, and a bench station that identifies/weighs/counts a container placed on it.

The dominant project risk is not technical. Every abandoned system in this space died because manual data entry didn't scale, or because a solo maintainer drowned in an over-engineered stack. **Bias toward boring, and toward making intake fast.**

## Commands

**Python is pinned to 3.12 and managed by [uv](https://docs.astral.sh/uv/)** — no
system interpreter needs to match, and `uv` downloads the toolchain itself. Every
backend command runs through `uv run`; there is no venv to activate.

```bash
make bootstrap        # submodules, venv, deps, .env from .env.example
make migrate          # alembic upgrade head
make run              # API with autoreload on :8000
make check            # everything CI runs: lint, mypy --strict, pytest (idcodec + backend + deviceagent)
make help             # all targets

# Backend, directly
cd backend && uv run pytest -q
cd backend && uv run pytest tests/unit/test_scan_codes.py -q     # single file
cd backend && uv run pytest -k "worked_example" -q               # single test
cd backend && uv run pytest -m live                              # network; skipped by default
cd backend && uv run alembic revision --autogenerate -m "description"

# Device agent (its own venv and lock — the API image must not grow a serial library)
make agent-check      # ruff, mypy --strict, pytest; folded into `make check`
make agent-run        # the agent against the fake reader, no hardware needed
cd deviceagent && uv run pytest -q
cd deviceagent && uv run pytest -m live      # needs a real PN532; skipped by default

# idcodec — no dependencies at all, so its own venv is the point
make idcodec-check    # ruff, mypy --strict, pytest; folded into `make check`
cd idcodec && uv run pytest -q

make check-migrations # applies migrations, then `alembic check` for model drift
make openapi          # regenerate openapi.json — CI fails if it is stale
```

Docker Compose is the desktop/NAS deployment path, not the dev loop:

```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m app.scripts.seed_demo
```

Frontend (`pnpm`) and Mensa firmware (`idf.py`) commands land with those
components; neither directory exists yet.

## Conventions that CI enforces

Learn these before writing code — each has a test that fails loudly.

- **No `CHECK` constraints anywhere.** A test greps `sqlite_master`. `sa.Enum` is
  the trap: it silently emits `VARCHAR + CHECK`. Use `sa.String` +
  `StrEnumType(SomeStrEnum)` from `app/models/types.py`.
- **Integration tests run real Alembic migrations**, never `create_all()` — that
  is the only way model/migration drift and the ledger triggers are exercised.
- **Migrations must not import from `app`.** `alembic/env.py` renders custom
  types as `sa.String` so a migration never depends on application code.
- **Every numeric `parameter_value` needs `value_min`/`value_max`**, equal for a
  scalar. Search is an interval-overlap test, so a null-bounded row is invisible
  to every range query — silently. Write through `services/parameters.py`.
- Timestamps use `UtcDateTime`; quantities are `*_milli INTEGER`, money
  `*_micro INTEGER`.

Working on a submodule? `submodule.recurse` is on, so a `git checkout` in the
parent **resets the submodule worktree**. Commit and push inside the submodule
first, then bump the pointer.

Highest-value test suite is `backend/tests/unit/test_value_parser.py` — the electronics-shorthand grammar. Second is the ECIA fixture set, where `tests/fixtures/ecia/*.bin` paired with hand-verified `*.expected.json` **are** the ground truth, since no reference parser exists to diff against.

## Repository structure

Master repo (**Almagest**) plus submodules, split only where coupling is genuinely absent:

- `backend/`, `frontend/`, `deviceagent/` — **one repo, kept together.** All three are bound by the API contract; a route signature change touches all of them and must be one atomic commit.
- `idcodec/` — same repo, its own distribution (`almagest-idcodec`) and its own venv. The short-ID codec and the tag payload rules, **standard library only**. Both the API and the agent depend on it by path — the API re-exports it through `app.services.shortid` and `app.services.provisioning` so existing call sites are untouched, the agent imports it directly — so the two can never fold a tag UID differently. It exists because the agent runs on a Pi 4 and used to pull the whole API runtime in for two pure functions. Nothing that needs a `Session`, `app.models` or `app.config` may go in it; `idcodec/tests/test_stdlib_only.py` fails if anything non-stdlib is imported.
- `mensa/` — submodule. The bench station firmware: ESP-IDF, separate toolchain.
- `antlia/` — submodule, **public**. A Flipper Zero app that reads a container tag and types its short ID into the connected computer as a USB keyboard, so a laptop can identify a bin. Split for the same reason as `mensa/`: its own toolchain (`ufbt`, the Flipper SDK) and no coupling to the API contract — it needs no network at all. It carries a **second implementation of the short-ID codec, in C**, which is the one risk worth knowing about: `antlia/tests/vectors.h` is generated from `idcodec/` by `antlia/tools/gen_vectors.py`, so a divergence fails Antlia's CI. Change `idcodec/shortid.py` or `tagpayload.py` and you must run `make vectors` there too.
- `circinus/` — submodule. OpenSCAD/CAD; binary-ish files that would bloat every clone forever.
- `ecia-barcode/`, `elec-value-parser/` — submodules, PyPI-publishable. Names stay descriptive, not thematic — they are the only artifacts aimed at strangers. Extract via `git subtree split` (preserves history) once their tests are green; not before.

Only repos get constellation names; everything inside one stays descriptive. See [docs/NAMING.md](docs/NAMING.md).

API clients are **generated from FastAPI's OpenAPI schema**, never hand-written — that is what makes the cross-repo splits safe.

## Architecture invariants

These are non-obvious, load-bearing, and expensive to retrofit. Violating any of them is a design bug, not a style preference.

**Three-tier stock model.** `parts` (definition) → `stock_lots` (a physical package at a location) → `locations`. **Quantity lives on the lot, never on the part.** PartKeepr hung it on the part and could never support multi-location or per-batch cost. Lots are packaging-aware: a 5000-piece reel and a cut-tape strip of the same MPN in the same bin are two lots.

**The ledger is append-only, enforced by DB triggers.** `stock_ledger` rejects UPDATE and DELETE via `RAISE(ABORT)`. Undo is a compensating row with `reversal_of_seq`, never a delete. Balances **must** be read from `stock_lots.qty_milli_cached` — summing the ledger in an API path is how this design dies at 200k rows. A nightly job compares cache to `SUM(delta_milli)` and records drift.

**Never use `CHECK`-constraint enums, and never `sa.Enum`** (which silently emits `VARCHAR + CHECK`). SQLite cannot alter a `CHECK`, so a `CHECK` enum turns "add a new kind" into a full table rebuild. Use `sa.String` plus a Python `StrEnum` validated at the model layer. This single rule is what keeps every deferred feature purely additive.

**Quantities are `*_milli INTEGER`** (thousandths of the part's UoM) so ledger sums stay exact. Money is `*_micro INTEGER` + currency. `pint` handles parsing and display only, never storage.

**Two separate trees**, both adjacency list (`parent_id`) plus cached `depth`/`id_path`/`label_path` rebuilt by one recursive CTE: `locations` (physical) and `part_categories` (logical taxonomy). Not nested sets, not a closure table. `id_path` uses numeric ids so renames never invalidate prefix queries; subtree filtering is `id_path LIKE :prefix || '%'`. The cache is fully reconstructible from `parent_id`, so a cache bug is never data loss. Ancestors of X = rows whose `id_path` is a prefix of X's. Implement once as a mixin plus `TreeRepository`, parameterized by table.

**Hierarchy is never encoded in a printed or tag payload.** Containers move; an encoded path becomes a lie the moment a drawer changes cabinet. Labels and tags carry only the opaque `short_id`; the human path is always derived. Same reasoning forbids `(container, index)` payloads.

**`short_id`**: Crockford base32, 7 data symbols + 1 mod-37 check symbol, rendered `4K7T-92M8`. One shared ID space across all object types via `object_ids(short_id PK, entity_type, entity_pk)`, so a scan resolves without knowing the type and survives an object being reclassified. Type is a cosmetic display prefix, never parsed.

**One payload, two carriers.** `https://<host>/s/{short_id}` is written both as the QR content and as the NFC NDEF URI record. Record the tag UID as well and resolve NDEF-first with a UID fallback. **Nothing mutable ever goes on a tag** — not counts, not fill state. A remote mutation (bulk import, reconciliation job, BOM pick) cannot touch a tag it does not physically hold, so the tag would go stale while still looking authoritative. A tag is a foreign key, not a record.

**`UNIQUE(part_id, template_id)` on `parameter_value` is load-bearing.** It guarantees each join contributes at most one row, so multi-predicate parametric queries use plain `JOIN`s that never fan out. Enum facets (dielectric, mounting, package) live in the same table via `choice_id` so search, provenance and review have one code path. `parameter_template.substitution_direction` (`higher_ok`/`lower_ok`/`range_overlap`/`exact`) means substitution search reuses the same filter executor with a swapped operator table — there is no second query engine.

**Capacity is advisory and a scan is never rejected.** An over-capacity put-away is accepted, the location is flagged `is_overfull`, and a defrag suggestion is generated. When auto-assignment finds nothing, it escalates — drop soft preferences, materialize a free grid cell, propose a defrag move plan, propose a new sibling container, and finally fall back to a permanent `INBOX` staging location. Blocking scans teaches the user to stop scanning.

**Enrichment never writes `parameter_value` directly** — it writes `parameter_value_candidate`, promoted only when the field is empty and single-source with confidence ≥ 0.8, or when sources agree. Disagreement priority: `manual > datasheet_table > mpn_decoder > distributor_freetext > llm_inferred`.

**Substitution search stays deterministic — never delegate it to a model.** `parameter_template.substitution_direction` decides what satisfies a requirement, and it is correct by construction. Embeddings and LLMs may *suggest candidates to explore* (the fuzzy front door: "something to level-shift 3.3 V to 5 V"), but only the SQL filter decides. A plausible substitute with the wrong voltage rating is a field failure.

**Never auto-accept an OCR'd or LLM-read part number.** Vision models confidently invent plausible values when the source is illegible, and a wrong-but-confident part ID is worse than "unknown". Route to the review queue with the image attached.

**Reservations are derived** from `stock_allocations` into a cache, never stored as an authoritative counter — a hand-maintained counter drifts and cannot be rebuilt.

## Honest capability limits

Encode these in user-facing text; do not let the UI imply more precision than exists.

- **The scale cannot count below ~150 mg/unit.** 0402 (0.7 mg) and 0603 (2 mg) are at or below the noise floor and must be **refused outright** — the dominant error is the load cell's own nonlinearity, which averaging cannot remove.
- **Differential weighing is primary**, absolute recount secondary. Weigh → take → weigh removes the container tare from the formula, which is what makes it survive reel splits and relabeling.
- **Vision counting is ~99% on a monolayer of one part type up to ~150–250 parts.** A poured pile of thousands is not countable by any method here. The workflow is therefore **count the handful removed, not the bin** — segmentation error grows with density.
- **Stacking must never fail silently.** 2D silhouettes lose occluded area with no bounded correction. Three detectors (shallow tray, scale cross-check, raking light) each force a hard "spread these out and retry".
- Vision and mass estimates fuse by inverse-variance weighting; `|z| > 3` means **do not fuse, flag** — it indicates a mixed bin, a wrong `unit_mass_mg`, or hidden overlap.
- **The resistor colour-band reader is a *checker*, not an identifier.** Verifying an expected value is robust; open-ended identification must return ranked candidates, never an assertion. Classify in CIELAB (not RGB/HSV), normalise against a fixed white patch on the tray, and snap results to the nearest E24/E96 value — E-series membership is the strongest error-correction signal available.
- **The IC marking reader's limit is the marking→part lookup, not the optics** (~30–60% end-to-end). It is a timeboxed side project, below anything touching counting or tracking correctness.

## Environment

Secrets go in `.env` (gitignored); `.env.example` documents every key. Machine- and cluster-specific context lives in `CLAUDE.local.md`, which is also gitignored — **do not move cluster names, hostnames or namespaces into this file or any other committed file.**

Deployment target is Kubernetes. One architectural consequence matters here regardless of cluster: the datastore is SQLite on a ReadWriteOnce volume, so the API runs **exactly one replica with `strategy: Recreate`**. A `RollingUpdate` would try to attach a second pod to the same RWO volume and deadlock, and two SQLite writers is corruption. See `CLAUDE.local.md` for concrete cluster details.

The manifests live in **`deploy/`** and the operational half is **[deploy/README.md](deploy/README.md)**; the shape and the cluster probing behind it are in **[docs/adr/0009](docs/adr/0009-cluster-deployment-nodeport-443.md)**. Three things about it are easy to get wrong:

- **Images are built only by `.github/workflows/release.yml`.** There is no container runtime on the dev box, so there is no local build target and never should be a Makefile target pretending otherwise.
- **`make k8s-deploy` scales the API to zero before migrating**, then applies. That downtime is deliberate — RWO does not prevent two writers, because both pods land on the same node.
- **`nodePort: 443` is exact, not a default.** Every provisioned NFC tag and printed QR carries `https://almagest.lan/s/{short_id}` with no port in it, and a tag is a physical object no migration can reach.
