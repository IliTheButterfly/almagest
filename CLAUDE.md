# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: Phase 1 nearly through, plus pieces of 2 and 4

The full design lives in **[docs/PLAN.md](docs/PLAN.md)** — treat it as the source of truth for architecture and phasing, and read the relevant section before implementing anything. Repo names are settled in **[docs/NAMING.md](docs/NAMING.md)**; decisions taken since are in **[docs/adr/](docs/adr/README.md)**, and **an ADR wins wherever it disagrees with PLAN.md** — PLAN.md's header lists the places that happens.

**Cite ADRs by slug, not by number.** Five numbers are used twice (0007, 0009, 0011, 0012, 0013) because two work streams numbered independently, and they are not being renumbered — some four hundred `ADR 00NN` citations already exist in code, and repointing a fraction of them at the wrong decision is worse than the collision. [docs/adr/README.md](docs/adr/README.md) is the index that resolves one.

**Built and green** (`make check` passes: ruff, `mypy --strict`, pytest):

- `backend/` scaffolding, Alembic, CI, Docker build
- the core schema — 49 tables now (23 at first), append-only ledger enforced by DB triggers
- `idcodec/` — the short-ID codec and tag payload rules, standard library only
- `services/shortid.py` (the session-taking half), `services/tree.py`, `services/parameters.py`
- `services/search/` — the value-parser adapter and the parametric filter executor
- `/api/search/parts`, `/api/resolve/{short_id}`, `/s/{short_id}`, `/api/system/health`
- **the datasheet-research queue** (`services/research.py`, `/api/research/*`) —
  ADR 0017's stage *before* extraction: claim a part that has no datasheet, report
  every candidate URL tried with its verdict, and let the API derive the outcome.
  Six states, and `exhausted` is not `failed` — "no datasheet exists for this part"
  is a normal result and must stay out of the failure health check. The queue is
  five columns and an index on `parts`, **not a table**, for the reason
  `ExtractionState`'s docstring gives. The worker that drives it is not written yet
- both submodule libraries (`elec-value-parser`, `ecia-barcode`), tagged and pinned
- `frontend/` — the PWA, and far more of it than this file used to admit: search,
  storage, containers, projects, builds, intake, review and the scanner. Camera
  decode is `zxing-wasm` in the browser over an escalating pass ladder
- **captures** — the still the scanner keeps, with every barcode *and* every
  OCR'd line outlined on it and tappable. Text is read in the browser
  (`tesseract.js`), which amends ADR 0005 for this one case and only this one;
  see ADR 0015 for why the datasheet split does not fit here. A capture parks
  into the intake queue with its photograph attached, and `extract.ts` pairs each
  printed heading with the value under it to suggest fields — **ranked
  suggestions, never applied values**, per the never-auto-accept rule
- `mcpserver/` — the MCP server: 26 curated tools over the HTTP API, writes gated
  behind `ALMAGEST_MCP_ALLOW_WRITES`, and `coverage.py` + its manifest test forcing
  a disposition for **every** route so the tool surface cannot silently go stale.
  See ADR 0012 (mcp)
- `deviceagent/` — the `TagSource` protocol, the fake that replays a scripted
  session, NDEF decoding, NDEF-first/UID-fallback resolution, tag presence, the
  **station session** (PLAN.md workflow 5: identify → ready → propose → confirm →
  commit, looping while the tag stays put), the API client, and the loopback
  WebSocket. **Three drivers, and the two wired ones have still never run**:
  `Pn532TagSource` (UART, what PLAN.md specifies, the default) and
  `Rc522TagSource` (SPI, added because an MFRC522 was already on hand — see
  ADR 0013 (rc522), and note PLAN.md rejects it on library grounds that no longer
  apply since `agent/iso14443a.py` is ours and unit-tested), plus a Flipper Zero
  over RPC (ADR 0014) — that third one **has** now run on a device, see below. All
  three contract tests are `live`-marked. `DEVICEAGENT_READER` chooses between the two station
  modules — or `none`, which says this machine has no platform reader at all
  and only the bridge's USB readers matter (ADR 0014's laptop-with-a-Flipper,
  and what `deploy/station/` configures). Nothing above the driver knows which
  answered
