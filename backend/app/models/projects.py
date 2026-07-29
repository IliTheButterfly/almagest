"""Projects, BOMs, builds and the allocations that reserve stock for them.

Five tables, and the one non-obvious thing about them is where reservations
live: **nowhere.** `stock_lots.qty_reserved_milli_cached` is a *cache* of
`SUM(stock_allocations.qty_milli) WHERE state = 'reserved'`, per lot. Nothing
increments it as an authoritative counter, because a hand-maintained
reservation counter drifts — a crashed pick, a half-applied cancel, a
concurrent release — and once it has drifted there is no source of truth left to
rebuild it from. Everything in this module is arranged so the rebuild is the one
statement in :data:`RESERVED_CACHE_REBUILD_SQL`, which
`ix_stock_allocations_reserved_lot` makes cheap. The `reservations` row already
seeded in `cache_state` is where the nightly drift check for it belongs.

The other load-bearing choice is that **`bom_lines.part_id` is nullable**. An
imported KiCad BOM has to land intact even when a line names a part that is not
in the catalogue and cannot be matched — that is the entire reason import is
cheap enough to be used. So every imported field (designators, value, footprint,
the raw CSV row) is carried on the line itself, and an unmatched line is still a
row a human can act on rather than an import that failed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin
from app.models.enums import AllocationState, BuildStatus, ProjectStatus
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: **The definition of "reserved", as one SQL expression**, with `{lot}`
#: standing in for however the caller names the lot: a correlated column
#: (`stock_lots.id`) in a bulk statement, a bind parameter (`:lot_id`) when
#: reading or verifying a single lot.
#:
#: Parameterised rather than written out per caller because the alternative has
#: already gone wrong here once: a bulk rebuild and a single-row read that
#: computed the same quantity two different ways, disagreed, and the bulk path
#: was the one that persisted. Every consumer — the rebuild below, the drift
#: check in `app.db.maintenance`, the per-lot verification in
#: `app.services.reservations` — formats *this* string, so there is one
#: predicate to get right and a divergence is not expressible.
#:
#: `COALESCE(..., 0)` is doing real work — a lot with no reservations gets 0, not
#: NULL, so the column stays NOT NULL and `qty_milli_cached -
#: qty_reserved_milli_cached` never evaluates to NULL.
#:
#: **ADR 0004 added `AllocationState.STAGED` and did not touch this line.** That
#: is not an omission: a staged row's parts have physically left the source lot,
#: so including them here would hold stock in a drawer that no longer contains
#: it *while* the same units also sit as real stock in the project's staging
#: location — the double count the ADR spends a section on. A staged row holds
#: stock at its new place in the ordinary way, so the predicate stays a single
#: indexable equality and `ix_stock_allocations_reserved_lot` still covers it.
RESERVED_SUM_SQL = (
    "COALESCE((SELECT SUM(a.qty_milli) FROM stock_allocations a"
    f" WHERE a.lot_id = {{lot}} AND a.state = '{AllocationState.RESERVED.value}'), 0)"
)

#: **The whole reservation design, as one statement.** Lives here rather than in
#: a service so there is exactly one copy: a second, subtly different rebuild
#: somewhere else would produce a cache that disagrees with the drift check
#: meant to police it, and then neither could be trusted.
#:
#: Rewriting every row rather than only the ones with allocations is deliberate:
#: that is what makes this a *repair*, correcting a lot whose cache is wrong
#: precisely because its last allocation was released.
RESERVED_CACHE_REBUILD_SQL = (
    "UPDATE stock_lots SET qty_reserved_milli_cached = "
    + RESERVED_SUM_SQL.format(lot="stock_lots.id")
)

#: Wide enough for a KiCad designator field on a densely decoupled board —
#: "C1,C2,C3,..." across a hundred capacitors is one line, not a hundred. `Text`
#: would be equally correct on SQLite; the width documents that this is a field
#: copied from an import, never a parsed list.
_DESIGNATORS_LENGTH = 1024


class Project(Base, TimestampMixin):
    """A thing being built.

    Holds the BOM; does *not* hold quantities or progress. Those belong to a
    `ProjectBuild`, because you build v1 twice and the second run's allocations
    are not the first's.

    `name` is deliberately **not unique**. Two revisions of a board legitimately
    share a name (`revision` is what separates them), and a uniqueness failure
    on import is exactly the friction this design spends everything to avoid.
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Free text ("v1", "rev B", a git short SHA), never parsed or ordered. A
    #: build copies this into `project_builds.bom_revision` at plan time, so
    #: editing the BOM afterwards is visible as a revision change rather than
    #: silently rewriting what an old build was built from.
    revision: Mapped[str | None] = mapped_column(String(32))

    #: No index: there are dozens of projects, not thousands, and an index on a
    #: three-valued column over a tiny table only costs writes.
    status: Mapped[str] = mapped_column(
        StrEnumType(ProjectStatus),
        nullable=False,
        default=ProjectStatus.PLANNING,
        server_default=ProjectStatus.PLANNING.value,
    )

    description: Mapped[str | None] = mapped_column(Text)
    #: Where the BOM came from: a repo URL, a KiCad project path, a filename.
    #: Kept so a re-import can be recognised as an update to *this* project
    #: instead of creating a second one beside it.
    source_ref: Mapped[str | None] = mapped_column(String(512))
    notes: Mapped[str | None] = mapped_column(Text)


