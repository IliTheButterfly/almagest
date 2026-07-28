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
