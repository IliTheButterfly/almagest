# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: greenfield

There is no application code yet. The full design lives in **[docs/PLAN.md](docs/PLAN.md)** — treat it as the source of truth for architecture and phasing, and read the relevant section before implementing anything. Phase 1 has not started.

Repo names and the naming scheme are settled in **[docs/NAMING.md](docs/NAMING.md)**.

The commands below describe the intended toolchain. **They do not work yet** — the scaffolding is part of Phase 1. Do not report a command as failing because the project isn't built; check whether the directory exists first.

## What this is

**Almagest** — a self-hosted electronic-component inventory system: track what parts exist, where they physically are, and how many remain. DigiKey-style parametric search ("through-hole 20–30 µF ceramic capacitor"), cached datasheets, an expandable physical addressing scheme, NFC-tagged containers, and a bench station that identifies/weighs/counts a container placed on it.

The dominant project risk is not technical. Every abandoned system in this space died because manual data entry didn't scale, or because a solo maintainer drowned in an over-engineered stack. **Bias toward boring, and toward making intake fast.**

## Commands (once scaffolded)

```bash
make bootstrap                      # clone submodules, create venv, install deps
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m app.scripts.seed_demo

# Backend
cd backend && pytest -q
cd backend && pytest tests/unit/test_value_parser.py -q          # single file
cd backend && pytest -k "test_4k7 or test_shorthand" -q          # single test
cd backend && pytest -m live                                     # network tests, skipped by default
cd backend && ruff check . && ruff format --check . && mypy app
cd backend && alembic revision --autogenerate -m "description"

# Frontend
cd frontend && pnpm dev && pnpm test && pnpm lint && pnpm build

# Firmware — Mensa, the bench station (submodule, ESP-IDF)
cd mensa && idf.py build flash monitor
```

Highest-value test suite is `backend/tests/unit/test_value_parser.py` — the electronics-shorthand grammar. Second is the ECIA fixture set, where `tests/fixtures/ecia/*.bin` paired with hand-verified `*.expected.json` **are** the ground truth, since no reference parser exists to diff against.

## Repository structure

Master repo (**Almagest**) plus submodules, split only where coupling is genuinely absent:

- `backend/`, `frontend/`, `deviceagent/` — **one repo, kept together.** All three are bound by the API contract; a route signature change touches all of them and must be one atomic commit.
- `mensa/` — submodule. The bench station firmware: ESP-IDF, separate toolchain.
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

**`short_id`**: Crockford base32, 7 data symbols + 1 mod-37 check symbol, rendered `4K7T-92MQ`. One shared ID space across all object types via `object_ids(short_id PK, entity_type, entity_pk)`, so a scan resolves without knowing the type and survives an object being reclassified. Type is a cosmetic display prefix, never parsed.

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