class ProjectBuild(Base, TimestampMixin):
    """One run of building a project.

    Separate from `projects` because allocations, shortages and consumption are
    all properties of *a run*: building five boards in March and three in
    October are different demands against different stock, and collapsing them
    would make "what did I actually use" unanswerable — which is the only
    question a BOM system exists to answer.
    """

    __tablename__ = "project_builds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: No `index=True`: `ix_project_builds_project_status` below leads with this
    #: column and serves every lookup a bare index on it would, including the
    #: parent-side scan a project delete does.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    #: Sequential per project, assigned by the service. Exists so a build has a
    #: stable human handle ("build 2") that does not move when another build is
    #: deleted — an ordinal computed at display time would.
    build_no: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(128))

    #: How many assemblies this run makes. Demand for a line is
    #: `bom_lines.qty_per_assembly_milli * assembly_count`, computed, never
    #: stored per line — storing it would go stale the moment the count changes.
    assembly_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    #: Copy of `projects.revision` when the build was planned. A copy and not a
    #: reference on purpose: the BOM is mutable, so this is the only record that
    #: an old build was planned against different lines than the ones there now.
    bom_revision: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(
        StrEnumType(BuildStatus),
        nullable=False,
        default=BuildStatus.PLANNED,
        server_default=BuildStatus.PLANNED.value,
    )

    #: Where this build's *floating* parts went — the `locations` row that
    #: represents the project inside the `PROJECTS` staging root (ADR 0004).
    #: NULL until the first withdrawal, because a project that never takes
    #: anything out of stock must not litter the storage tree with an empty box.
    #:
    #: It points at the **project's** node, not a per-build one, so every build
    #: of one project records the same location: physically there is one project
    #: box, and `app.services.staging` looks the node up by a DB-unique key
    #: rather than by name (project names are deliberately not unique). Kept as
    #: a column anyway so a reader — the UI, the delete refusal — never has to
    #: re-derive it, and so `SET NULL` below makes a manually deleted staging
    #: tree self-healing instead of a dangling reference.
    #:
    #: `SET NULL`, not `RESTRICT`: a staging location holding nothing is
    #: ordinary furniture a user may remove, and the next withdrawal recreates
    #: it. `RESTRICT` would instead make deleting an empty project box
    #: impossible for as long as any build of the project exists.
    staging_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL")
    )

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Both columns are NOT NULL, so this constrains what it appears to —
        # unlike a compound unique index over nullable columns, where SQL treats
        # every NULL as distinct and the constraint quietly permits duplicates.
        UniqueConstraint("project_id", "build_no"),
        # "This project's builds, newest first" is the only list view, and the
        # composite serves it without a second index on `project_id` alone.
        Index("ix_project_builds_project_status", "project_id", "status"),
    )


