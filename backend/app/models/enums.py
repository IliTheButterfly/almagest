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
    """Where a parameter value came from.

    Ordered by trust: a manufacturer's own printed table beats an API's
    marketing copy. `PRIORITY` below resolves disagreements.
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


class ProvisioningDevice(StrEnum):
    """What is doing the reading.

    Web NFC is Chromium-on-Android only, so the phone path simply does not exist
    on iOS or on the Pi kiosk — the station reader is not a nicety, it is the
    only path for those.
    """

    PHONE_WEBNFC = "phone_webnfc"
    STATION_PN532 = "station_pn532"
    MANUAL = "manual"


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
