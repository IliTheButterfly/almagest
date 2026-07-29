# Almagest — Electronic Component Sorting & Inventory System

Repo names and the naming scheme are settled in [NAMING.md](NAMING.md).

## Context

There is no system today — component storage is ad-hoc, so finding a part means opening drawers, and knowing whether a part is even owned is guesswork. The goal is a self-hosted system that answers three questions fast: *do I have this?*, *where is it?*, and *how many are left?* — plus DigiKey-style parametric search ("through-hole 20–30 µF ceramic capacitor"), instant access to datasheets, and an addressing scheme that survives adding shelves, boxes, trays and drawers indefinitely.

The single biggest risk is not technical. Every abandoned DIY project in this space (and the dead PartKeepr, whose users migrated away) died because **manual data entry didn't scale**, or because a solo maintainer drowned in an over-engineered stack. So the design is deliberately boring where it can be, and every intake path is built around getting an item recorded in seconds with curation deferred.

The checkout holds only `docs/` and config — no application code. This is greenfield.

## Decisions locked

| | |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, SQLite (WAL) + FTS5, `pint` |
| Frontend | React + Vite PWA — **mobile-first**, same build serves phone, desktop and Pi kiosk |
| Topology | API + DB in Docker on a desktop/NAS. Pi runs kiosk Chromium + a small `deviceagent` |
| Identification | **NFC primary** — NTAG213, NDEF URI = the same `/s/{short_id}` URL. QR optional per container type |
| Barcode scanning | **In-browser** via `getUserMedia` + `zxing-wasm` — for distributor labels at intake, on the phone |
| Containers | **v1: off-the-shelf drawer cabinets** — label card in the molded slot + NFC tag on the drawer underside. Printed Gridfinity later |
| Repo | Master repo (**Almagest**) + submodules. **Mensa** (station firmware) and **Circinus** (CAD) split now; `ecia-barcode` and `elec-value-parser` extracted when stable. `backend`+`frontend`+`deviceagent` stay together |
| Station | Fixed bench station: bottom-tag NFC (auto-identify on placement) + scale + backlit vision counting |
| Scale | TAL221 100 g cell + SparkFun Qwiic Scale (NAU7802, I²C) — ~$60 |
| Counting camera | Pi HQ Camera (IMX477) + 16–25 mm lens at ~300 mm over the backlit tray |
| Printing | **Abstraction layer**, no hardware locked in. Bin label cards are laser/inkjet cardstock |
| In scope | BOM/projects with reservations; non-component inventory (tools, screws, cables) |
| Deferred | LED locator, multi-user, UHF RFID (ruled out) — all additive, no migration needed |

**Why the Pi is thin.** Barcode decode happens in the browser, identically on a phone and on the kiosk. `deviceagent` exists only for what a browser sandbox cannot reach: the PN532 NFC reader, the counting camera, and the ESP32 that owns the scale ADC and LEDs. Web Serial was evaluated as a way to remove `deviceagent` entirely and declined — see the station section.

## Reading order

The document grew by topic rather than by build order. To read it as a build plan: **Repository structure → Data model → Capacity and auto-assignment → Identification → Physical layer → Layout authoring and tag provisioning → Label printing → Barcode scanning → Parametric search → Core workflows → Phasing → Verification.** The station, vision counting, datasheets/enrichment and the IC marking reader are later phases and can be read last.

## Repository structure

A master repo (**Almagest**) with submodules, but split only where coupling is genuinely absent. Names and rationale: [NAMING.md](NAMING.md).

**Split out** (own repos, pinned as submodules):

| Repo | Why split |
|---|---|
| `mensa` | The bench station firmware. ESP-IDF; entirely separate toolchain, changes rarely |
| `circinus` | OpenSCAD/STL/CAD — binary-ish files that bloat *every* clone forever, since git keeps all history |
| `ecia-barcode` | The MH10.8.2 / EIGP-114 parser. **No mature parser exists on PyPI** — genuinely publishable, and the first of its kind |
| `elec-value-parser` | The `4k7` / `0R22` / `2M2` shorthand parser. Zero coupling, broadly useful |

**Kept together** — `backend`, `frontend` and `deviceagent` live in one repo, because all three are bound by the API contract. Changing a route signature touches all of them, and that should be one atomic, CI-verified commit rather than a three-repo dance with version skew.

**What makes the splits safe:** the API clients are **generated from FastAPI's OpenAPI schema**, never hand-written, so the contract is machine-checked across repo boundaries.

**Submodule tax, acknowledged and mitigated:** detached HEADs and un-bumped pointers are the classic failure. Mitigate with `git config submodule.recurse true`, a `make bootstrap` that clones and initialises everything, and CI that fails if a submodule pointer is not on a tagged commit.

**Counter-argument, recorded deliberately:** on a greenfield project the boundaries are guesses, and splitting on day one hardens a guess into structure. `git subtree split` extracts a clean repo **with full history** at any time — so the two library extractions can happen the moment their test suites go green, and need not be decided up front. Start with `mensa` and `circinus` split (those boundaries are certain), and extract the two libraries when they stabilise.

```
almagest/                  master: docker-compose, docs, ADRs, Makefile, submodule pins
  backend/
    app/
      api/routes/         parts, search, scan, stock, storage, projects,
                          datasheets, labels, scale, system
      models/             storage.py stock.py parameter.py catalog.py scanning.py
      services/
        shortid.py        Crockford base32 + mod-37 check
        tree.py           TreeRepository (shared by locations + categories)
        capacity.py       capacity strategies, dimension cascade, occupancy
        assignment.py     hard filters, scoring, escalation, defrag
        search/           value_parser.py query_builder.py
        enrichment/       providers/ mpn_decoders/ extract.py
        labels/           render.py backends/ spec.py
        scale.py          counting math, calibration, tare
        counting/         segment.py estimate.py fuse.py calibrate.py
      db/                 session, pragmas, migrations glue
    alembic/versions/
    tests/{unit,integration,fixtures,fakes}
  frontend/src/{routes,components,lib/{scan,nfc,api}}
  deviceagent/            nfc_pn532.py, esp32_serial.py (scale+LEDs),
                          camera.py, station.py (state machine), outbox.py
  data/{almagest.db, datasheets/, backups/}
  docker-compose.yml

  mensa/                  ← submodule: station firmware. ESP-IDF, NAU7802 +
                            WS2812B + ERM, USB-serial line protocol
  circinus/               ← submodule: CAD. gridfinity generate_stl.py (OpenSCAD
                            driver), label_cards.py, station/ (plinth, gantry,
                            platform, tray)
  ecia-barcode/           ← submodule, PyPI-publishable: MH10.8.2 parser + DI table
  elec-value-parser/      ← submodule, PyPI-publishable: electronics shorthand grammar
```

## Data model

Three-tier core, the pattern every mature system converged on: **`parts` (definition) → `stock_lots` (a physical package at a location) → `locations`**. Quantity lives on the lot, never on the part — PartKeepr hung it on the part and could never support multi-location or per-batch cost.