class BomLine(Base, TimestampMixin):
    """One line of a BOM. **`part_id` is nullable, and that is the point.**

    A KiCad export names parts the way the schematic does — a value, a
    footprint, sometimes an MPN field somebody filled in and sometimes not. If
    an unmatched line could not be stored, an import would be all-or-nothing and
    the user would go back to a spreadsheet. So the line lands with whatever the
    file said, `part_id` NULL, and matching becomes a later pass over
    `ix_bom_lines_unmatched` that the human confirms.

    Every imported column is kept **verbatim beside** the resolved one
    (`mpn_raw` next to `part_id`) for the same reason `scan_events` keeps raw
    payloads: a better matcher written next year can be re-run over the
    original text, but only if the original text survived.
    """

    __tablename__ = "bom_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Indexed for real here, unlike `ProjectBuild.project_id`: the composite
    #: below is *partial*, so it cannot serve "every line of this project" —
    #: which is the query that renders a BOM.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Position in the imported file, so the BOM displays in the order the user
    #: saw it in KiCad. Not unique with `project_id`: a re-import that renumbers
    #: lines has to be able to write the new numbers before clearing the old,
    #: and a malformed file with two "line 3"s must still land.
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: `"R1,R4,R7"`, exactly as imported. One string, not a child table: it
    #: arrives as one field, is displayed as one field, and is never joined on.
    designators: Mapped[str | None] = mapped_column(String(_DESIGNATORS_LENGTH))

    #: Per *one* assembly. Thousandths like every quantity here, so a line for
    #: 0.5 m of wire is expressible without a second unit convention.
    qty_per_assembly_milli: Mapped[int] = mapped_column(Integer, nullable=False)

    #: NULL means "this line names something we could not identify" — a legal,
    #: expected, actionable state. `SET NULL` rather than `CASCADE` on delete:
    #: removing a part from the catalogue must revert the line to unmatched, not
    #: delete the line, because the board still has that component on it.
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("parts.id", ondelete="SET NULL"), index=True
    )
    #: Whether a human agreed with `part_id`. An automatic exact-MPN match sets
    #: the part and leaves this false, so a plausible-but-wrong match is never
    #: silently promoted to a decision — the same rule that forbids
    #: auto-accepting an OCR'd part number.
    is_match_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: KiCad's "do not populate". The line still has to exist — it is in the
    #: file, and it gets fitted on the next revision — but it generates no
    #: demand, so shortage math filters on this rather than on the line's
    #: absence.
    is_dnp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    # --- as imported, never overwritten by matching ---
    #: The schematic value: `"4k7"`, `"100nF"`, `"LM358"`. Free text; the value
    #: parser may make sense of it during matching, and must not rewrite it.
    ref_value: Mapped[str | None] = mapped_column(String(128))
    footprint: Mapped[str | None] = mapped_column(String(128))
    mpn_raw: Mapped[str | None] = mapped_column(String(128))
    #: Normalised copy of `mpn_raw` (`app.services.scanning.codes.normalize_mpn`),
    #: indexed so re-running the matcher over an updated catalogue is an indexed
    #: join instead of a table scan per line.
    mpn_norm: Mapped[str | None] = mapped_column(String(128), index=True)
    manufacturer_raw: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    #: The whole source row as a JSON object, including columns this schema has
    #: no home for (supplier SKU, board-house fields, an alternate MPN nobody
    #: has a `parts` row for yet). Written once, read by a human, never queried
    #: by its contents.
    raw_fields_json: Mapped[str | None] = mapped_column(Text)

    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # "What still needs a part?" — the worklist that makes a cheap import
        # honest. Partial, so it holds only the lines that need work and stays
        # small no matter how many BOMs are imported.
        Index("ix_bom_lines_unmatched", "project_id", "line_no", sqlite_where=part_id.is_(None)),
    )


