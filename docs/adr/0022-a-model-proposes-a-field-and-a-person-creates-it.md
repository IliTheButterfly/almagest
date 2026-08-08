# ADR 0022 — A model proposes a filterable field; a person creates it

**Status:** proposed
**Date:** 2026-08-08

## Context

Datasheets state parameters this install has no field for. That is not a
hypothetical: `cross_check.IngestReport.unknown_templates` has been computed on
every extraction run since ADR 0005's pipeline landed, and it means exactly "this
datasheet named a parameter and there is no `parameter_template` for it". **Nothing
persisted it.** Worse, the *useful* half was dropped with it — which datasheet said
so, which part it was stated for, the value, and the line it was read from. All
four are what somebody deciding "should capacitors have an ESR field?" needs, and
none is recoverable afterwards, because the extraction response is not stored.

The ask was for the 27B model to read discovered datasheets and "add pertinent
missing fields". Adding them directly was raised and rejected, and the reason is
worth writing down because it is not the usual one.

**The dangerous failure is not a bad row. It is a duplicate.** A
`parameter_template` is shared by every part in its category, and a second template
for one concept — `Rds(on)` beside `on_resistance` — breaks search *silently*:

* the parametric filter is a join on `template_id`, so a MOSFET with 45 mΩ recorded
  under one name reads as having **no** on-resistance under the other;
* `parameter_template.substitution_direction` then decides substitutions on
  whichever half it can see, and "a plausible substitute with the wrong rating is a
  field failure";
* `UNIQUE(part_id, template_id)` on `parameter_value` still holds and now
  guarantees nothing useful, because the fan-out it prevents was never the problem.

A wrong `base_unit` is the same harm arrived at from the other end: every
`value_min`/`value_max` written against the template was computed under that
quantity, so the numbers keep answering range queries while meaning something else.
`app.services.parameter_fields` already documents both as *invariants of the search
engine* rather than input validation.

Neither failure raises anything. Both look exactly like a working install.

## Decision

**Anything automated may record a *proposal* for a field, with the datasheet line
that states it. `POST /api/parameter-fields` remains the only writer of
`parameter_template`, and a person presses the button.**

Four parts.

### 1. The proposal is stored, with per-document evidence

`parameter_field_proposals` (one row per proposed field, keyed on a **normalised**
name) plus `parameter_field_proposal_evidence` (one row per document that stated
it). The split is the design: a heading appearing in five unrelated sheets is a real
field, and the same heading five times from one sheet is that manufacturer's private
vocabulary. That count is the headline signal, so it is a `COUNT` over rows rather
than a counter column nothing could rebuild.

Every evidence row carries a verbatim `quote`, `NOT NULL` and non-empty by
service-level refusal — the same contract `ExtractedField.source_text` and
`IdentityCandidate.source_text` carry, for the same reason. A proposal nobody can
trace back to a line of a datasheet cannot be reviewed, and an unreviewable
assertion is what this pipeline exists not to store.

`FieldProposalStatus` has **no `created` member**. `ACCEPTED` records that a person
made the field, with `created_template_id` pointing at what came out.

### 2. The duplicate check runs before a proposal is ever shown

`field_proposals.nearest_existing_template` is a named, tested function, and it is
what earns the feature. A ladder, strongest first:

| Rung | What it is |
|---|---|
| `exact_name` | Normalised names identical — `Voltage Rating` against `voltage_rating` |
| `display_name` | The name equals an existing template's normalised *display* name |
| `synonym` | Both are in one curated group in `SYNONYM_GROUPS` — `Rds(on)` and `on_resistance` |
| `tokens` | Shared word stems **and** a compatible quantity |

`synonym` is the rung that matters, and it is **curated content in the module**
rather than anything inferred. The duplicate that actually happens is a datasheet's
symbol against an inventory's word; the two share no stem and no spelling, so no
amount of string comparison connects them, and a *model* deciding two fields are the
same concept would be the assertion this whole feature refuses to accept. The list
is therefore short and conservative: a wrong entry silently merges two real fields,
which is the same class of harm as the duplicate it prevents.

`tokens` requires the unit as well as the words, and that is not decoration:
`rise_time` and `rise_rate` share a stem and are different fields; seconds against
volts-per-second is what says so. An *unstated* unit is treated as compatible with
everything, because "unknown" must not read as "different" — the cost of a false
near-match is one extra line to read, and the cost of a missed one is the duplicate.

Recording re-runs the match, so a template created since the first sighting turns
"nothing resembles this" into "merge into that one" rather than leaving a stale
answer a reviewer would trust.