- `deviceagent/` again, as the **device bridge** (ADR 0014) — reader discovery, a
  capability set per attached device, `tag.write`, and a write path on every
  station driver, plus a Flipper Zero over its own RPC on USB or BLE launched
  into bridge mode automatically. `agent/flipper/fake.py` is a Flipper made of
  software, so the whole path is tested with nothing plugged in. **The USB half
  has now run on real hardware** (see the antlia bullet below); BLE stays opt-in
  and its discovery call has still never reached a radio
- `frontend/src/lib/tags/bridge.ts` — the browser's client for the above, which
  degrades in silence when no bridge is running (almost every page load)
- `antlia/` bridge mode — the Flipper side, and **it has now run on a device**
  (Momentum `mntm-012`, API 87.1, 2026-08-02): a real tag was read, written with a
  server-minted URI, read back identically, and resolved by a verification walk.
  `deploy/station/commission_hardware.py` is that run and
  `deviceagent/tests/test_flipper_live.py` is the checklist. Two bugs only
  hardware could find are fixed in passing — see ADR 0014's "Verified on
  hardware". **BLE is still unrun and now has a known blocker**: `bleak` needs
  BlueZ >= 5.51 and the bench machine has 5.48

**Also built, and this list said otherwise until round 11 of the review caught
it**: the scan resolver chain (`services/scanning/resolver.py` behind
`POST /api/scan/resolve`), alias learning (`scanning/aliases.py`,
`POST /api/scan/alias`), layout authoring (`services/layout_authoring.py`),
label sheets (`POST|GET /api/labels/sheets` + `label_rendering.py`) and FTS5
(`services/search/fts.py` and its migration). 235 tests cover the five.

**Label sheets exist as routes only** — nothing in the PWA calls them, there is
no print affordance on a container screen, and `mcpserver/coverage.py` excludes
both as `HANDS_ON`. Printing a drawer card today means `curl`. Said out loud
because the correction above otherwise trades one misleading absence for a
misleading presence: the next person looking for the print button would conclude
it had broken.

And **`curl` only works for two of the eleven seed container types.** A card is
sized from `front_width_mm`/`front_height_mm`, `card_size_mm` raises rather than
guessing, and only `raaco-c8-30` and `raaco-c10-40` have them — filled in from
their own seed description's "~18x87 mm cards". `akro-mils-10144` and all eight
`gridfinity-*` types state no card size anywhere, so anything created from them
answers `missing_front_dimensions`. Two things follow, and neither is a bug in
the label code:

- **Somebody has to measure a drawer front.** `ContainerTypeForm` accepts both
  fields now, so a hand-made type is fine.
- **A container already standing on a seed type cannot be repaired at all.**
  `PATCH /api/container-types/{id}` *clones* a seed instead of mutating it, and
  **no route repoints a location at another container type**. So the refusal's
  advice — set the dimensions on the type — does not work for the case that
  needs it. A repoint route is the fix and is not written; it is only reachable
  by `curl` today, which is why it is recorded here rather than rushed.

**Half-built, and be precise about which half:** the **vision path**
(`services/enrichment/vision.py` + `vision_openai_compat.py`, ADR 0021) is the
first image-to-model interface here. The interface, the schema, the parser, the
fake and both wire shapes exist and are tested offline; `test_e2e_capture_dispatch.py`
runs a photograph through to a reviewable candidate with no GPU and no network.
**It has now run against a real model** — `qwen3-vl:8b` on the cluster's Ollama,
2026-08-07, all five live contract tests passing. Both wire shapes work; the
reasoning budget, not the image, is what sets `max_tokens`; and a server enforces
a constrained field's *type* but not its bounds. See ADR 0021's "Verified on
hardware" and "What it actually reads" for the numbers and for the one
confidently-wrong answer that the `source_text` requirement caught. There is
still **no queue, no worker and no UI**, so nothing invokes it outside a test and
a script.

