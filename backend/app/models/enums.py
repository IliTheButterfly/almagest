"""Every enumerated value in the schema, as Python `StrEnum`.

**These are never `sa.Enum` and never a `CHECK` constraint.** SQLite cannot
alter a `CHECK`, so a `CHECK` enum turns "support a new kind of thing" into a
full table rebuild — and `sa.Enum` silently emits `VARCHAR + CHECK`, so using
it looks harmless and is not. Columns are plain `String`; membership is
enforced by `StrEnumType` at the model layer on write.

This single rule is what keeps every deferred feature in `PLAN.md` purely
additive: adding a ledger kind, a capacity model or an entity type is a new
member here and nothing else.

Because `StrEnum` members *are* `str`, `lot.status == LotStatus.ACTIVE`
compares correctly against a value loaded from the database with no coercion.
"""

from __future__ import annotations

from enum import StrEnum


class EntityType(StrEnum):
    """Discriminator for the shared short-ID space and other polymorphic links.

    One ID space across all object types means a scan resolves without knowing
    what it scanned, and an object that gets reclassified keeps its printed
    label. The type is a *display prefix* only — never parsed from the code.
    """

    PART = "part"
    LOCATION = "location"
    STOCK_LOT = "stock_lot"
    CONTAINER_TYPE = "container_type"
    PART_CATEGORY = "part_category"
    SUPPLIER_PART = "supplier_part"
    DOCUMENT = "document"
    PROJECT = "project"
    DEVICE = "device"


class LedgerKind(StrEnum):
    """What a `stock_ledger` row records.

    A whole-lot move is one `MOVE` row with `delta_milli = 0` and from/to set.
    A partial move is two rows sharing a `group_uuid`: `SPLIT_OUT` (−N) and
    `SPLIT_IN` (+N). Minting a new lot per shelf change would destroy lot
    identity and per-lot cost continuity, so `stock_lots.location_id` is
    mutable and the history lives here instead.
    """

    RECEIVE = "receive"
    CONSUME = "consume"
    ADJUST = "adjust"
    COUNT = "count"
    MOVE = "move"
    SPLIT_OUT = "split_out"
    SPLIT_IN = "split_in"
    SCRAP = "scrap"
    RETURN = "return"


class LedgerSource(StrEnum):
    """How the movement was captured. Drives trust when balances disagree."""

    MANUAL = "manual"
    SCAN = "scan"
    SCALE = "scale"
    VISION = "vision"
    IMPORT = "import"
    API = "api"
    DEFRAG = "defrag"