### 3. The model pass follows `ExtractionProvider`'s split, and is not scheduled

`services/enrichment/field_discovery.py` is the pure half (request, schema, parser,
fake) and `field_discovery_openai_compat.py` the transport, exactly as `extract.py`
and `vision.py` are arranged. The prohibitions are unrepresentable in the schema
rather than written as rules, following `vision.schema_for`:

* `category` is an `enum` of slugs that exist — a model cannot invent one, and a
  fabricated slug would either offer the field nowhere or offer it on a subtree
  nobody chose;
* `base_unit` is an `enum` of quantities the value parser answers to;
* `quote` is required, non-empty and bounded;
* **there is no `substitution_direction` field.** Guessing it produces a
  substitution search that is wrong by construction while looking like it works, so
  the person filling the create form supplies it, and the form does not default it
  either;
* `value_type` is `numeric` or `enum` only — `app.services.parameters` has no writer
  for `text` or `bool`, so such a field could be created and never filled in.

`app/scripts/propose_fields.py` is `--sha256` plus `--once`/`--fixture`. **There is
no queue and nothing schedules it**, deliberately: a 27B on the one card is a second
GPU consumer that would compete with the capture-dispatch drain (ADR 0021), and that
contention needs its own decision rather than arriving as a side effect. The
consequence is stated rather than hidden — a datasheet uploaded today produces no
proposals until somebody asks.

### 4. There is exactly one door onto `parameter_template`

`POST /api/field-proposals/{id}/decision` records a decision and **creates
nothing**. `accepted` requires a `field_id` that already resolves, obtained by the
client calling `POST /api/parameter-fields` itself with every guard applied.
Accepting without one is refused rather than treated as "create it for me", because
that branch is the one a later edit would grow into a second creation path.

This is the shape the intake queue already keeps: a vision run reaches `PROPOSED`
and stops, and a chosen candidate becomes real through the existing
`POST /api/intake/pending/{id}/resolve`.

Over MCP, `create_parameter_field` stays `Excluded(Reason.AUTHORING)` and
`propose_parameter_field` is what a model gets instead — write-gated behind
`ALMAGEST_MCP_ALLOW_WRITES` like the movement tools, and the one write there that
touches no stock.

## Consequences

**What this buys.** The signal that was being discarded is now durable and needs no
model at all: `cross_check.ingest` records a proposal with the quote, the part, the
value and the page every time an extraction names a field that does not exist. The
model pass adds coverage of parameters no extraction request thought to ask for.

**What it costs.**

* **A person is in the loop, per field.** That is the point, and it is also the
  bottleneck: a hundred proposals is a hundred decisions. Ordering by likeness to an
  existing field is the mitigation — the rows that must not be waved through are at
  the top.
* **`SYNONYM_GROUPS` is content somebody maintains.** It will be incomplete, and an
  incomplete list fails in the safe direction (a duplicate offered, not a merge
  forced). A *wrong* entry fails in the unsafe direction, which is why every group
  is a pair or triple checkable at a glance and why two tests refuse a one-member
  group and a name in two groups.
* **The seen-count is of documents, not parts.** One family datasheet covers many
  part numbers, so `evidence.part_id` names one of them rather than all of them.
  Stated in the model rather than left to be discovered.
* **Retention is unbounded and nothing prunes**, the same gap `model_runs` has.
* **No 27B run has happened.** The whole path is proved offline:
  `FakeFieldDiscoveryProvider` replays a fixture through the **real** parser, over
  real migrations and the real routes. The fixture is hand-authored and says so, and
  the transport has never spoken to a model — exactly the position ADR 0021 was in
  before 2026-08-07, and it should be treated with the same suspicion until a live
  run corrects it.

## Alternatives rejected

**Let the model create the field.** What was asked for, and what the duplicate
argument above rules out. A wrong field definition is not a row somebody notices; it
is a search that quietly stops matching.

**One `accept` route that creates the field and closes the proposal.** Fewer calls
from the screen, and a second writer of `parameter_template` that would drift from
`parameter_fields`' four guards the first time either changed.

**Widen `parameter_value_candidate` instead of new tables.** A proposal has no
`part_id` and no `template_id` — the template is precisely what does not exist — so
every column would have had to become nullable, which is how that table's
observation-uniqueness rule stops constraining anything.

**Match on names alone and skip the curated synonyms.** Cheaper, and it misses the
only case that matters. `Rds(on)` and `on_resistance` normalise to `rdson` and
`onresistance`; nothing mechanical bridges that.