**Not built yet:** any *unattended* agent-assisted field filling — capture
extraction is algorithmic and offers candidates a person chooses between, and the
vision path above has no worker to drive it. Tag provisioning has
its API, its walks, a reader that can write, **and now tags written by real
hardware** — what it does not have is a tag written through a mounted container
rather than one lying on a desk. Treat this list with suspicion anyway: it is
older than the code and has now been wrong twice, `frontend/` having sat in it
while the PWA grew past 240 files, and the five items above having sat in it while
they were built and tested. **Check before believing it** — this file is the
first thing every agent reads, and a stale absence sends people looking for code
that is already there. The station's **scale half is
deferred, not pending** — see [ADR 0003](docs/adr/0003-hardware-locked-and-the-scale-deferred.md), which supersedes PLAN.md's
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
make check            # ruff, mypy --strict, pytest across idcodec + backend + deviceagent
                      #   + mcpserver + deploy/station, plus the openapi.json staleness check.
                      #   Everything CI runs EXCEPT the frontend and the image build
make fe-check         # the frontend gate: lint, typecheck, tests, build. Its own CI job, and
                      #   deliberately not folded in — it is a different runtime entirely
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
cd deviceagent && uv run pytest -m live      # needs a real reader — PN532, RC522 or a
                                             #   Flipper Zero; skipped by default

# MCP server — the inventory as tools an agent can call
make mcp-check        # ruff, mypy --strict, pytest; folded into `make check`
make mcp-run          # the stdio server (an MCP client normally launches this itself)
cd mcpserver && uv run pytest -q
cd mcpserver && uv run pytest -m live    # needs a running API; skipped by default

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

The frontend is `pnpm`, wrapped in `make fe-install` / `fe-dev` / `fe-check` /
`fe-api` so nobody has to remember which directory it runs from. **Mensa firmware
(`idf.py`) has no commands because there is no firmware** — the submodule holds a
README and nothing else, and the scale it exists to drive is deferred, not
pending (ADR 0003).

The bench machine is its own deployment target — `deploy/station/`, all on
loopback with no root, gated by `make station-check`. See
[deploy/station/README.md](deploy/station/README.md).

## Conventions that CI enforces

Learn these before writing code — each has a test that fails loudly.

- **No `CHECK` constraints anywhere.** A test greps `sqlite_master`. `sa.Enum` is
  the trap: it silently emits `VARCHAR + CHECK`. Use `sa.String` +
  `StrEnumType(SomeStrEnum)` from `app/models/types.py`.
- **Integration tests run real Alembic migrations**, never `create_all()` — that
  is the only way model/migration drift and the ledger triggers are exercised.