**Conventions that matter later:**
- Quantities are `*_milli INTEGER` (thousandths of the part's UoM) so ledger sums are exact forever. Money is `*_micro INTEGER` + currency. Timestamps are ISO-8601 UTC text.
- **No `CHECK` enums and never `sa.Enum`** (which silently emits `VARCHAR + CHECK`). SQLite cannot alter a `CHECK`, so a `CHECK` enum turns "add a new kind" into a table rebuild. Validate with Python `StrEnum` at the model layer. This one rule is what makes every deferred feature purely additive.
- Alembic configured `render_as_batch=True`. Pragmas: `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`.

### Storage tree

`locations` is an **adjacency list (`parent_id`) plus cached `depth`, `id_path`, `label_path`**, rebuilt by one recursive CTE. Not nested sets (subtree moves renumber the table), not a closure table (machinery with no payoff at ~10³ nodes when SQLite has recursive CTEs). `id_path` uses numeric ids so renaming never invalidates prefix queries; subtree filtering is `id_path LIKE :prefix || '%'`, index-served. The cache is 100% reconstructible from `parent_id`, so a cache bug is never data loss. Ancestors of X = rows whose `id_path` is a prefix of X's — no recursion needed.

Cycle guard must run before any move (`CHECK` only catches self-parenting):
```sql
SELECT EXISTS (SELECT 1 FROM locations WHERE id = :new_parent AND id_path LIKE '%/' || :moved || '/%');
```

`part_categories` is the *same* structure and a **separate tree** — logical taxonomy is not physical storage. Implement once as a mixin + `TreeRepository` parameterized by table. `parts.category_id` is **nullable**: mandatory taxonomy is an entry tax paid in abandoned scans.

### Container types are data

`container_types(child_layout: grid|list|none, grid_rows, grid_cols, capacity_model, inner dims, default_fill_factor, capacity_slots, max_parts_per_slot, esd_safe, is_placeable, full_threshold, ...)`. Shelves, boxes, trays, drawers, bags, reel racks and rooms are all the same entity, and a new container kind is a row, not a migration.

Plus `container_type_slot_templates(slot_label, row_idx, col_idx, row_span, col_span, size_class, inner_volume_mm3)` — real assortment boxes have mixed compartment sizes (4 large + 12 small), and pure rows×cols breaks the first time you buy one. The grid case is a *generated* template.

Capacity *models*, by contrast, are Python strategy classes selected by the `capacity_model` string. Resist ever storing a capacity formula as a DB string — that's the over-engineering that made the prior art unmaintainable.

### Addressing and short IDs

**Crockford base32** (`0123456789ABCDEFGHJKMNPQRSTVWXYZ` — drops `I`,`L`,`O`,`U`), **7 data symbols + 1 mod-37 check symbol**, rendered `4K7T-92M8` (hyphen cosmetic). Input normalizes `O→0`, `I/L→1`. Mod-37 detects all single-symbol substitutions and all adjacent transpositions — the two errors humans make reading a label. Crockford's check alphabet uses `*~$=U` for values 32–36, which are font-fragile; instead **rejection-sample** (discard ~13.5% of candidates whose check value ≥32) so the printed string stays inside the 32-symbol alphabet.

**One shared ID space across all object types**, via `object_ids(short_id PK, entity_type, entity_pk, is_primary)`. A scan needs no context to resolve, and an object changing type doesn't invalidate a printed label. Readability is recovered by rendering the type as a *display prefix* (`BIN 4K7T-92M8`) that is never parsed or stored. At ~5×10⁴ objects the birthday collision probability over 32⁷ is ~3.6% — and since `short_id` is a PK, a collision is *detected*, costing one retry, not silent corruption.

**Hierarchy is never encoded in the printed code.** The moment a box moves shelves, an encoded label is a lie. `label_path` is derived and always fresh — which also fixes InvenTree's most-complained-about gap (it shows only the leaf name and hides the full path). Labels carry the QR, the ID, and the object's **name**; never the path, because names travel with the object and paths don't.

**`locations.short_id` is nullable.** Auto-generated grid slots don't get printed IDs — nobody will ever stick 96 labels on an 8×12 box. Scan the box, tap the cell; address as parent ID + slot label (`BIN 4K7T-92M8 / C-07`). Any slot can be promoted to a printed ID on demand.

### Stock and the ledger

`stock_lots(part_id, location_id, packaging_code, pack_nominal_qty_milli, batch_code, serial, date_code, supplier_part_id, unit_cost_micro, status, qty_milli_cached, qty_reserved_milli_cached, slots_occupied, volume_mm3_cached, container_tare_mg, opened_at, expires_at, retired_at)`.

Lots are **packaging-aware**: a 5000-piece reel and a cut-tape strip of the same MPN in the same bin are two lots, independently costed. `expires_at` covers solder paste, batteries and electrolytics.

`stock_ledger` is **append-only** and is the system's spine: `seq AUTOINCREMENT, ts, lot_id, part_id, kind, delta_milli, qty_after_milli, from_location_id, to_location_id, counted_qty_milli, unit_cost_micro, ref_type, ref_id, group_uuid, actor_id, source, measured_mass_mg, reversal_of_seq, client_op_id, note`. Immutability is enforced **by triggers, not convention** — a future "fix the typo" script is otherwise inevitable:
```sql
CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON stock_ledger
BEGIN SELECT RAISE(ABORT,'stock_ledger is append-only'); END;
-- and the matching BEFORE DELETE
```
Undo is a compensating row with `reversal_of_seq`. Balances **must** be cached in `qty_milli_cached` with a single-statement rebuild plus a nightly drift check into `cache_state` — summing the ledger in an API path is how this design dies at 200k rows. The cache is deliberately *not* constrained non-negative: a bad recount must surface as a dashboard anomaly, not block the ledger write.

**Move semantics.** `location_id` is mutable; history lives in the ledger. A whole-lot move is **one** row (`kind='move'`, `delta_milli=0`, from/to). A partial move is **two** rows sharing a `group_uuid` (`split_out` −N, `split_in` +N). The alternative — minting a new lot per shelf change — would destroy lot identity and per-lot cost continuity. Document this deviation in the code.

### Parameters

One table, discriminated by `value_type`, combining InvenTree's cached-numeric (fast filtering) with PartKeepr's normalized columns (lossless display):

```sql
CREATE TABLE parameter_template (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
  value_type TEXT NOT NULL,              -- numeric|enum|bool|text
  base_unit TEXT,                        -- pint-parseable: 'farad','ohm','volt'
  applies_to_category TEXT,
  substitution_direction TEXT NOT NULL DEFAULT 'exact',  -- higher_ok|lower_ok|range_overlap|exact
  sort_order INTEGER NOT NULL DEFAULT 0);

CREATE TABLE parameter_choice (id INTEGER PRIMARY KEY, template_id INTEGER NOT NULL,
  key TEXT NOT NULL, label TEXT NOT NULL, sort_order INTEGER, UNIQUE(template_id, key));

CREATE TABLE parameter_value (
  id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL, template_id INTEGER NOT NULL,
  raw_input TEXT NOT NULL,                          -- '4k7', '20-30uF' exactly as entered
  value_nominal REAL, value_min REAL, value_max REAL, value_typ REAL, tolerance_pct REAL,
  display_mantissa REAL, display_si_prefix TEXT, display_unit_symbol TEXT,
  choice_id INTEGER, value_text TEXT,
  provenance TEXT NOT NULL DEFAULT 'manual', confidence REAL,
  UNIQUE(part_id, template_id));
CREATE INDEX ix_pv_tmpl_num ON parameter_value(template_id, value_nominal) WHERE value_nominal IS NOT NULL;
CREATE INDEX ix_pv_tmpl_ch  ON parameter_value(template_id, choice_id)     WHERE choice_id IS NOT NULL;
```

`UNIQUE(part_id, template_id)` is load-bearing: each join contributes at most one row, so multi-predicate queries use plain `JOIN`s that never fan out. Enum facets (dielectric, mounting, package) live in the *same* table via `choice_id`, so search, provenance and review have one code path.

**EAV vs JSON, decided:** at a few thousand parts (`parameter_value` ≈ 30–150k rows) indexed EAV with 3–5 joins is sub-millisecond in SQLite. `json_extract` + generated columns doesn't remove the schema churn — a new *filterable* field still needs a generated column and a partial index — it just relocates it and loses FK integrity to `parameter_template`. EAV wins. Reserve `parts.extra_specs_json` for unmapped provider fields that are never filtered.

`part_kinds`, `package_types` (dimension + mass defaults), `packagings`, `units`, `tags`, `part_substitutes`, `manufacturers`, `suppliers`, `supplier_parts`, `supplier_price_breaks` round out the catalog.

### Other tables

- `documents(sha256 UNIQUE, kind, mime, byte_size, page_count, source_url, storage_path)` + `document_links(document_id, entity_type, entity_pk, role)` — content-addressed, M:N because one family PDF covers many MPNs.
- `barcode_aliases(code_norm, symbology, entity_type, entity_pk, alias_kind, parsed_json, hint_qty_milli, hint_batch, hit_count)` — user-taught bindings. May target a `supplier_part`, which is what makes a DigiKey reel label resolve to part + quantity + PO in one scan. `symbology` is free text, so `nfc_uid`/`nfc_ndef` need no DDL.
- `scan_events(ts, source_id, symbology, raw_payload, payload_sha256, decoded_kind, resolved_*, action_taken, latency_ms)` — keep raw payloads so unparsed vendor formats can be mined later instead of lost.
- `client_operations(client_op_id PK, device_id, endpoint, request_hash, status, response_json)` — idempotency. **Not cheaply retrofittable**, because retry semantics touch every write path.
- `projects`, `project_builds`, `bom_lines` (nullable `part_id` so an imported KiCad BOM lands intact), `bom_line_substitutes`, `stock_allocations(build_id, bom_line_id, part_id, lot_id, qty_milli, state)`. `available = qty_milli_cached − qty_reserved_milli_cached`, where the reserved cache is **derived** from allocations and rebuildable — a hand-maintained reservation counter drifts and cannot be reconstructed.
- `location_occupancy`, `layout_suggestions` (so dismissals stick), `cache_state`, `settings` (assignment weights live here, so tuning isn't a deploy), `label_prints`, `scan_sources`, `locator_devices`/`locator_channels` (LED, leaf tables, nothing references into them).
- FTS5: `part_fts(mpn, description, manufacturer, keywords, param_digest)` and `datasheet_fts(text)` as **separate** tables — one datasheet serves many MPNs, so duplicating its text per part would bloat the index. `param_digest` is a catch-all column included from day one because FTS5 external-content tables must be dropped and recreated to change their column set.

## Capacity and auto-assignment

Capacity is **advisory, never a hard constraint.** A put-away is never blocked; the bin is flagged `is_overfull` and a defrag suggestion is generated. Blocking scans teaches the user to stop scanning.

| `capacity_model` | capacity | used | full when |
|---|---|---|---|
| `none` (shelf, room) | — | informational | never |
| `slots` (24-compartment box) | slot count | occupied slots | no free slot accepts this part |
| `volume` (bin, bag) | `usable_volume × fill_factor` | Σ lot volume | `fill_ratio ≥ full_threshold` |
| `positions` (reel/tube rack) | position count | Σ `ceil(pkg_width / pitch)` | positions exhausted |
| `mass` | reserved for later | | |

Lot volume is **packaging-aware**, which handles reels without a separate regime: if the packaging has its own `package_volume_mm3` (reel/tube/tray/bag) the lot occupies *that*, whether it holds 5000 parts or 12. Otherwise it's `unit_volume × qty`.

Item dimensions resolve through a cascade, and the winning rule is recorded in `parts.volume_source` so the UI says *"estimated from package 0603"* rather than lying with a precise-looking number: **override → L×W×H×shape_factor → package_type default → category default → size-class constant** (tiny 2 mm³ / small 30 / medium 300 / large 3 000 / bulky 30 000). `fill_factor` (default 0.55) is a property of the *container* — screws pack tighter than TO-220s — and is calibratable later from observed "user says full at X" data.

Occupancy is cached, marked `dirty` by triggers on ledger insert and lot relocation for the affected location **and all ancestors** (so "this shelf is 80% full" works). Full rebuild of every row is a sub-second query — that's the escape hatch.

**Assignment** = hard filters, then a weighted score whose weights live in `settings`:

```
score(L) = w_consol · consolidation(L)   # part already has a compatible lot here
         + w_afty   · affinity(L)        # Wu-Palmer similarity over the category tree,
                                         #   computed from id_path prefixes, no recursion
         + w_fit    · fit(L)             # PEAKED: exp(-((fill-0.70)^2)/0.08)
         + w_access · access(L)          # L.access_score × part.hot_score
         + w_home   · homing(L)
         - w_frag   · 0.25 · (locations already holding this part)
         - w_depth  · (L.depth / max_depth)
```

Hard filters: `is_placeable`, ESD (resolved by walking ancestors — nearest non-NULL wins, so marking a whole cabinet ESD-safe is one edit), `allowed_part_kinds`, max item dimension, packaging compatibility, free compartment when one-part-per-slot, not already overfull.

`fit` is **peaked, not monotonic**: pure best-fit creates unusable slivers, pure worst-fit burns prime real estate on one resistor. `hot_score` is refreshed nightly as `Σ exp(−age_days/45)` over consume events, normalized. Ties break deterministically on `(−free_capacity, short_id)` — the same input must always propose the same bin or the user stops trusting it.

**When nothing fits, escalate — never error.** Drop soft preferences → materialize an unused grid cell → propose the cheapest defrag move plan for one-tap confirmation → propose a new sibling container → fall back to a permanent `INBOX` staging location with a "N items unsorted" dashboard counter. **A scan is never rejected.**

**Defrag pass** writes to `layout_suggestions`: `merge_lots` (same part in ≥2 locations), `merge_bins` (two same-type siblings both under 40% with affinity > 0.6), `promote_hot`/`demote_cold` (bounded to the top 20 hottest parts), `retire_empty`, `overfull`. Each carries a move plan the apply endpoint replays as ordinary ledger moves, so a defrag is fully undoable.

## Barcode scanning — browser-first, for distributor labels

Containers identify themselves by NFC, so camera scanning has exactly one job left: **reading vendor barcodes at intake** (reel and bag DataMatrix, Code128, EAN). That happens on the **phone**, where autofocus is available — which removes the hardest constraint, since a phone camera at 8–10 cm reads a 10 mm dense DataMatrix far more reliably than a fixed-focus webcam ever would.

Decode runs **in the browser**: `getUserMedia` + `zxing-wasm`, enabling exactly four symbologies (QR, DataMatrix, Code128, EAN-13) because each additional format costs a finder-pattern pass per frame. Center-ROI crop, 3-frame voting (accept on 2-of-3 identical), and a 3-second payload-hash hold-off so one label held in front of the camera doesn't fire five resolves.

If bulk intake of many reels ever gets tedious, a **USB HID wedge scanner needs no code at all** — it's a keyboard. A focused input field plus a timing heuristic (>50 chars/sec with a terminator distinguishes a scanner from typing) covers it, so buying one stays a config change rather than a rewrite.

### Resolver chain

One endpoint, `POST /api/scan/resolve`, with ordered handlers, first match wins (InvenTree's pattern): **internal short ID → `barcode_aliases` → ECIA/MH10.8.2 DataMatrix → LCSC proprietary → bare-MPN heuristic → EAN/UPC digits → unknown**.

`status` is `resolved` | `ambiguous` (with candidates) | `unknown`. On `ambiguous`/`unknown` the response carries `parsed` fields and `suggest_bind: true`, and the UI offers "bind this code" → `POST /api/scan/alias`. That payload then resolves at step 2 forever. This is the **alias-learning** feature and it generalizes PartsBox's "ID Anything" from self-minted QRs to arbitrary vendor payloads.

### ECIA / MH10.8.2 parser

Distributor labels are a genuine shared standard (EIGP-114.2018 over ANSI MH10.8.2) used by DigiKey, Mouser, Arrow, Newark/Farnell, Avnet, TTI. No mature PyPI parser exists — this is ~150 lines in `backend/app/services/scanning/ecia.py`.

Envelope: `[)>` + RS(0x1E) + `06` + GS(0x1D), fields GS-separated, terminated RS + EOT(0x04). DI table: `P`=customer PN, `1P`=supplier PN, `30P`/`2P`=revision, `1T`=lot, `Q`=quantity, `9D`/`10D`=date code, `4L`=country, `K`/`4K`=PO, `S`=serial, `1V`=manufacturer, `33P`=bin. **Match longest DI first** — they overlap as prefixes (`1P` vs `P`).

Edge cases, all non-fatal: Mouser's malformed `>[)>06` header (strip stray leading `>`, re-anchor); missing envelope (split on GS anyway, warn); missing terminator (camera cropping — parse what's there, lower confidence); repeated DIs (store as a list); embedded GS in a value (validate against per-DI regex, attempt to re-glue). LCSC is *not* standard-compliant — separate handler, reverse-engineered from samples.

Fixtures are the ground truth since there's no reference implementation to diff against: `tests/fixtures/ecia/*.bin` (raw bytes) + hand-verified `*.expected.json`, collected from real incoming reels, plus synthesized adversarial cases.

## Parametric search

### Value parser — the highest-risk module

`backend/app/services/search/value_parser.py`. `pint` does not parse electronics shorthand; this preprocessor runs first and **only with template context**, because the same text is meaningless without knowing the physical quantity.

```
value      := comparison | range | scalar
comparison := ('>='|'≥'|'<='|'≤'|'>'|'<') scalar
range      := scalar ('-'|'..'|'~') scalar
scalar     := sign? mantissa tolerance?
mantissa   := infix | digits ('.' digits)? sp? si_prefix? unit?
infix      := digits infix_letter digits?          -- 4k7, 2M2, 0R22, 10n3
infix_letter := p|n|u|µ|m|k|M|G|R                  -- R/r = ×1, resistor convention
tolerance  := ('±'|'+/-') digits '%'
```

Ambiguity is **never guessed**. Case-sensitive per SI (`m`=milli, `M`=mega), cross-checked against `template.base_unit`: bare `1M` under `resistance` → 1 MΩ; the identical text under `capacitance` is **rejected to the review queue**, because megafarads aren't real. Per-template plausibility guards (capacitance ∈ [1 pF, 1 F]) back this up independently.

`0603` vs `1608` is not a unit problem — it's a dual-notation enum. Seed `parameter_choice` with composite keys (`key='0603_1608'`, `label='0603 (imperial) / 1608 (metric)'`), so either string hits the same row and **the user is never asked which convention a source used**.

Ranges (`20-30uF`) fill `value_min`/`value_max` and leave `value_nominal` NULL — matched by interval overlap, not equality to a midpoint. Comparisons (`≥50V`) fill `value_min` only.

### Query

`POST /api/search/parts` takes a filter list; a GET querystring alias runs the same parser so URLs stay pastable. "Through-hole 20–30 µF ceramic capacitor" becomes:

```sql
SELECT p.id, p.mpn, p.description
FROM part p
JOIN category c ON c.id = p.category_id AND c.slug = 'capacitor'
JOIN parameter_value pv_m ON pv_m.part_id = p.id
  AND pv_m.template_id = (SELECT id FROM parameter_template WHERE name='mounting_type')
  AND pv_m.choice_id = (SELECT id FROM parameter_choice WHERE template_id=pv_m.template_id AND key='THT')
JOIN parameter_value pv_t ON pv_t.part_id = p.id
  AND pv_t.template_id = (SELECT id FROM parameter_template WHERE name='capacitor_technology')
  AND pv_t.choice_id = (SELECT id FROM parameter_choice WHERE template_id=pv_t.template_id AND key='ceramic')
JOIN parameter_value pv_c ON pv_c.part_id = p.id
  AND pv_c.template_id = (SELECT id FROM parameter_template WHERE name='capacitance')
  AND pv_c.value_min <= 30e-6 AND pv_c.value_max >= 20e-6;
```

FTS composes by **filtering first** (cheap, indexed, a few hundred candidates) then ranking within that set via `bm25()` over `part_fts` and `datasheet_fts`, the datasheet hit dampened ×1.5. No free-text term → skip FTS entirely, order by stock then name.

**Substitution search** reuses the identical filter list; only the operator per predicate changes, read off `parameter_template.substitution_direction` — `higher_ok` (voltage rating), `lower_ok` (tolerance), `range_overlap` (capacitance), `exact` (package). One filter executor with a `mode: search|substitute` flag; there is no second query engine.

## Datasheets and enrichment

**Provider interface** (Part-DB's pattern), one Protocol with `lookup(mpn, manufacturer)` returning `ProviderResult{fields: {name: ProviderField{value, confidence, source}}, datasheet_url, raw_payload}`. Implementations: `MouserProvider` (free single API key, no OAuth — start here), `LcscJlcpartsProvider` (local SQLite dump from the `jlcparts` project, zero network at query time), `DigiKeyProvider` (OAuth2 + one-time manual app approval, ~1000 req/day), `NexarProvider` (pricing/lifecycle only — datasheets are gated out of its mid tier), `ManualProvider` (priority 0, always wins).

Enrichment **never writes `parameter_value` directly** — it writes `parameter_value_candidate`. Promotion auto-accepts only when the field is empty and single-source with confidence ≥ 0.8, or when sources agree within tolerance. Disagreement priority: `manual > datasheet_table > mpn_decoder > distributor_freetext > llm_inferred`. A manufacturer's own printed table beats an API's marketing copy. `provider_cache(provider, mpn_norm, fetched_at, ttl_days, payload_json)` with a 90-day TTL protects brutal quotas.

**Datasheet store** is content-addressed: `data/datasheets/{sha256[0:2]}/{sha256[2:4]}/{sha256}.pdf`, git-style fanout, hash computed before write so dedup is free. External datasheet URLs rot within a few years — the local cache is not optional. `GET /api/datasheets/{sha256}` streams inline for the browser's native PDF viewer; `GET /api/parts/{id}/datasheet` redirects to the primary.

**QR-to-datasheet** works via the label's existing short ID (`https://sorting.ts.net/s/4K7T92M8` → part detail → datasheet one tap). Off-LAN, `sorting.local` won't resolve over cellular — put devices on a **Tailscale tailnet** (also solves the PWA's HTTPS-secure-context requirement for camera access, with real certs and no CA distribution). Zero-dependency fallback: print the bare MPN as text under every QR so a manual manufacturer-site search always works.

**Extraction pipeline:** fetch → **Docling** (Apache-2.0, TableFormer; handles multi-column electronics tables far better than pdfplumber out of the box) → fall back to pdfplumber + tesseract only when extracted-chars-per-page ≈ 0, which is itself the signal to flag low confidence → LLM structured extraction against a JSON schema, sliced to the MPN's section or batching a whole variant table in one call → **MPN-decoder cross-check** → confidence score → review queue below 0.8 or on disagreement. Cost is ~$0.0005–0.001 per part batched; the whole 1000-part backfill is under $2.

**MPN decoders** (`decode(mpn) -> dict`, registry, most-specific-prefix-first): Murata GRM, Samsung CL, Yageo CC/RC, TDK C-series, and generic SMD resistor codes (3-digit/4-digit/EIA-96, implemented directly — **the `resistors` PyPI package cannot serve this and the earlier claim that it could was wrong**: it is 121 lines of colour-band decoding with no mention of EIA-96, SMD or numeric markings at all, and it ships a `gray`/`grey` key mismatch that raises `KeyError` on a grey band. The three formats are ~60 lines and need no dependency; the EIA-96 table is *generated* as `round(100 * 10**(k/96))` and pinned against all 96 published values, which removes the transcription risk). No generic capacitor decoder exists anywhere — these five families are deliberate pragmatic coverage.

**Minimum-viable free path: Mouser + jlcparts + MPN decoders + manual entry, $0 hard cost.** Layer DigiKey once approved. Treat Nexar and LLM extraction as pure upside.

## Local GPU inference — where it helps and where it doesn't

The cluster node has an RTX 4090 (24 GB), and `nvidia.com/gpu: 1` is confirmed schedulable. Three candidate uses, with sharply different value:

**1. Semantic part discovery — embeddings only, no LLM.** The valuable job is the *fuzzy front door*: "something to level-shift 3.3 V to 5 V" when the user does not know the parametric query to write. An embedding model over part descriptions plus the already-indexed datasheet text gives semantic recall that complements FTS5's lexical matching. Needs **<2 GB VRAM and no LLM**, making it the cheapest and highest-value GPU use here. **No vector database** — at a few thousand parts, brute-force cosine in numpy is sub-millisecond; `sqlite-vec` if it must live in-DB.

**Invariant: substitution search stays deterministic and must never be delegated to a model.** `parameter_template.substitution_direction` returns correct answers by construction; an LLM returns plausible ones, and a plausible substitute with the wrong voltage rating is a field failure. Embeddings may *suggest candidates to explore*; only the SQL filter decides what actually satisfies a requirement.

**2. Datasheet extraction — the strongest case, but not for cost.** Local inference saves nothing: the API path is already budgeted at **under $2 total** for ~1000 parts at Flash tier. What it buys is **unlimited re-runs while tuning prompts** — where rate limits genuinely bite — plus overnight batch throughput. Expect a 4-bit ~30B local model to be *worse* than a frontier Flash-tier model on messy multi-column tables; the existing candidate/confidence/MPN-cross-check pipeline turns that into graceful degradation (more review-queue items) rather than bad data. Design: **local first pass, frontier API as escalation for low-confidence items.**

**3. Auto-filing repo issues — deterministic health checks first, LLM optional.** Unsupervised "suggest improvements" generation produces low-signal noise that buries real items, and contradicts the rule that LLM output is never auto-accepted. The always-actionable items are *derived from data, not opinion*, and the schema already computes them: `cache_state` drift (ledger vs cached balance — a genuine correctness alert), pending `layout_suggestions`, `is_stub=1` parts older than N days, failed datasheet extractions, locations with stale `last_verified` (cycle-count debt), and `verification_mismatches` from tag provisioning. Those are SQL in a CronJob. An LLM's only marginal value is phrasing and grouping them; if used, it proposes into a review queue and a human promotes to issues. (Prerequisite: no git remote exists yet.)

**Deployment constraints.** The namespace has **no ResourceQuota**, so an unbounded model server can starve the co-tenant builder — set explicit limits. And a free GPU unit is a **race, not a reservation**: run the extraction model as a **Job/CronJob that releases the device**, and keep only the small embedding model as an always-on Deployment. Embeddings always-on, LLM on-demand, is what makes sharing the 4090 workable. Serving: vLLM for batch throughput, Ollama/llama.cpp for simplicity.

## The station — identify, weigh, count

The station replaces most handheld scanning. A container is carried to it, set down, and **identifies itself** — because the NFC tag is on its underside and the reader antenna is under the platform, there is no scanning gesture at all. The station then weighs it, and a separate backlit tray next to it counts parts by vision.

**Fixture** (all printed): a ~220 × 220 mm plinth taking up to a 4×4 Gridfinity footprint. The platform is a **thin (≤4 mm) printed PETG plate** cantilevered off a TAL221 beam. The PN532 antenna sits directly under the platform centre, aligned with where a bottom-pocket tag lands.

**The NFC/load-cell conflict, resolved by keeping metal out of the field.** Metal near an NFC antenna detunes it via eddy currents — that's why on-metal tags need ferrite. So: the platform is **printed, not sheet metal**, and the TAL221's small aluminium beam is offset 30–40 mm to the side (its normal cantilever mounting), never between tag and antenna. The tag then sits ~8–12 mm above the antenna — comfortably inside a PN532's 30–50 mm open-air NTAG213 range, no ferrite needed. If a metal platform is ever added for rigidity, that is the point to insert a ~$3–5 ferrite sheet.

**Sensor ownership.** The **ESP32 owns the NAU7802** (keeps the I²C run under 10 cm), the LED backlight, and the vibration motor — LED timing is something non-realtime Linux cannot guarantee cleanly, and this plays to existing ESP-IDF experience. It reports to `deviceagent` over USB-serial with a trivial line protocol. The **Pi keeps the PN532** (UART) and the camera (CSI must be on the Pi).

**Web Serial is evaluated and declined** as a `deviceagent` replacement. Chromium desktop does support `navigator.serial` behind a user gesture over HTTPS, so the kiosk PWA *could* talk to the ESP32 directly — but `deviceagent` has to exist regardless for the camera and PN532, Web Serial is Chromium-desktop-only (so no phone reuse), and splitting hardware ownership between browser-privileged and daemon-privileged code is more brittle than one coherent event stream over the existing loopback WebSocket.

**Scale hardware: TAL221 100 g cell (~$10) + SparkFun Qwiic Scale / NAU7802 (~$13–30)** over I²C. I²C matters — the kernel driver owns the transaction, whereas bit-banging HX711 from Python is corrupted by scheduler preemption. Practical noise floor ~20–50 mg. Station BOM beyond the Pi ≈ **$120–150** bought (cell, ADC, PN532, camera, LEDs, ERM motor, ESP32, magnets/pins) plus filament.

**Honest limits, surfaced in the UI:** viable down to ~150–250 mg/unit — through-hole resistors and caps, connectors, screws, modules. A 0402 (0.7 mg) and 0603 (2 mg) are **at or below the noise floor and are refused outright**, no matter how much averaging is applied, because the dominant error is the cell's own nonlinearity/hysteresis, not random noise.

**Differential weighing is the primary mode**, absolute recount secondary. Weigh → take parts → weigh again; `Δmass / unit_mass` gives units removed. This eliminates the container tare from the formula entirely, which is what makes it robust: a reel's tare is *not* a fraction of its parent's after a split (leader, trailer and splice tape are uneven), so absolute mode silently corrupts on `split_out` while differential mode simply doesn't care.

```
σ_ΔM² = σ_before² + σ_after²                    (differential)
σ_ΔM² = σ_reading² + σ_tare²                    (absolute)
n̂     = ΔM / m̄
σ_n̂   ≈ sqrt( (σ_ΔM/m̄)² + (n̂·σ_m/m̄)² )
```

Decision rule on `k = m̄ / σ_reading`: **refuse** if `k < 3`, `unit_mass_mg IS NULL`, or `ΔM < 3σ_ΔM`; **confirm** if the 95% CI spans ≥2 integers; **auto-commit** only when the whole CI rounds to one integer (still writes `source='scale'` and stays undoable).

Calibration: hand-count N≥20 (ideally 50–100), weigh, insert `unit_mass_samples`; `parts.unit_mass_mg` is a Welford running mean weighted by N, so later larger calibrations dominate early small ones. Show old-vs-new before applying, so one bad calibration can't silently corrupt the baseline.

**Ledger says 500, scale says 380 — never auto-resolve.** If `unit_mass_mg` has a tight multi-sample history and the location has a measured tare, the count is the suspect → prompt a recount. If it's the first weighing or the mass has few samples, the mass baseline is the suspect → prompt a fresh 20-unit hand-count from *this* bin; disagreement beyond tolerance is evidence of a wrong reel or mislabel, not a ledger error.

Tare belongs to the **physical container**, so it lives on `locations.tare_mg` (+ `tare_sigma_mg`, `tare_measured_at`, `tare_source`); `stock_lots.container_tare_mg` is a cache. A split forces a fresh tare capture, never a computed one.

A scale is **not** a `ScanSource` — it emits a continuous numeric stream needing stateful filtering, not discrete identifiers for the resolver. Sibling abstraction `WeightSource` in `deviceagent`, with a settling detector (ring buffer of W samples; stable when `max−min < 3r` and buffer full and ≥300 ms since the last jump; 8 s timeout), always reporting canonical integer `mass_mg` and never underreporting `sigma_mg`. WS messages: `weight.reading` / `weight.stable` / `weight.timeout` / `weight.error` / `weight.zeroed`. Scale absent → no `weight.*` ever emitted → the PWA hides every by-weight affordance. No special-casing.

New tables: `weighings(session_id, phase, lot_id, location_id, mass_mg, sigma_mg, window_n, settle_ms)` and `unit_mass_samples(part_id, lot_id, n_units, gross_mass_mg, tare_mg, unit_mass_mg, sigma_mg, accepted)`.

## Vision counting

**The reframe that makes this tractable: count the handful you removed, not the bin.** Vision is ~99% accurate on a monolayer of one part type up to roughly **150–250 parts per frame**. A bin of 3000 loose 0402s is not countable by *any* method here. But the question is never "how many are in the bin" — it is "how many did I take", which is small by construction, and a small handful poured onto a tray is a monolayer for free. Segmentation error grows with density, so counting the delta is strictly more robust than re-counting the whole bin.

This is why vision and the scale are worth having together — they fail in opposite places. The scale is blind below ~150 mg (0402, 0603); vision is *best* on exactly those small, uniform, high-contrast parts. Neither handles a poured pile of thousands, and the UI must say so rather than produce a falsely precise integer.

**Backlit tray**: ~150 × 100 × 15 mm printed shell, **clear/natural PETG floor (1.5 mm) plus a removable paper diffuser** over an LED panel — paper diffuses evenly, PETG gives a wipeable surface. It registers repeatably via two 3 mm dowel pins plus two 6 × 2 mm magnets pulling it against a fixed stop, so the camera's mm-per-pixel calibration stays valid between sessions. **Critical dimension: interior depth only ~1.3–1.5× the tallest expected part**, so a second layer visibly overflows the rim.

**Two lighting modes are required.** Diffuse backlight gives the cleanest silhouettes for opaque dark passives — but **backlighting does not solve shiny metal**: scattered light makes screws and leads silhouette *smaller* than they are. So the fixture also carries a diffuse printed dome + LED ring for reflective parts, selected per session by declared part type.

**Camera: Raspberry Pi HQ Camera (IMX477, 4056 × 3040, C/CS mount, ~$50–75)** with a 16–25 mm lens at ~300 mm working distance. The resolution requirement is derived, not guessed: stable segmentation needs **8–15 px across the object's short dimension** (morphological opening alone eats 1–2 px), so a 0402's 0.5 mm side at 10 px demands **0.05 mm/px**. The HQ camera over a 100 mm field gives 0.0246 mm/px → **~20 px across a 0402**, twice the target. A wide-FOV camera covering the whole 300 mm platform would manage only ~7.7 px — below threshold, which is why the counting camera is aimed at the tray, not at the platform. The interchangeable lens is the deciding factor over Camera Module 3, whose autofocus hunting is an unwanted variable in a fixed rig; skip 4K USB webcams, which rarely focus under ~150 mm and whose firmware sharpening/MJPEG corrupts exactly the fine edges blob separation depends on. Prefer the longer lens/longer working distance (25 mm at ~318 mm gives ~14° vs 12 mm at ~153 mm giving ~29°), which is the cheap stand-in for a telecentric lens. Perspective shift is `part_height × radial_offset / WD` — a 5 mm TO-220 at 40 mm off-axis at WD 318 mm shifts ~0.6 mm, tolerable — but run a one-time `cv2.calibrateCamera` checkerboard pass since the rig is fixed.

**Pipeline — classical CV, decisively.** With controlled lighting, **global Otsu beats adaptive thresholding**; adaptive exists to paper over illumination gradients you should not have in a fixed fixture, and it fragments clean background into speckle.

```python
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
blur = cv2.GaussianBlur(gray, (5, 5), 0)
_, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
# touching objects: distance transform + watershed
dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
_, fg = cv2.threshold(dist, 0.5 * dist.max(), 255, 0)
```

Touching parts get **two independent votes**: watershed's local-maxima count, and `round(blob_area / single_area)`. `single_area` is estimated from the image itself by iterated trimmed median over non-border contours (most parts land isolated), so no prior knowledge of the part is needed. Watershed handles round/blobby parts well and over- or under-splits elongated chips; the area ratio is the reverse. **When the two votes disagree, flag the cluster — never silently pick one.**

**Stacking is the one failure that must never be silent.** Out-of-plane overlap systematically *loses* occluded silhouette area with no bounded correction. Three independent detectors, any of which forces a hard "spread these out and retry": the shallow tray (a second layer overflows the rim), the scale cross-check below, and a second **raking/grazing light** — coplanar parts cast no mutual shadow, a stacked part does.

**Fusing vision and mass** by inverse-variance weighting:
```
n̂ = (n_v/σ_v² + n_m/σ_m²) / (1/σ_v² + 1/σ_m²)
z  = (n_v − n_m) / sqrt(σ_v² + σ_m²)      # |z| > 3 → do NOT fuse, flag
```
Disagreement is itself the valuable output: it means a mixed bin, a wrong `unit_mass_mg`, or a hidden overlap undercount. Mixed bins are also detectable directly — a blob-area histogram that is bimodal in a way small-integer touching multiples don't explain — and should be refused.

**Deep learning is deliberately not the first tool.** Zero-shot exemplar counters look conceptually perfect for "here's one resistor, count them", but measure MAE ≈ 5.7–7.1 on FSC-147 — ~10% relative error on objects larger and easier than SMD passives — and "Can SAM Count Anything?" concludes SAM-based counting is unsatisfactory specifically for small, crowded objects. Density-map crowd counting is the wrong problem shape (unbounded crowds, not exact small integers). Full control of the fixture satisfies exactly the precondition where classical CV wins. Reserve a fine-tuned **YOLOv8/v11 + SAHI tiling** pass as a later fallback for irregular shapes (DIP ICs, connectors) where contour geometry is too variable — a few hundred labelled boxes per class, seconds per frame on CPU, fine for a non-realtime station.

**Monolayer help**: a 2–4° sloped tray floor (free), a Ø10 mm coin ERM motor (~$1–2) pulsed 1–2 s to break clumps, and a live overlay that paints still-ambiguous clusters red so the user taps or spreads until clean. Perfect mechanical singulation is not the goal — commercial vision counters (Eyecon, ~99.99%) singulate first and only ever ask the algorithm to count non-touching objects; this fixture aims to get close enough that the two-vote pipeline copes.

**Calibrate per part number once** from a ~25–50 piece confidently-counted reference batch, learning both `single_area` and `unit_mass_mg` in the same session — so the two sensors are calibrated together and can cross-check each other thereafter.

**Promise in the UI**: "up to ~200 parts in a monolayer, ~99%". Anything flagged as unresolved overlap falls back to the fused estimate with visibly wider error bars.

## Resistor colour-band checker

Reads the colour bands off axial through-hole resistors in the tray image and decodes the value. Existing prior art to start from: `alhazmy13/ResistorsOpenCV`, `SupreethRao99/CVResist`, and the Hackaday OpenCV writeup — all of which report the **same** failure mode, **sensitivity to lighting**. That is exactly the variable this station already controls, which is why it can work here and doesn't in the wild.

**Frame it as a *checker*, not an identifier.** The high-value question is "does what is physically in this bin match what the database says it is?" — verifying a known expected value is far easier and far more robust than open-ended identification, and it catches the genuinely common failure of a mis-sorted part. Since the tray is already imaged for counting, **the same shot can decode every resistor in frame and flag any that disagrees with the expected value** — count and verify in one capture. Open-ended identification of an unknown resistor is supported too, but must return **ranked candidates, never an assertion**.

**Lighting: diffuse top/dome, not backlight.** Colour needs reflected light; the counting backlight produces silhouettes with no colour information. This uses the dome/ring mode the station already needs for reflective parts, so it adds no hardware.

Pipeline:
1. Segment the resistor body, find its major axis, sample a strip along it, and cluster into bands.
2. **Classify in CIELAB, not RGB or HSV.** Perceptual uniformity is what separates brown/red/orange and brown/violet/grey — the confusions that break every naive implementation.
3. **A permanent white/grey reference patch on the tray** normalises per-shot illumination and sensor white balance. Cheap and decisive; without it, colour constancy is the whole problem.
4. **Determine band order explicitly.** Gold/silver identifies the tolerance band and therefore the tail; with no gold/silver, use inter-band spacing. A genuinely symmetric case must return **two candidate values**, not a guess.
5. **Snap the decoded value to the nearest E-series (E24/E96) standard value.** A decode of "4.83 k" is wrong — it is 4.7 k. E-series membership is a strong, free error-correction signal that constrains the answer far more than the per-band confidences do.
6. Report confidence; refuse rather than guess.

**Honest limits:** blue-body metal-film parts shift apparent band colour against the body and worsen green/blue confusion; faded, dirty or old resistors degrade badly; 4- vs 5- vs 6-band must be detected, not assumed. Band widths of ~0.5–1 mm are comfortable at the station's ~20 px per 0.5 mm, so resolution is not the constraint — colour is. SMD resistors are a different problem entirely and are handled by numeric code decoding (3-digit, 4-digit and EIA-96, implemented in `app/services/enrichment/mpn_decoders/` — **not** by the `resistors` package, which does colour bands only; see the MPN decoders section), not by this.

**Where it sits: a Phase 3 follow-on to vision counting**, since it reuses the tray, camera, calibration and dome lighting. Materially cheaper and more reliable than the IC marking reader below, because colour decoding is close to a solved problem once lighting is fixed, and the checker framing plus E-series snapping makes it robust rather than merely clever.

## IC marking reader — build the cheap version first

Reading part numbers off unmarked/mystery ICs. The proposed rig (addressable-LED dome + apex camera, capturing under many light angles) is **RTI — Reflectance Transformation Imaging**, developed for reading worn coin and tablet inscriptions. The technique is correct and the physics favours it: **laser marking dominates modern ICs** (>50,000 rub cycles vs 500–1,000 for ink), and laser marks are *topographic*, nearly invisible under diffuse light. Grazing illumination moves the specular lobe outside the lens aperture while the groove walls scatter light back — bright marks on a dark field. Matte-black dome interior is correct (a white interior becomes an integrating sphere and destroys directionality); "all LEDs on" then gives a diffuse baseline shot for free, which is what ink-printed marks and logos want.

**Tooling:** `RTIBuilder` is **deprecated**; the maintained option is **Relight** (CNR-ISTI Pisa), whose `relight-cli` is genuinely headless and scriptable (PTM and HSH fits; HSH behaves better at grazing angles). For surface normals directly, `yasumat/RobustPhotometricStereo` provides L1/SBL/RPCA solvers that reject specular and shadowed observations per-pixel — exactly what glossy black epoxy needs. Curvature / normal-map rendering is the output that makes shallow etch legible. **Highlight RTI** (a mirror sphere in frame) derives light directions without trusting printed dome geometry — worth doing, since there is a whole research line on correcting dome direction error.

**Optics are easier than expected — no microscope needed.** The IMX477 active area is 6.29 × 4.71 mm, so a 10 mm field of view is only **0.63×** magnification, giving 2.46 µm/px — a 0.4 mm character spans ~160 px, roughly 6× oversampled. A tight SOT-23 crop (4 mm FOV) is ~1.5×. An Arducam 25 mm CS lens (~$20) plus a cheap extension tube reaches 1× at ~50 mm working distance.

**Depth of field is the real constraint:** ~0.22 mm at 0.6×, ~0.10 mm at 1×, ~0.055 mm at 1.5×. Parts must sit flat on a levelled stage; curved packages need conditional focus stacking (`focus-stack`), costing 5–20 s. **Capture is camera-bound, not LED-bound** — WS2812B updates in under 2 ms, but full-res stills run 0.3–1.5 s each, so a 24-shot sequence is **15–60 s per part**. Note every real published RTI dome uses **64–128 LEDs**, not 24 (ARTID: 64 LEDs, 18″ dome, ~$600 excl. camera; others 76–128; SimpleRTIDome targets ~€200 for items ≤70 mm) — 24 is a floor, not the norm. A gutted USB microscope is not worth it; the IMX477 is strictly better.

**The dominant limiter is not imaging — it is the marking→part lookup.** IC top marks are frequently truncated MPNs, house codes, or date/lot codes. Community databases (embedeo ~3,343 codes, smd.yooneed.one, Elektronik-Kompendium, s-manuals) cover cryptic 2–3 character *discrete* codes and have known cross-manufacturer collisions; **DigiKey has no marking-code search at all**. For larger ICs the better path is OCR → fuzzy/prefix match against a distributor full-text search, reusing the provider interface. Realistic end-to-end resolution for a mystery salvaged IC is **~30–60%** even with a perfect image.

**Therefore: build the $50 version first.** A handful of LEDs at 10–20° grazing on the ESP32 already in the BOM — no dome, no photometric stereo — feeding a vision LLM plus fuzzy prefix-match. That is 2–5 hours and ~$50–150, against 20–40+ hours and ~$150–400 for the full dome. Only build the dome if the cheap version demonstrably fails on the real salvage backlog. **Timebox it; this is a genuinely interesting side project with bounded payoff, not core infrastructure**, and it must sit below anything touching the correctness of counting or tracking.

**One hard constraint either way: never auto-accept an OCR'd or LLM-read part number.** Vision models' documented failure mode is confidently inventing a plausible value when the source is illegible, and a wrong-but-confident chip identification is worse than "unknown". Results route to the existing review queue as `parameter_value_candidate`-style rows with the enhanced image attached via `documents`, and OCR provenance is recorded so confidence is never inherited from a guess. OCR engine ranking is PaddleOCR > EasyOCR > Tesseract; note there is **no published accuracy figure for OCR on photographed IC top marks** — a genuine literature gap, so measure it on your own parts before trusting it.

## Label printing — abstraction first

Consumable cost and availability drive the hardware choice, so the design commits to **no printer** and targets **generic standard-size die-cut thermal rolls** (25×25, 40×30, 50×30 mm) at $0.008–0.02/label from any label supplier. That requires a printer with an **adjustable gap sensor** and an open protocol. Note explicitly: Brother QL's DK tape is *format*-locked (the keying is mechanical notches, not a chip, so compatibles exist — but only in Brother's own sizes), so despite being cheap in bulk it fails the "widely available consumables" test.

```
LabelSpec       template + target size + DPI + payload  (pure data)
LabelRenderer   spec + object data -> PIL.Image at target DPI, or a multi-up PDF
LabelBackend    Protocol: capabilities{dpi, widths, min_width_mm, cut} ; print(image, spec)
  ZplBackend        real ZPL over TCP:9100 or /dev/usb/lpN    <- Zebra ZD410/ZD411/GX430t
  TsplBackend       TSPL2 raster                              <- Xprinter and clones
  BrotherQlBackend  luxardolabs/brother_ql fork (maintained April 2026)
  CupsBackend       any CUPS queue, incl. phomemo-tools and laser
  PdfSheetBackend   multi-up PDF for laser sheets             <- reportlab / pylabels2
  AgentBackend      forwards to deviceagent for a Pi-attached USB printer
  FileBackend       PNG to disk — testing, and "print later"
```

The server always **re-fetches current name/path/quantity at print time** and never trusts client-supplied text, so a stale label is impossible. `label_prints` records backend, template and DPI so a reprint matches the original. Printing is deliberately *not* queued through the offline outbox — labels aren't audit-critical the way ledger movements are.

**Hardware guidance to act on when convenient, not now:** a used **Zebra ZD410/ZD411** (~$85–130, real ZPL so `nc printer 9100 < label.zpl` works with zero drivers, 15 mm minimum width — the only cheap unit that reaches tiny bins, and a 300 dpi variant exists) for part/lot labels on generic rolls. Anything advertising "ZPL-compatible" under ~$150 is really TSPL/ESC-POS, and most bottom out at 40–50 mm width, which physically cannot label a small compartment. The sub-$50 Niimbot tier is a trap: reverse-engineered Bluetooth protocols that break on firmware updates, and proprietary rolls at $0.03–0.05/label — *more* than Brother.

**Bin labels sidestep the printer question entirely.** Because bins are printed with a friction-fit label slot, bin labels are **plain 200 gsm cardstock cut from a laser or inkjet sheet** — no adhesive, no thermal media, and the direct-thermal fade problem (rated 6–12 months indoors, weeks under UV as the leuco dye breaks down) simply never arises. Toner on cardstock in a slot is archival, costs fractions of a cent, needs no new hardware, and is trivially reprintable when a bin's contents change. This is `PdfSheetBackend` rendering a multi-up sheet with cut marks.

So the thermal printer is only ever needed for **part/lot labels that must stick to something** — antistatic bags, reels, cut-tape strips, salvage baggies. That is where adhesive media and the "cheap, widely available consumables" requirement apply. Two ordering cautions there: buy **"labels," not "tags"** (Zebra and others sell same-size non-adhesive tag stock), and prefer **rounded-corner die-cut** stock, which resists peel initiation far better than square corners at near-zero extra cost.

**QR sizing, where a QR is used at all:** a ~30-char payload is QR version 2–3 at ECC-M; at a 0.4 mm module that is ~13 mm square including the 4-module quiet zone. At 203 dpi a module is only 3.2 dots (marginal); at 300 dpi it is 4.7 (reliable) — **300 dpi matters at the smallest sizes**. Bin label card = large human-readable name/value, `short_id` in small monospace, QR optional per container type. Part/lot label = MPN as the largest text + package + description + QR, with quantity stamped "as of" print time and explicitly marked a snapshot.

## Identification — NFC primary

**NFC is the primary identifier; a printed QR is optional per container type.** Containers are plastic, so the metal-detuning problem that would have made NFC expensive does not apply — bare NTAG213 stickers at ~$0.16–0.28 each work directly.

**The payload is a URL, identical in every carrier:** `https://<host>/s/{short_id}`, written as a plain NDEF URI record (well-known type `U`, abbreviation `0x03` — not a smart poster, which would blow past the practical byte ceiling). That single decision is why NFC costs almost nothing to build — Phase 1's `/s/{short_id}` route already serves it, so there is no new endpoint, no new resolver handler, no new payload format.

**Write the NDEF URI *and* record the UID; resolve NDEF-first with a UID fallback.** UID-only was considered and is *nearly* right — retail NTAG213 stickers ship NDEF-formatted-but-empty, so Web NFC does fire `reading` with a populated `serialNumber`, and keying off the UID alone genuinely works inside the PWA on Android. What it costs: **iPhone entirely** (background reading only fires for a well-formed NDEF *URI* record — a UID-only tag produces nothing, not even a notification), and **tap-to-open on Android** (Web NFC only delivers to a *foregrounded* tab, and a blank tag cannot launch anything — Android shows "No supported application for this NFC tag"). Note a truly *unformatted* tag fires no event at all; only factory-NDEF-formatted stock works, which retail stickers are.

The fact that settles it: **the 7-byte UID lives in pages 0–2, factory-locked and physically separate from NDEF user memory starting at page 4.** A write that fails partway can corrupt the NDEF payload but **cannot touch the UID** — so the worst case of writing is degrading to exactly the UID-only design, with the verify screen flagging it for a rewrite. One ~0.4 s write per tag, once, inside a provisioning budget that is already 2–3 s per drawer. NTAG213 is rated 100k writes / 10-year retention, so endurance is not a consideration.

**Nothing mutable ever goes on a tag.** Not counts, not fill state, not timestamps. Beyond the obvious (you would need the tag at a reader to update it), the decisive argument is that any *remote* mutation — a bulk import, a reconciliation job, a BOM pick — cannot touch a tag it does not physically hold, so the tag would silently go stale while still *looking* authoritative. That is worse than storing nothing. **A tag is a foreign key, not a record.**

Encoding `(container, index)` in the payload is rejected for the same reason hierarchy is never in the payload: that pair *is* the derived path, and it becomes a lie the moment a drawer moves to a different cabinet — the identical "must be physically present to fix it" problem, restated for structure instead of counts.

- **Android**: the PWA reads *and writes* tags via **Web NFC** (`NDEFReader`, Chrome for Android 89+).
- **iPhone XS+**: OS background tag reading opens the URL in Safari on tap — **no app required**. iOS cannot *write* tags, so provisioning happens from an Android phone or the station reader.
- **Web NFC is Chromium-on-Android only** — not desktop Chromium, so the Pi kiosk cannot use it and needs a real reader. It is also a W3C *Community Group* draft, not standards track: treat it as a permanent Chrome-Android feature, not a maturing standard. `scan()`/`write()` need a secure context and a user gesture per call; the permission persists per origin. `NDEFReadingEvent` also exposes `serialNumber` (the UID), so a UID-keyed fallback exists, but NDEF-URI is preferred — it reuses the same resolution path and survives tag replacement (rewrite the same `short_id`, no DB change).
- **Tag choice: NTAG213**, 144 B user memory (ample for the URL). A 25 mm sticker is a **tap (1–4 cm), not a wave**. **Avoid MIFARE Classic** despite it often being the cheapest tag sold — it is not NDEF-native, so Web NFC and plain phone reads will not work with it at all. Fudan FM11NT021 is a cheaper NTAG213-compatible but lacks `GET_VERSION`, so some tooling misidentifies it. No encryption needed — home lab, low threat.
- **Station reader: genuine Adafruit PN532 over UART (~$40)** with `adafruit-circuitpython-pn532`, which is maintained and reads NDEF natively. MFRC522 is rejected despite costing ~$3: its Python ports are UID-focused with hand-rolled NDEF and several unmaintained forks — the $37 premium buys a maintained NDEF-native library. Avoid cheap PN532 clones (documented flaky SPI/firmware). `nfcpy` and `pyscard` remain healthy if an ACR122U is ever preferred; `libnfc` is low-activity.

**Provisioning:** create the `locations` row (assigning `short_id`) → write the NDEF URI → **read back to verify** → print the label card. **Do not lock tags read-only by default.** With no security requirement, the real risk is accidentally overwriting a provisioned tag from a left-open write screen — mitigate in software (blank-tags-only by default, explicit overwrite toggle, 20 s auto-timeout on the write screen). `makeReadOnly()` is irreversible and would block the routine relabeling a hobby inventory needs; offer it opt-in for fixtures that never change.

## Physical layer

**Every container carries exactly two things: a printed label card and an NFC tag.** Nothing else in v1.

### v1: off-the-shelf drawer cabinets

The starting point is standard multi-drawer component cabinets, not printed bins. Each cabinet gets a label; each drawer gets a label **and** a tag.

**These cabinets mostly already have molded label card slots**, which is why the label story needs no adhesive and no thermal media — cards are cut from a laser/inkjet sheet and slid in. Verified examples: **Akro-Mils 10144** (44 small + 4 large drawers, 20″ × 6⅜″ × 15¹³⁄₁₆″, polystyrene, label slots confirmed, ~$48); **Raaco steel cabinets** (C8-30 375 × 306 × 147 mm through C10-40 465 × 306 × 147 mm, PP drawers in a painted-steel carcass, full-width label holders taking ~18 × 87 mm cards). Both plastics are non-conductive, so nothing detunes the tag.

**A finding that shapes the layout editor: no manufacturer publishes a rows × cols grid.** Retailers list a drawer *count* plus a size mix — "44 small + 4 large", not "8 × 6 with merges". So the editor must be a **canvas of base cells with merged regions**, never an assumed clean grid. That is exactly what `container_type_slot_templates` exists for.

**Tag on the underside, label on the front.** A typical small drawer front is ~52 × 40 mm — not enough for both a readable label and a reliable tag. Below ~20–22 mm diameter, Android read reliability degrades noticeably (weaker NFC antennas than iPhones); 30 mm is the size that gives a comfortable 20–40 mm tap range. The drawer underside offers ~152 × 52 mm of flat plastic, so a full 30 mm tag fits, and it faces the station antenna when the drawer is set down. **Honest cost: you cannot tap a closed drawer with a phone — you pull it out first.** Given the workflow is to remove the drawer and place it on the station anyway, that is the right trade, and the printed label covers the "just look at it" case. (Underside ribbing/draft angles are not documented for any model — size the sticker footprint conservatively and check one drawer before ordering 300 tags.)

**Per-drawer tagging is a per-instantiation toggle, not a global decision.** Tagging only the cabinet and picking the drawer on screen cuts ~90% of the physical labour and all per-drawer mis-binding risk, and it is the better choice whenever the pick already involves looking at a screen. Per-drawer tags earn their cost when drawers physically travel to the station. Ship both and choose per cabinet.

### Later: 3D-printed Gridfinity

**Gridfinity is the baseline** for anything that is a discrete, pick-up-able container — exactly what gets carried to the station. Verified spec: **42 mm grid pitch, 41.5 mm bin footprint, 7 mm height unit**; the magnet variant uses 6 × 2 mm neodymium discs inset from the corners. It maps onto the data model for free: a baseplate is a `locations` row with `child_layout=grid` and `grid_rows`/`grid_cols` in grid units; each bin is a child whose volume derives from `(41.5·cols) × (41.5·rows) × (7·height_u)` minus a wall/lip allowance. Genuinely irregular compartments (padded cases, tackle boxes) stay on `container_type_slot_templates`.

Generator: **kennetek/gridfinity-rebuilt-openscad** (MIT, actively maintained, one parametric OpenSCAD file, runs headless via `openscad -D … -o out.stl`). This is why `container_type_physical.generator_params_json` can literally *be* the OpenSCAD parameter set — STL generation becomes reproducible rather than a hand-curated library. **ndevenish/gflabel** (Python/build123d) covers label geometry, though its maintainer describes it as an intermittently-updated hobby project.

**Tag pocket on the BOTTOM** — Ø25.5 mm × 0.4 mm deep, centred on the footprint underside; on a 1×1 bin it clears the corner magnet bosses by ~8 mm all round. NFC reads fine through PLA/PETG (non-conductive, no detuning), so the pocket exists only to stop the sticker being abraded as bins slide. A 1.2 mm printed snap-cap disc at 0.3 mm interference retains it, so a dead tag is replaceable without reprinting the bin. **Bottom placement is the key decision**: with the reader antenna under the station platform, a container identifies itself the moment it is set down — zero scanning gesture. Mid-print M600 embedding is rejected because it inverts the provisioning order (you cannot embed a tag before confirming the write succeeded).

**Label slot, not adhesive.** Since the bins are printed anyway, put a friction-fit slot on the raked front face (~10° rake) sized for 200 gsm cardstock (~0.27 mm) with 0.2–0.3 mm clearance; ~40 × 12 mm on a small bin, scaling with width. This sidesteps the adhesive problem entirely — permanent adhesive on polypropylene lifts within months, and removable adhesive on low-surface-energy plastic is worse — and beats embossed text (slow to print, poor legibility, cannot show live data). A clear PETG cover window is optional, not default.

**Label content: large human-readable name/value first**, since finding the drawer by eye is the whole point, plus the `short_id` in small monospace as a support fallback. **The QR is off by default** — a 40 × 12 mm strip has no room for a QR reliable at reading distance (~15 × 15 mm) without crowding out the text — and remains a per-`container_type` option for anyone without NFC.

**The honest cost is print time, not money.** Populating a real lab is 100–300+ bins at 30 min–2 h each: tens to over a hundred hours of printing, plus a per-bin ritual of inserting a tag and a label card. Mitigate by standardising on very few footprint variants.

## Layout authoring and tag provisioning

This is the tooling that makes hundreds of drawers tractable, and it belongs in Phase 1 — it is the difference between a system that gets populated and one that doesn't.

### Layout authoring

**Pure grids cost nothing to store.** With `materialize_slots=false`, a uniform cabinet stores **zero** `container_type_slot_templates` rows; the layout is computed by `f(grid_rows, grid_cols, slot_label_scheme)`. The first merge, label override, or per-cell size class **materializes** the whole type — one explicit row per surviving region — and flips `materialize_slots=true`, after which the template table is authoritative and the generator is never consulted again for that type.

`slot_label_scheme` is one of `row_alpha_col_num` (`A1`..`H6`), `sequential` (`1`..`48`, optional zero-pad), or `custom` (every label explicit), each with a small params object.

**Visual editor**, one component for phone and desktop: the canvas renders as a CSS grid with ≥44 px touch targets (dense grids scroll with a sticky header). **Tap** selects a cell and opens a cell inspector (bottom sheet on mobile, side panel on desktop) for label override, size class, inner volume, and a live label preview. **Long-press then drag** enters merge mode, anchoring a selection rectangle clamped to its bounding box — only contiguous rectangles are legal — with a floating "Merge 2×2" action. Merged cells expose **Split**, which decomposes back to base cells at their generated labels. Desktop adds spreadsheet-style click-drag and Shift+Arrow.

**Instances own their own copy of the layout.** `POST /api/locations/{parent}/instantiate {container_type_id, count, naming_pattern, tag_granularity}` creates N cabinets and materializes the type's template into each instance's own child `locations` rows. Instances are **not live-linked** to the type — which is what makes the change guard simple: editing a type only affects future instantiations.

**Change guard** for editing an instance's own layout: *safe* — relabeling, size-class and volume edits, scheme changes that don't move cells. *Guarded* — shrinking the grid, deleting a slot, or merging cells whose child locations hold stock or a bound tag; the API returns **409 with the list of affected slots** and the UI offers "move contents to a holding location first". *Refused outright* — reinterpreting an existing slot's identity; a shrink always deletes-then-recreates slots confirmed empty, never renumbers in place.

**`sort_order` must follow physical reading order**, because it drives both the provisioning cursor and the label sheet. Recomputed on every template save as `(row_idx, col_idx)` ascending, assigned with gaps of 10. A merged region sorts by its **top-left corner** — exactly where a reader's eye reaches it — so merges never break reading order.

A **seed library** of real cabinet types ships as `is_seed=1` rows (Alembic data migration). Seed types are read-only; editing one implicitly clones. `POST /api/container-types/{id}/clone` covers "this cabinet is identical to that one".

### Bulk provisioning

**Apply every tag physically first, then walk the cabinet binding them.** This ordering is the whole trick: you are confirming whatever tag is *already on that drawer*, so there is no loose-tag hand-off where two units can be swapped. Station provisioning with loose tags carries exactly that risk and should be reserved for pre-provisioning before the drawers exist — and always followed by a verification pass.

**Phone via Web NFC is primary** (you are standing at the cabinet anyway); the station PN532 is the fallback and the only path if provisioning from iOS.

**The cursor is never stored** — it is always `MIN(sort_order)` among that cabinet's children lacking a `location_tags` row. Resuming a half-finished cabinet is therefore free and immune to anything bound out of band.

Flow: the cabinet grid renders with the cursor slot pulsing. Tap a tag → if its UID is bound elsewhere, modal "Already bound to {label_path}" with **Move here** / **Cancel**; otherwise bind, write the NDEF URI, and record `written_at` and `bind_source`. Feedback: 150 ms green flash, `navigator.vibrate(50)`, a short tone; after a **400 ms debounce** (so the same tag can't double-fire while still in range) the cursor **auto-advances with zero additional interaction**. Target 2–3 s per drawer — **a 44-drawer cabinet in under two minutes**, walking-paced rather than software-paced.

Escape hatches: tapping any cell jumps the cursor there, so auto-advance is a fast path and not a lock; **Skip** leaves a slot empty and advances; **Undo** is one always-visible floating button showing the last-bound slot's label, backed by a five-deep stack (restoring the prior binding if the action was a Move).

**The verification walk is not optional busywork.** A separate session kind re-reads every tag in order and compares to the expected UID. Match → tick and advance. Mismatch → log `expected_tag_uid`, `scanned_tag_uid`, and a reverse lookup of which slot the scanned tag *actually* belongs to ("this tag belongs to B2") — and **do not auto-fix**; a human decides whether to swap or rebind. No software can stop a person sticking a tag on the wrong drawer; only detect it, and a mis-bound tag is otherwise invisible until it causes a wrong put-away.

### Label sheets matched to the layout

`PdfSheetBackend` takes a cabinet plus a card type and lays cards out **row-major in the same grid as the physical drawers**, so a sheet is cut and inserted left-to-right, top-to-bottom with no re-sorting. Card size derives from `container_types.front_width_mm/front_height_mm` minus a margin for the plastic lip (a 46 × 22 mm front → a 40 × 18 mm card); a spanning drawer's card scales to its footprint with a visible outline so cuts still line up. Supports pre-die-cut stock (mapped by product code to cell pitch/margins) or plain cardstock with hairline crop marks.

Drawer card: `slot_label` large top-left, `short_id` in Crockford grouping bottom in monospace, a tiny cabinet breadcrumb. Cabinet card: name large, `short_id`, full `label_path`. **No contents or counts** — they would be stale immediately. Legibility at ~50–60 cm arm's length means slot label ≥8–10 mm cap height bold sans, `short_id` ≥3.5 mm mono.

**QR inclusion is conditional on card geometry, not a global switch:** include it when the card's short dimension is ≥16 mm (a Raaco-style 18 × 87 mm strip has room; a 40 × 12 mm Gridfinity slot does not), since a ~30-char payload needs ~13 mm square including its quiet zone. `locations.last_printed_at` drives a "never printed" badge and a one-tap "reprint this card" from any slot detail or the verify screen; `POST /api/labels/sheets` accepts a `slot_ids` filter so a single replacement card can be positioned on a partly-used sheet.

### Routes

`POST|GET /api/container-types`, `GET|PATCH /api/container-types/{id}`, `POST /api/container-types/{id}/clone`, `GET|PUT /api/container-types/{id}/slot-template`, `POST /api/locations/{id}/instantiate`, `POST /api/locations/{id}/reapply-layout`, `GET /api/locations/{id}/layout` (grid + tag + contents state, shared by editor/provisioning/verify), `POST /api/locations/{id}/provisioning-sessions`, `GET /api/locations/{id}/provisioning-sessions/current`, `POST /api/provisioning-sessions/{id}/{bind|undo|skip}`, `POST /api/locations/{id}/verification-sessions`, `POST /api/verification-sessions/{id}/check`, `GET /api/verification-sessions/{id}`, `POST /api/location-tags/{id}/unbind`, `POST /api/location-tags/resolve`, `POST /api/labels/sheets`, `GET /api/labels/sheets/{job_id}`.

### Schema (additive)

```sql
CREATE TABLE provisioning_sessions (
  id INTEGER PRIMARY KEY,
  root_location_id INTEGER NOT NULL REFERENCES locations(id),
  kind TEXT NOT NULL,                          -- provision | verify  (no CHECK: see conventions)
  device_kind TEXT,                            -- phone_webnfc | station_pn532
  started_at TEXT NOT NULL, completed_at TEXT);

CREATE TABLE verification_mismatches (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES provisioning_sessions(id),
  location_id INTEGER NOT NULL REFERENCES locations(id),
  expected_tag_uid TEXT, scanned_tag_uid TEXT NOT NULL,
  scanned_resolved_location_id INTEGER REFERENCES locations(id),
  created_at TEXT NOT NULL);

ALTER TABLE container_types ADD COLUMN front_width_mm REAL;
ALTER TABLE container_types ADD COLUMN front_height_mm REAL;
ALTER TABLE container_types ADD COLUMN is_seed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE locations       ADD COLUMN last_printed_at TEXT;
ALTER TABLE location_tags   ADD COLUMN bind_source TEXT;
```

No stored cursor anywhere. All new tables or nullable/defaulted columns — additive, no backfill.

**UHF RFID (EPC Gen2) is ruled out.** It is the only tag tech offering a genuinely new capability — sweep a shelf and inventory every bin unopened — and it fails in exactly this environment. A closed metal drawer is a Faraday cage at UHF; tags inside are unreadable from outside, and the only fix is a near-field antenna *per drawer*. Tag-to-tag detuning is severe under ~7 mm, so bins touching edge-to-edge break. A 40×20 mm label on a cheap module gets tens of cm, not the multi-meter spec number. Decisively: even professionally engineered deployments show *bimodal* read rates (RFID Journal's analysis of the industry's "99.8%" claim found a meaningful fraction of sites reading ~50% and needing manual recheck) — and **a cycle-count system with false negatives reports phantom losses, which is worse than not having one**, while re-scanning QR has zero false-negative risk by construction. Cost for the record: SparkFun M6E-Nano retired and NRND (~$138), M7E replacement $330; the cheap R200/YRM100 route is $13–30 plus $0.30–0.75/tag but has no maintained Python library (`sllurp` targets industrial networked readers only), so it means writing a serial driver — $100–430 for something untrustworthy. If ever revisited, note reader bands are regional (FCC 902–928, ISED Canada same, ETSI 865–868) and tags are narrowband-tuned to match.

## Core workflows

**1. New part intake.** `SCANNING → RESOLVING → (NEW_DRAFT | KNOWN_FOUND | AMBIGUOUS) → ENRICH → REVIEW → DIMENSIONS → QUANTITY → ASSIGN → PRINT`. ECIA DIs auto-fill MPN, supplier PN, lot, date code and quantity. `POST /api/locations/suggest` proposes the destination. Nothing touches the ledger until ASSIGN commits.

**The fast path is the point.** At RESOLVING, a "Queue for later" toggle posts to `POST /api/intake/pending` and returns to SCANNING immediately — zero screens per item. Scan a whole box of reels in under a minute, then walk the pending queue later at a desktop through the same review tail. This is the countermeasure to the thing that kills these projects, and `parts` is shaped for it: only `name` and `part_kind` are NOT NULL, so an unrecognized label becomes a legal `is_stub=1` part row in one tap instead of a form the user abandons.

**2. Known-part re-stock.** The resolver attaches `existing_lots[]` whenever the matched part has any lot with quantity > 0, so the UI branches to `KNOWN_FOUND` **before** enrichment or dimensions run, showing identity plus every existing location path and per-location quantity. Two actions: add to the lot that's already there, or start a new lot elsewhere.

**3. Take/return.** One screen, one-handed: scan, big ±1 stepper plus keypad, big Commit, then an 8-second one-tap undo that posts a compensating ledger row. A client `uuid4` idempotency key is attached at scan; duplicate scans within ~2 s or during an in-flight commit are dropped by the same debounce as the decoder.

**4. Move / transfer.** Scan source, scan destination, confirm. "Empty this bin into that one" is a separate entry point from a bin's screen. Same source and destination blocks commit. In a bulk empty, one lot failing validation commits the rest and reports just that failure.

**5. Station session — the primary day-to-day loop.** `IDLE → CONTAINER_DETECTED (weight jump > ~200 mg) → IDENTIFYING (NFC poll, ~5 tries / 1.5 s) → IDENTIFIED | UNIDENTIFIED → WEIGHED (stable, tare-subtracted) → READY (name, path, short_id, ledger balance, weight-derived count) → ACTION (take N / add N / recount / pour into the counting tray) → CONFIRM → COMMIT`, looping back to ACTION while the tag and weight stay present, and returning to IDLE on removal. Removing the container before COMMIT aborts and writes nothing. `UNIDENTIFIED` (no tag, or a read failure) falls through to manual search or "provision this container now".

**6. Recount / take by weight or vision.** Differential is primary in both modes. For vision, pour only the handful removed onto the tray — never re-count the whole bin — then fuse against the mass estimate and flag on `|z| > 3`.

### Additional schema (additive)

```sql
ALTER TABLE locations ADD COLUMN tare_mg INTEGER;          -- + tare_sigma_mg, tare_measured_at, tare_source

CREATE TABLE location_tags (
  id INTEGER PRIMARY KEY,
  location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  tag_uid TEXT, ndef_url TEXT NOT NULL,
  is_read_only INTEGER NOT NULL DEFAULT 0,
  written_at TEXT NOT NULL, last_verified_at TEXT,
  UNIQUE(location_id));

CREATE TABLE devices (                                     -- station, reader, scale, printer, camera
  id INTEGER PRIMARY KEY, short_id TEXT UNIQUE,
  kind TEXT NOT NULL, display_name TEXT NOT NULL,
  connection TEXT NOT NULL, last_seen_at TEXT, calibration_json TEXT);

CREATE TABLE container_type_physical (
  container_type_id INTEGER PRIMARY KEY REFERENCES container_types(id) ON DELETE CASCADE,
  gridfinity_u_w INTEGER, gridfinity_u_d INTEGER, gridfinity_u_h INTEGER,
  stl_ref TEXT, generator TEXT, generator_params_json TEXT,
  tag_pocket TEXT DEFAULT 'bottom', label_slot_mm TEXT);

CREATE TABLE count_sessions (                              -- vision counting audit trail
  id INTEGER PRIMARY KEY, lot_id INTEGER REFERENCES stock_lots(id),
  image_document_id INTEGER REFERENCES documents(id),
  n_vision INTEGER, sigma_vision REAL, n_mass INTEGER, sigma_mass REAL,
  n_fused REAL, z_disagreement REAL, single_area_px REAL,
  lighting_mode TEXT, overlap_flagged INTEGER NOT NULL DEFAULT 0,
  accepted INTEGER, created_at TEXT NOT NULL);
```

Also `parts.single_area_px` (or better, keep it per-`part_id` in `unit_mass_samples`' sibling `single_area_samples`) so the vision calibration lives alongside the mass calibration and both are learned in one reference-batch session. Every image is stored via the existing content-addressed `documents` table, so a disputed count is always re-inspectable.

## Phasing

Each phase ends with something genuinely usable, not scaffolding.

| Phase | Contents | Explicitly not |
|---|---|---|
| **1** | Parts, storage tree, append-only ledger, capacity + auto-assignment, short IDs, `/s/{short_id}` resolve route, resolver chain + alias learning, ECIA parser, manual parameter entry against seeded templates, full parametric search, **layout editor + bulk tag provisioning + verification walk**, `PdfSheetBackend` label-card sheets, **in-browser phone barcode scanning**, **Web NFC read/write from the phone PWA**, mobile-first PWA (search, part detail, tree, bin contents, take/return, intake queue) | Station hardware, external APIs, LLM, datasheets, BOM |
| **1.5** | **Physical bootstrap, runs in parallel with 1** — buy cabinets, print label cards, apply tags, run the provisioning walk. Gridfinity STL generation from `generator_params_json` only if/when printed bins are adopted. This is labour-bound, not code-bound | — |
| **2** | The station: printed fixture, PN532 bottom-tag auto-identify, ESP32 + NAU7802 scale, `deviceagent` + station state machine, differential take-by-weight, container tare. Plus projects/BOM/reservations/shortages and KiCad BOM import | Vision counting, absolute recount, mass calibration UI |
| **3** | Vision counting: backlit tray, HQ camera + calibration, Otsu/watershed + median-area two-vote pipeline, overlap detection, inverse-variance fusion with the scale, joint mass+area calibration from a reference batch, `count_sessions` audit trail. Then the **resistor colour-band checker** as a follow-on, reusing the same tray/camera/dome | YOLO fallback |
| **4** | Content-addressed datasheet store, upload, viewer, QR/NFC-to-datasheet wiring, Docling extraction + `datasheet_fts` — **useful standalone: full-text search across every PDF you own** | External providers, LLM |
| **5** | Provider interface + Mouser + jlcparts + DigiKey + Nexar, candidate table, review queue, five MPN decoder families, absolute recount, thermal `ZplBackend` for stick-on part labels | LLM extraction |
| **6** | LLM structured extraction cross-checked against decoders, batched per shared datasheet. Litestream layered *on top of* the nightly logical dump — WAL replication protects against disk loss, a separate-format dump protects against corruption the WAL would faithfully replicate | |

**Side project, explicitly out of the numbered phases:** the IC marking reader. Start with the $50 grazing-light + vision-LLM prototype, timeboxed; build the RTI dome only if that fails on real parts. It must never block or destabilise the counting/tracking pipeline.

**Deferred by design, all additive:** UHF RFID is ruled out, above. Dome lighting for reflective parts and a fine-tuned YOLO+SAHI pass for irregular shapes are Phase 3 follow-ons, not prerequisites. LED locator (`locator_devices`/`locator_channels` are leaf tables; drive via **ESP32 + WLED's JSON API `seg[].i`** — `rpi_ws281x` is broken on Pi 5 since the SoC dropped the legacy PWM/DMA it needed, and WLED's legacy HTTP *and MQTT* APIs only reach segment 0). Multi-user (`stock_ledger.actor_id` exists now as a plain nullable INTEGER with **no FK clause**; add the FK via `batch_alter_table` when an `actors` table arrives. With one user, NULL unambiguously means the owner, so nothing is lost — but note history written before the column is used is permanently unattributable).

## Verification

```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m app.scripts.seed_demo   # container types, packages, units,
                                                          # parameter templates, INBOX, sample parts
docker compose exec api pytest -q
```

- **Unit tests** — `tests/unit/test_value_parser.py` is the single highest-value suite: table-driven over `4k7`, `0R22`, `2M2`, `10n3`, `100nF`, `1u`, `20-30uF`, `≥50V`, `10k ±1%`, and the `1M`-under-capacitance rejection. Also short-ID check-symbol round-trip and transposition detection, capacity strategies, the assignment scorer's determinism, and the counting-math CI formula.
- **ECIA parser** — run against every `tests/fixtures/ecia/*.bin` and diff to `*.expected.json`. Collect real samples from the next few incoming orders and add them as fixtures; they are the only ground truth.
- **Integration** — FastAPI `TestClient` against a temp SQLite file with **real Alembic migrations applied**, to catch model/migration drift. Assert the ledger triggers actually reject UPDATE and DELETE. Assert `qty_milli_cached` matches `SUM(delta_milli)` after a randomized movement sequence, and that the tree cache rebuild is idempotent.
- **Search** — verify the worked example end to end: seed a THT 22 µF ceramic, an SMD 22 µF ceramic and a THT 22 µF electrolytic; confirm the "through-hole 20–30 µF ceramic" query returns exactly one.
- **Fakes, no network in CI** — every provider has a `Fake*Provider` replaying a JSON fixture recorded once from a real response, plus one `@pytest.mark.live` contract test skipped by default to occasionally catch upstream schema drift. The camera is faked by feeding static JPEGs with known codes straight into the decode function. The scale is faked by a `FakeWeightSource` replaying a settling sequence, including a never-settles case to exercise the timeout.
- **Labels without hardware** — render through `FileBackend` and eyeball the PNGs, and validate any generated ZPL against the free `labelary.com` preview API before buying a printer. Print one bin-label card sheet, cut one out, and confirm it friction-fits the printed slot without falling out when the bin is shaken.
- **Vision counting, offline** — a fixture set of tray photographs with hand-verified ground-truth counts, checked into `tests/fixtures/counting/`: an easy monolayer, one with many touching parts, one deliberately double-layered (must be **flagged**, not counted), one with mixed part types (must be **refused**), and one of shiny screws under backlight (documents the known failure). Assert the two-vote pipeline's agreement and that every overlap case trips a detector. This suite is what stops a silent undercount from ever shipping.
- **Fusion math** — property-test that inverse-variance fusion lies between the two inputs, that `|z| > 3` always refuses to fuse, and that a deliberately wrong `unit_mass_mg` reliably trips the disagreement flag rather than being averaged away.
- **NFC** — write a tag from the Android PWA, verify by read-back, then confirm the same tag opens the right part page on an iPhone with no app installed. Then the real test: place a drawer on the station and confirm it identifies **through the platform** at 8–12 mm with the load cell mounted and loaded. Expect bench iteration on antenna centring — this is the design's biggest unknown. Also confirm a 30 mm tag actually sticks flat to one real drawer underside before ordering hundreds (ribbing and draft angles are undocumented for every cabinet model).
- **Provisioning** — bind a full cabinet end to end and time it; target under two minutes for 44 drawers. Then deliberately mis-bind two tags and confirm the verification walk catches both and names the slot each tag actually belongs to. Kill the browser mid-session and confirm the derived cursor resumes at the right slot.
- **Layout guard** — put stock in a drawer, then attempt to shrink the grid; assert a 409 listing that slot, and assert no path in the API ever renumbers an existing slot in place.
- **Station drift** — leave a loaded platform in place for 24 h and log zero drift; PETG creeps under sustained load, so confirm software auto-tare absorbs it rather than the geometry.
- **End-to-end by hand** — provision a bin, put a real part in it, place it on the station, confirm auto-identify and weight, take 5 units by pouring them on the tray and counting, undo, and confirm the ledger shows a compensating row rather than a deletion.
- **Backups** — run `POST /api/system/backup/run`, then restore the dump into a scratch container and confirm `alembic current` and row counts match.