class LotStatus(StrEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    CONSUMED = "consumed"
    RETIRED = "retired"


class ClientOperationStatus(StrEnum):
    """Lifecycle of a `client_operations` row — write idempotency.

    `IN_PROGRESS` is only ever *observable* if a write path commits partway
    through an operation, and none does: the ledger row and the cached balance
    move in one transaction, so a crash leaves no row at all and the retry
    redoes the work. A row found in this state therefore means something
    committed early, which is a bug worth being able to see rather than a
    normal intermediate state.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CapacityModel(StrEnum):
    """Selects a Python capacity strategy class.

    The *model* is data; the *formula* is code. Storing a capacity formula as a
    string in the database is precisely the over-engineering that made the
    prior art in this space unmaintainable.
    """

    NONE = "none"
    SLOTS = "slots"
    VOLUME = "volume"
    POSITIONS = "positions"
    MASS = "mass"

    #: A measured grid of interchangeable units — Gridfinity's 42 mm pitch being
    #: the reference case. Distinct from `SLOTS` because a slot is a compartment
    #: and a unit is an *area*: a 2x1 bin consumes two units, not one slot, and
    #: counting compartments would report a full baseplate as half empty.
    #:
    #: Adding this member is the entire schema cost of ADR 0002's recursive
    #: container types. Had `capacity_model` been a `CHECK` constraint or an
    #: `sa.Enum`, it would instead have meant rebuilding `container_types` and
    #: every table referencing it.
    GRID_UNITS = "grid_units"


class ChildLayout(StrEnum):
    GRID = "grid"
    LIST = "list"
    NONE = "none"


class SlotLabelScheme(StrEnum):
    ROW_ALPHA_COL_NUM = "row_alpha_col_num"
    SEQUENTIAL = "sequential"
    CUSTOM = "custom"


class SizeClass(StrEnum):
    """Last resort in the item-dimension cascade."""

    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    BULKY = "bulky"


class VolumeSource(StrEnum):
    """Which rule in the dimension cascade produced a part's volume.

    Recorded so the UI can say "estimated from package 0603" instead of showing
    a precise-looking number that is really a guess.
    """

    OVERRIDE = "override"
    DIMENSIONS = "dimensions"
    PACKAGE_TYPE = "package_type"
    CATEGORY = "category"
    SIZE_CLASS = "size_class"


class ValueType(StrEnum):
    NUMERIC = "numeric"
    ENUM = "enum"
    BOOL = "bool"
    TEXT = "text"


class SubstitutionDirection(StrEnum):
    """What satisfies a requirement when searching for a substitute.

    Substitution search reuses the identical filter executor with a swapped
    operator table, so there is no second query engine — and it stays
    deterministic. An LLM returns plausible substitutes; a plausible substitute
    with the wrong voltage rating is a field failure.
    """

    HIGHER_OK = "higher_ok"
    LOWER_OK = "lower_ok"
    RANGE_OVERLAP = "range_overlap"
    EXACT = "exact"


class Provenance(StrEnum):
    """Where a parameter value came from — and, for a candidate, its **source**.

    Ordered by trust: a manufacturer's own printed table beats an API's
    marketing copy. `PRIORITY` below resolves disagreements.

    One enum serves both `parameter_value.provenance` and
    `parameter_value_candidate.source`, deliberately: promotion copies the
    candidate's source straight into the value's provenance, so a second enum
    would only create a mapping table to get out of step with.

    **Adding a source is a one-line change here and nothing else, because there
    is no `CHECK` constraint on either column.** That rule has now paid three
    times: `CapacityModel.GRID_UNITS` for ADR 0002's recursive container types,
    the scanning enums (`AliasKind`, `ScanDecodedKind`) grown after
    `scan_events` was already populated, and now this — a new provider is a
    member here plus a row in `PROVENANCE_PRIORITY`, with no rebuild of
    `parameter_value`, `parameter_value_candidate`, or anything referencing
    them. Under `sa.Enum` each of the three would have been a full table
    rebuild of a table already holding data.
    """

    MANUAL = "manual"
    DATASHEET_TABLE = "datasheet_table"
    MPN_DECODER = "mpn_decoder"
    DISTRIBUTOR_FREETEXT = "distributor_freetext"
    LLM_INFERRED = "llm_inferred"


#: Higher wins. `manual > datasheet_table > mpn_decoder > distributor_freetext
#: > llm_inferred`, straight from the design doc.
PROVENANCE_PRIORITY: dict[str, int] = {
    Provenance.MANUAL: 50,
    Provenance.DATASHEET_TABLE: 40,
    Provenance.MPN_DECODER: 30,
    Provenance.DISTRIBUTOR_FREETEXT: 20,
    Provenance.LLM_INFERRED: 10,
}


class CandidateStatus(StrEnum):
    """Where one `parameter_value_candidate` row stands.

    The review queue is exactly `status = 'pending'`, which is why the other
    three members all describe a *closed* candidate rather than a stage of
    processing: enrichment either produced something the rules could act on, or
    it produced work for a human, and there is no third state in between.
    """

    #: In the review queue. The default, because a candidate that the promotion
    #: rules decline to act on must remain visible rather than evaporate.
    PENDING = "pending"
    #: This candidate's value is what `parameter_value` now holds, written
    #: through `app.services.parameters` like every other value.
    PROMOTED = "promoted"
    #: A human looked and said no. Sticky: re-running the same extraction must
    #: not resurrect it as pending, which is the same reasoning as
    #: `LayoutSuggestionStatus.DISMISSED`.
    DISMISSED = "dismissed"
    #: Closed without anyone looking, because looking would be pointless: the
    #: field already holds a value this candidate agrees with, or a
    #: more-trusted candidate for the same field was promoted instead. Kept
    #: distinct from `DISMISSED` precisely because no human judgement is
    #: recorded here — it is derived, and recomputable from the rows.
    SUPERSEDED = "superseded"


class CandidateReviewReason(StrEnum):
    """Why a candidate is in the queue instead of in `parameter_value`.

    Enumerated rather than free text because these are the ways enrichment is
    allowed to decline, and a queue that cannot be filtered by them is a queue
    nobody works through. Every one of them is a *refusal to guess*, so none of
    them is an error.

    **A pending candidate always carries one.** `parameter_value_candidate`
    documents this column as "why it is still pending"; a pending row with a
    NULL reason is unexplainable in the queue UI and invisible to every filter,
    which is the same silent-omission failure the rest of this design is built
    to avoid. Adding a member here is purely additive precisely because the
    column is `StrEnumType`, never a `CHECK` — which is what makes covering a
    newly-discovered refusal cheap enough that leaving it NULL is never the
    convenient option.
    """

    #: Single-source, field empty, but `confidence < 0.8`. The plain threshold
    #: from the design doc.
    LOW_CONFIDENCE = "low_confidence"
    #: The field already holds a value and this candidate disagrees with it.
    #: Covers the late-higher-priority-source case: the priority order decides
    #: which source to *believe*, not whether a background job may silently
    #: rewrite a value a human may already have ordered stock against.
    FIELD_OCCUPIED = "field_occupied"
    #: Two or more sources offered values that do not agree within tolerance.
    #: The single strongest signal that a human should look, and the reason the
    #: MPN-decoder cross-check exists at all.
    SOURCES_DISAGREE = "sources_disagree"
    #: The raw value could not be parsed against the template, or names a
    #: `parameter_choice` that does not exist. The text is kept verbatim
    #: anyway — an unparseable value is a grammar gap to fix, but only if the
    #: string survived.
    UNPARSEABLE = "unparseable"
    #: The raw value parsed, but as a **one-sided limit** (`>=50V`, `<100nF`):
    #: it states a bound, not the part's value. Search is an interval-overlap
    #: test (`value_min <= hi AND value_max >= lo`), so storing one would leave
    #: the other bound NULL and make the part invisible to every range query,
    #: silently. Distinct from `UNPARSEABLE` because the grammar is fine and
    #: there is no gap to fix — what is needed is a two-sided correction, which
    #: is a different instruction to give a reviewer.
    ONE_SIDED_LIMIT = "one_sided_limit"
    #: The observation is of a kind that must never auto-promote whatever its
    #: confidence says: an OCR'd or model-read part number, or a printed
    #: marking whose meaning depends on which component you are holding
    #: (`104` is 100 kΩ on a resistor and 100 nF on a capacitor).
    REQUIRES_HUMAN = "requires_human"


class PromotionOutcome(StrEnum):
    """What one evaluation of a field's candidates did.

    **Never persisted** — like `EscalationLevel` and `ShortageKind` — because it
    is derivable from the resulting `CandidateStatus` rows at any time, and a
    stored copy would be a second version of the same fact to get out of step.
    It lives here so every enumerated "kind of thing" in the system stays in one
    file.
    """

    #: A candidate crossed into `parameter_value`, through
    #: `app.services.parameters` like every other write.
    PROMOTED = "promoted"
    #: The field already held a value the candidates agree with. Nothing was
    #: written and nothing needs a human.
    ALREADY_SATISFIED = "already_satisfied"
    #: At least one candidate needs a human. The rules refused to guess.
    QUEUED = "queued"
    #: Nothing was pending for this field.
    NOTHING_PENDING = "nothing_pending"


class CrossCheckVerdict(StrEnum):
    """What the MPN decoder had to say about one model-extracted field.

    **Never persisted**, like `PromotionOutcome`: it is recomputable from the two
    candidate rows any time, and the durable record of a disagreement is already
    `CandidateReviewReason.SOURCES_DISAGREE` plus both rows sitting in the queue.
    A stored copy would be a second version of that fact.
    """

    #: The decoder read the same value out of the part number. Two sources that
    #: fail for unrelated reasons landed on one answer.
    CONFIRMED = "confirmed"
    #: The decoder read a *different* value. Never resolved by averaging and
    #: never by whichever confidence is higher — see `cross_check`.
    CONFLICT = "conflict"
    #: Nothing to check against: no family claimed the part number, the field is
    #: not in that family's table, or one of the two values could not be parsed.
    #: The model's own confidence then faces the plain 0.8 bar alone.
    UNCHECKED = "unchecked"


class IdentityRefusal(StrEnum):
    """Why a model-read part number was not attached to any part.

    **Never persisted, and deliberately not a status on a row** — nothing is
    written at all. "Never auto-accept a model-read part number" is a refusal,
    and a refusal needs no storage to be correct; the caller is handed the
    number, the text it was read from, and the values that were discarded.
    """

    #: No catalogue part has this `mpn_norm`. The model may have found a real
    #: sibling variant in the table, or invented one; both look identical from
    #: here, which is the entire reason a human decides.
    NO_MATCH = "no_match"
    #: More than one part shares this `mpn_norm` — two manufacturers' parts
    #: normalising alike. Picking either would attach a datasheet's values to a
    #: coin flip.
    AMBIGUOUS = "ambiguous"


class LayoutSuggestionKind(StrEnum):
    """What kind of defrag opportunity a `layout_suggestions` row describes.

    Only `OVERFULL` is actually *written* by this phase's code — from capacity
    occupancy rebuilds flagging a location over capacity, and from
    auto-assignment's defrag escalation. The other five are structural
    provision (table columns, this enum, the move-plan shape) for a later
    nightly full-warehouse defrag pass that is out of scope here; see
    `docs/PLAN.md`, "Capacity and auto-assignment".
    """

    MERGE_LOTS = "merge_lots"
    MERGE_BINS = "merge_bins"
    PROMOTE_HOT = "promote_hot"
    DEMOTE_COLD = "demote_cold"
    RETIRE_EMPTY = "retire_empty"
    OVERFULL = "overfull"


class LayoutSuggestionStatus(StrEnum):
    """Dismissals stick: once a human dismisses a suggestion, regenerating the
    same opportunity must not resurrect it as a fresh row."""

    PENDING = "pending"
    DISMISSED = "dismissed"
    APPLIED = "applied"


class EscalationLevel(StrEnum):
    """Which rung of the auto-assignment escalation ladder produced an answer.

    Not written to any table — nothing currently persists an assignment
    decision directly — but kept here anyway so every enumerated "kind of
    thing" in the system stays discoverable in one file rather than
    fragmenting across services. A scan is never rejected: the ladder always
    bottoms out at `INBOX`, which is why that is a member here rather than an
    exception type.
    """

    DIRECT = "direct"
    SOFT_PREFERENCES_DROPPED = "soft_preferences_dropped"
    MATERIALIZED_CELL = "materialized_cell"
    DEFRAG_PLAN = "defrag_plan"
    NEW_SIBLING = "new_sibling"
    INBOX = "inbox"


class AliasKind(StrEnum):
    """*Which part of a scanned label* a `barcode_aliases.code_norm` is.

    `entity_type` says what the alias points at and `symbology` says what
    carried it; this says what was bound, which is the only one of the three
    the resolver has to know in order to look anything up. The distinction is
    load-bearing rather than descriptive: a whole DigiKey reel payload contains
    the lot and the quantity, so binding it resolves *that one reel* forever
    and carries `hint_qty_milli`/`hint_batch` with it — while binding the
    supplier SKU lifted out of the same payload is what makes the *next* reel
    of the same part resolve on its first scan.
    """

    #: The complete normalised payload. What "bind this code" writes by default.
    WHOLE_PAYLOAD = "whole_payload"
    #: A manufacturer part number, from a structured payload or typed in.
    MPN = "mpn"
    #: A distributor's own ordering code (ECIA `1P`), which is why an alias may
    #: legitimately target a `supplier_part` rather than a `part`.
    SUPPLIER_SKU = "supplier_sku"
    #: Lot, serial or tracking code identifying **one physical package**
    #: (ECIA `1T`/`S`). Binding one is how a specific bag gets recognised.
    PACKAGE_CODE = "package_code"
    #: An NFC tag UID, the fallback carrier when the NDEF record is unreadable.
    TAG_UID = "tag_uid"


class ScanDecodedKind(StrEnum):
    """Which handler of the ordered resolver chain claimed a payload.

    Recorded on every `scan_events` row because it is the measurement that
    tells us where intake actually hurts: a rising share of `UNKNOWN` means a
    vendor format nobody parses yet, and the raw payloads to build that parser
    from are sitting in the same table.
    """

    SHORT_ID = "short_id"
    ALIAS = "alias"
    ECIA = "ecia"
    LCSC = "lcsc"
    MPN = "mpn"
    EAN = "ean"
    UNKNOWN = "unknown"


class ScanAction(StrEnum):
    """What the system did with a scan.

    Not the same thing as `ScanDecodedKind`: a payload can decode perfectly and
    still resolve to nothing (a known-format label for a part we have never
    stocked), and both facts are worth keeping separately.
    """

    #: Exactly one entity matched.
    RESOLVED = "resolved"
    #: Several candidates matched; the user was asked to choose.
    AMBIGUOUS = "ambiguous"
    #: Nothing matched. The UI offers "bind this code", which is the entire
    #: alias-learning loop — so this is a prompt, never an error.
    UNRESOLVED = "unresolved"
    #: The user taught a binding for this payload; a `barcode_aliases` row was
    #: written and the same scan resolves at step 2 from now on.
    BOUND = "bound"
    #: Dropped by the duplicate hold-off — one label held in front of a camera
    #: must not fire five resolves.
    SUPPRESSED = "suppressed"


class ScanSourceKind(StrEnum):
    """What kind of reader a `scan_sources` row describes."""

    #: `getUserMedia` + `zxing-wasm` on a phone. The primary intake path,
    #: because autofocus is what makes a dense DataMatrix readable at all.
    BROWSER_CAMERA = "browser_camera"
    #: A USB HID wedge — it is a keyboard, so it needs no driver and no code.
    HID_WEDGE = "hid_wedge"
    NFC_READER = "nfc_reader"
    #: Mensa, the bench station: identifies, weighs and counts a container.
    BENCH_STATION = "bench_station"
    #: A human typing a code off a label the camera could not read.
    MANUAL_ENTRY = "manual_entry"


class ProvisioningKind(StrEnum):
    """What a provisioning session is doing.

    Two kinds rather than one with a flag, because they have opposite
    postconditions: provisioning *creates* bindings, verification asserts the
    ones that exist are right and creates none.
    """

    PROVISION = "provision"
    VERIFY = "verify"


class ProvisioningActionKind(StrEnum):
    """What one step of a walk did — the log both the cursor and the undo stack
    are derived from.

    `MOVE` and `REBIND` are separate members rather than a `BIND` with a flag
    because undo has to put something *back*, and the two priors live in
    different places: a move displaced a binding at another slot, a rebind
    displaced a different tag at this one. Collapsing them would leave undo
    guessing which.
    """

    #: A slot that had no tag now has one.
    BIND = "bind"
    #: A tag bound elsewhere was moved here, after the human confirmed it.
    MOVE = "move"
    #: This slot's tag was replaced by a different one.
    REBIND = "rebind"
    #: Deliberately left empty and advanced past. The one fact a cursor derived
    #: from `location_tags` cannot recover on its own — a skipped slot is still
    #: a slot with no tag — which is why it is written down.
    SKIP = "skip"
    #: A tag re-read during a verification walk and found to be the expected
    #: one. Recorded so the verify cursor is derivable too; a mismatch
    #: deliberately writes *no* row here, which is what makes the walk stop.
    CHECK = "check"


class ProvisioningDevice(StrEnum):
    """What is doing the reading.

    Web NFC is Chromium-on-Android only, so the phone path simply does not exist
    on iOS or on the Pi kiosk — the station reader is not a nicety, it is the
    only path for those.
    """

    PHONE_WEBNFC = "phone_webnfc"
    STATION_PN532 = "station_pn532"
    MANUAL = "manual"


class TagGranularity(StrEnum):
    """Per-instantiation choice of which new locations get a printed
    `short_id` (and are therefore taggable) when a container type is
    instantiated.

    Deliberately a per-call request field, never a property of the type:
    "tagging only the cabinet and picking the drawer on screen" cuts ~90% of
    the physical labour and is the right call whenever the pick already
    involves a screen, but per-drawer tags earn their cost when drawers
    physically travel to the station — the same cabinet type is legitimately
    instantiated both ways on different days.
    """

    #: The container root gets a short_id; its slots stay addressed as
    #: `parent short_id + slot label` — no drawer-level printed identity.
    CONTAINER = "container"
    #: Every generated slot *also* gets its own short_id, on top of the
    #: container's — a superset of `CONTAINER`, not a replacement for it — so
    #: each drawer is individually tappable during a later provisioning walk.
    SLOT = "slot"


class LabelTemplate(StrEnum):
    """Which card layout `app.services.labels` draws.

    `PART_LOT` has no route yet — the stick-on part/lot label is Phase 5's
    thermal backend, per `docs/PLAN.md`'s phasing table — but it is defined
    now so the QR-plus-MPN-caption rule ("print the bare MPN as text under
    every part QR") has a home in the renderer's template set from day one
    rather than needing a second code path bolted on when that phase starts.
    """

    DRAWER_CARD = "drawer_card"
    CABINET_CARD = "cabinet_card"
    PART_LOT = "part_lot"


class LabelBackendKind(StrEnum):
    """Which `LabelBackend` implementation rendered a `label_sheet_jobs` row.

    Only the two hardware-free backends exist this phase. The thermal
    backends `docs/PLAN.md` sketches (`ZplBackend`, `TsplBackend`,
    `BrotherQlBackend`, `CupsBackend`, `AgentBackend`) are deliberately not
    implemented here — no printer is owned, and the design commits to none.
    """

    FILE = "file"
    PDF_SHEET = "pdf_sheet"


class TagPocket(StrEnum):
    """Where the NFC tag lives on a printed container.

    `BOTTOM` is the default and the reason the station needs no scanning
    gesture: with the reader antenna under the platform, a container set down
    identifies itself.
    """

    BOTTOM = "bottom"
    FRONT = "front"
    INSIDE = "inside"
    NONE = "none"


class PendingIntakeStatus(StrEnum):
    """Where a parked scan is in the intake worklist.

    Resolved and dismissed rows are **kept, not deleted**. The raw payload is
    the asset — a vendor format nobody parses yet is a parser waiting to be
    written, but only if the bytes survived — and "what did I scan last
    Tuesday" is a question worth being able to answer. This is a worklist
    rather than a ledger, so there are no triggers here; it just does not throw
    the evidence away.
    """

    #: Waiting to be walked at a desk. The whole point of the fast path.
    PENDING = "pending"
    #: Dealt with — a part was created, or stock received against an existing
    #: one. `resolved_part_id` records what it became, when there was one.
    RESOLVED = "resolved"
    #: Not a real intake after all: a duplicate scan, a shipping label, a box
    #: someone else's. Distinguished from `RESOLVED` because the two mean
    #: opposite things about whether the payload is worth mining.
    DISMISSED = "dismissed"


class ProjectStatus(StrEnum):
    """Where a project sits in its own lifecycle.

    Deliberately about the *design*, not about building it — a project has many
    builds and each carries its own `BuildStatus`, so "half built" is never a
    fact about this column. `ARCHIVED` exists so a finished design can leave
    the default list without being deleted, because deleting it would take its
    BOM (and therefore the answer to "what was in that board") with it.
    """

    PLANNING = "planning"
    ACTIVE = "active"
    ARCHIVED = "archived"


class BuildStatus(StrEnum):
    """Where one *run* of building a project sits.

    Distinct from `AllocationState` and not derivable from it: a build with
    every allocation consumed may still be open because the human has not said
    they are done, and a build with no allocations at all is a legitimate empty
    plan. The two are checked together — closing a build that still holds
    `RESERVED` rows is what releases them, which is the only reason the
    reserved cache cannot drift upwards forever.
    """

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    #: Given up on. Its reservations are released; its `CONSUMED` allocations
    #: stay, because the parts really were used and the ledger says so.
    ABANDONED = "abandoned"


class AllocationState(StrEnum):
    """What a `stock_allocations` row currently asserts about stock.

    The set is chosen so that **`stock_lots.qty_reserved_milli_cached` is
    exactly `SUM(qty_milli) WHERE state = 'reserved'`**, per lot, and nothing
    else. That single-predicate definition is the whole design: the reserved
    cache is derived and rebuildable in one statement (see
    `app.models.projects.StockAllocation`), never a hand-maintained counter
    that drifts and cannot be reconstructed.

    The consequence to keep in mind when adding a member: any new state that
    should hold stock has to be added to that predicate *and* to the rebuild,
    or the cache silently stops matching. Prefer states that do not.

    Two states people expect and that are deliberately **not** here:

    * *shortage* — demand exceeding available stock is computed by comparing
      the BOM to `qty_milli_cached - qty_reserved_milli_cached`. Storing it
      would be a cache of a cache, wrong the moment anything is received.
    * *substituted* — allocating an alternate is an ordinary allocation whose
      `part_id` is the substitute. A separate state would mean every consumer
      of this enum had to know that it also counts as reserved.
    """

    #: Demand, with no specific lot chosen: `lot_id IS NULL`. Holds nothing, so
    #: it never touches the reserved cache. This is what a freshly imported BOM
    #: expands into, and it is the state a line with no matched part can still
    #: legitimately be in — the shortage report is built from these.
    PLANNED = "planned"
    #: A named lot is held for this build. `lot_id IS NOT NULL`, and **these
    #: rows and only these rows** sum into `qty_reserved_milli_cached`.
    RESERVED = "reserved"
    #: The parts left the bin. The ledger row that recorded it is pointed at by
    #: `consumed_ledger_seq`, so this stops counting as reserved at the same
    #: instant `qty_milli_cached` drops — double-counting a pick is otherwise
    #: the obvious way to make available stock read low forever.
    CONSUMED = "consumed"
    #: The hold was given back: build cancelled, line deleted, or a
    #: re-plan picked a different lot. Kept rather than deleted so "we planned
    #: this and dropped it" stays visible and an undo has something to point
    #: at; harmless to the cache because the predicate excludes it.
    RELEASED = "released"


class ShortageKind(StrEnum):
    """What stands between one BOM line and being built.

    **Never persisted**, like `EscalationLevel` — a shortage is derived by
    comparing demand to `qty_milli_cached - qty_reserved_milli_cached`, so
    storing it would be a cache of a cache that is wrong the moment anything is
    received. It lives here anyway so every enumerated "kind of thing" stays in
    one file.

    `UNIDENTIFIED` and `SHORT` are deliberately **separate members and not one
    "cannot build" flag.** They are different problems with different fixes: a
    short line needs stock ordered, an unidentified line needs a human to say
    what the part *is*, and no quantity can be computed for it at all. Folding
    the second into the first — or worse, treating an unmatched line as needing
    zero — is what makes a BOM report "buildable" for a board nobody can build.
    """

    #: Enough free stock (its own or an accepted alternate's) exists for the
    #: outstanding demand, given what this build already holds.
    SATISFIED = "satisfied"
    #: A known part, a known number missing. Actionable by ordering.
    SHORT = "short"
    #: `bom_lines.part_id IS NULL`. The requirement is known; the *thing* is
    #: not, so availability and shortfall are `None` rather than zero.
    UNIDENTIFIED = "unidentified"
    #: `is_dnp` — in the file, not on the board. Generates no demand, and is
    #: reported rather than dropped so the BOM the user sees is the whole BOM.
    NOT_FITTED = "not_fitted"