- **Every backend route needs a line in `mcpserver/almagest_mcp/coverage.py`.**
  That file maps every `openapi.json` operation id to `Exposed("tool")` or
  `Excluded(Reason.X, "why")`, and `mcpserver/tests/test_coverage_manifest.py`
  diffs it against the committed schema in both directions. **Adding, renaming or
  deleting a route turns `make check` red until you decide** whether an agent
  should be able to call it — `Excluded` is a fine answer, and most routes are. Do
  not delete the test to get green; the whole design is that nobody has to
  remember. See [ADR 0012 (mcp)](docs/adr/0012-the-mcp-server-and-a-forced-coverage-decision.md) and `mcpserver/README.md`.
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
- `mcpserver/` — same repo, its own distribution (`almagest-mcp`) and its own venv. The inventory as tools an agent can call: 26 curated tools over the HTTP API — against 142 operations, so 116 are `Excluded` on purpose — stdio transport, `.mcp.json` at the repo root wires it up. **A translation layer, not a second API** — whole units instead of `qty_milli`, a `{template: value}` mapping instead of the API's list of pairs, and nothing that imports `app.models` or opens the database. Writes are off unless `ALMAGEST_MCP_ALLOW_WRITES` is set, and go through the same `/api/stock/...` routes the PWA uses. Its own venv because the MCP SDK has no business in the API image, and it needs no submodules: the contract test reads the committed `openapi.json` and the tool tests drive a fake transport. **`almagest_mcp/coverage.py` is the map — read it before adding a tool.**
- `mensa/` — submodule. The bench station firmware: ESP-IDF, separate toolchain.
- `antlia/` — submodule, **public**. A Flipper Zero app that reads a container tag and types its short ID into the connected computer as a USB keyboard, so a laptop can identify a bin. Split for the same reason as `mensa/`: its own toolchain (`ufbt`, the Flipper SDK) and no coupling to the API contract — it needs no network at all. It carries a **second implementation of the short-ID codec, in C**, which is the one risk worth knowing about: `antlia/tests/vectors.h` is generated from `idcodec/` by `antlia/tools/gen_vectors.py`, so a divergence fails Antlia's CI. Change `idcodec/shortid.py` or `tagpayload.py` and you must run `make vectors` there too.
- `circinus/` — submodule. OpenSCAD/CAD; binary-ish files that would bloat every clone forever.
- `ecia-barcode/`, `elec-value-parser/` — submodules, PyPI-publishable. Names stay descriptive, not thematic — they are the only artifacts aimed at strangers. Extract via `git subtree split` (preserves history) once their tests are green; not before.

Only repos get constellation names; everything inside one stays descriptive. See [docs/NAMING.md](docs/NAMING.md).

API clients are **generated from FastAPI's OpenAPI schema**, never hand-written — that is what makes the cross-repo splits safe.

## Architecture invariants

These are non-obvious, load-bearing, and expensive to retrofit. Violating any of them is a design bug, not a style preference.

**Three-tier stock model.** `parts` (definition) → `stock_lots` (a physical package at a location) → `locations`. **Quantity lives on the lot, never on the part.** PartKeepr hung it on the part and could never support multi-location or per-batch cost. Lots are packaging-aware: a 5000-piece reel and a cut-tape strip of the same MPN in the same bin are two lots.

**The ledger is append-only, enforced by DB triggers.** `stock_ledger` rejects UPDATE and DELETE via `RAISE(ABORT)`. Undo is a compensating row with `reversal_of_seq`, never a delete. Balances **must** be read from `stock_lots.qty_milli_cached` — summing the ledger in an API path is how this design dies at 200k rows. A nightly job compares cache to `SUM(delta_milli)` and records drift into `cache_state` — it **reports and does not repair**, because a scheduled rebuild erases the write-path bug it is evidence of. See [ADR 0013 (nightly pass)](docs/adr/0013-the-nightly-pass-repairs-staleness-and-only-reports-drift.md); the repair is an explicit `POST /api/system/caches/rebuild`.

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

The manifests live in **`deploy/`** and the operational half is **[deploy/README.md](deploy/README.md)**; the *other* deployment target — the machine at the bench, all on loopback with no root — is **[deploy/station/README.md](deploy/station/README.md)**; the shape and the cluster probing behind it are in **[ADR 0009 (cluster)](docs/adr/0009-cluster-deployment-and-the-443-problem.md)**. Three things about it are easy to get wrong:

- **Images are built only by `.github/workflows/release.yml`.** There is no container runtime on the dev box, so there is no local build target and never should be a Makefile target pretending otherwise.
- **`make k8s-deploy` scales the API to zero before migrating**, then applies. That downtime is deliberate — RWO does not prevent two writers, because both pods land on the same node.
- **The cluster cannot serve 443, so the app answers on `:30443`** — no ingress controller, no LoadBalancer, `hostPort` blocked, node-port range 30000–32767. Tags are specified to carry a *portless* URL, so **provision no tags** until a reverse proxy fronts 443. Also note `--dry-run=server` accepts an out-of-range `nodePort` and the real apply then rejects it: dry-run bypasses the port allocator.