class BomLineSubstitute(Base):
    """An alternate the human accepts *for this line*.

    Distinct from `part_substitutes`, which asserts a substitution globally.
    This one is local and that is the useful case: a 10 kΩ is fine for the pull-up
    on R7 and not fine for the divider on R12, and a global assertion cannot say
    that. Allocation is allowed to satisfy a line from a substitute's stock,
    which is why this table is consulted before a shortage is reported.

    `part_id` is NOT NULL, unlike on the line itself: an alternate that has no
    `parts` row cannot be allocated from, so it is not yet a substitute — it
    stays in the line's `raw_fields_json` until someone creates the part.
    """

    __tablename__ = "bom_line_substitutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: No `index=True`: the unique constraint below leads with this column, so
    #: "the alternates for this line" and the cascade a line delete performs are
    #: both already served.
    bom_line_id: Mapped[int] = mapped_column(
        ForeignKey("bom_lines.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Lower is preferred. Ties are broken by `id`, so the order a user typed
    #: them in survives — an unordered candidate list is one the user has to
    #: re-reason about every time.
    preference: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("bom_line_id", "part_id"),)


class StockAllocation(Base, TimestampMixin):
    """A claim on stock by a build: planned demand, a held lot, or a pick.

    **The reserved cache is derived from this table and nothing else.**
    :data:`RESERVED_CACHE_REBUILD_SQL` is the rebuild; the invariant it depends
    on is that exactly one state — `RESERVED` — holds stock *in the lot it
    names*, so the predicate is a single equality and can be indexed. `STAGED`
    also names a lot and also holds stock, but that lot is the one in the
    project's staging location, which holds it the ordinary way; see
    `AllocationState.STAGED`.

    A `CONSUMED` row stops counting as reserved at the same moment
    `stock_lots.qty_milli_cached` drops, which is why consumption is a state
    transition here and not a delete: deleting would work for the cache and lose
    the answer to "what went into build 2".

    **A delete of a `RESERVED` row still has to move the cache**, and no service
    can promise that: `build_id` is `ON DELETE CASCADE`, so SQLite removes these
    rows itself when a build or project goes, with no Python involved. That is
    what `trg_stock_allocations_deleted_reserved` (created in the migration that
    made this table) is for — the asymmetry with `stock_ledger`, which cannot be
    deleted at all, is exactly why the ledger's cache never needed one.

    There is deliberately **no** `UNIQUE(build_id, bom_line_id, lot_id)`. A line
    is legitimately satisfied from two bins, and a partial pick leaves a
    `CONSUMED` row for a lot the next pick reserves again — so uniqueness would
    reject the second pick from the same bin, which is normal work.
    """

    __tablename__ = "stock_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    build_id: Mapped[int] = mapped_column(
        ForeignKey("project_builds.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL for stock consumed during a build that no BOM line asked for — two
    #: more resistors because one went flying. Forcing a synthetic line for that
    #: would pollute the BOM with things the board does not have.
    #: `SET NULL` on delete so deleting a line does not erase the record that
    #: its parts were really used.
    bom_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("bom_lines.id", ondelete="SET NULL"), index=True
    )

    #: **Not** redundant with the lot's part. A `PLANNED` row has no lot at all,
    #: so this is the only place the demand's identity lives; and when a
    #: substitute is allocated, this is the substitute, not the line's part.
    #: When `lot_id` is set the two must agree — enforced in the service, since
    #: expressing it in DDL would need a `CHECK`.
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False
    )
    #: The physical package being held. NULL exactly while the row is `PLANNED`.
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_lots.id", ondelete="RESTRICT"), index=True
    )

    qty_milli: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[str] = mapped_column(
        StrEnumType(AllocationState),
        nullable=False,
        default=AllocationState.PLANNED,
        server_default=AllocationState.PLANNED.value,
    )

    #: The `stock_ledger.seq` that recorded the pick, set with the transition to
    #: `CONSUMED`. A real FK, so an allocation can never claim a movement that
    #: does not exist — and the ledger's triggers make the reference permanent,
    #: because nothing can delete the row it points at.
    consumed_ledger_seq: Mapped[int | None] = mapped_column(
        ForeignKey("stock_ledger.seq", ondelete="RESTRICT")
    )

    #: The `stock_ledger.seq` that moved these parts into the project's staging
    #: location, set with the transition to `STAGED`. The exact counterpart of
    #: `consumed_ledger_seq`, and it exists because **un-staging is the ledger's
    #: existing compensating undo** (ADR 0004) and an undo needs the row it
    #: compensates for. ADR 0004's consequence list says "one new nullable
    #: column"; it never says how "put it back" finds the movement to reverse,
    #: and the alternative — re-deriving it from `(lot, location, ref_id)` at
    #: undo time — is a heuristic that picks the wrong row as soon as one lot is
    #: staged twice.
    #:
    #: For a partial move it is the `split_in` half; `group_uuid` on that row
    #: ties in the `split_out`, so one reversal unwinds both. Cleared on the
    #: remainder row a partial consumption leaves behind, because that remainder
    #: is no longer the quantity the movement recorded — see
    #: `app.services.reservations.consume_staged`.
    staged_ledger_seq: Mapped[int | None] = mapped_column(
        ForeignKey("stock_ledger.seq", ondelete="RESTRICT")
    )

    #: When the hold was taken. A reservation held for months is hoarding — it
    #: reads as missing stock to every other query — and this is the only column
    #: that would show it.
    reserved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # **The index the reserved-cache rebuild is built around.** Partial on
        # `state = 'reserved'` and covering (`lot_id` then `qty_milli`), so the
        # rebuild's per-lot SUM is an index-only scan and the index holds only
        # live reservations — `CONSUMED` rows accumulate forever and would
        # otherwise make the rebuild get slower with every build ever done.
        Index(
            "ix_stock_allocations_reserved_lot",
            "lot_id",
            "qty_milli",
            sqlite_where=state == AllocationState.RESERVED.value,
        ),
        # A build's own view: its open reservations, its shortages, its picks.
        Index("ix_stock_allocations_build_state", "build_id", "state"),
        # The reverse question — "who is holding this part?" — which is what
        # turns a shortage report into something actionable.
        Index("ix_stock_allocations_part_state", "part_id", "state"),
    )
