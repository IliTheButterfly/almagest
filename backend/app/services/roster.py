"""The as-built roster: what a build really used, corrections included.

ADR 0004 asks for "a roster of parts used, because in practice they will not
always have been tracked correctly". That sentence contains the whole design
problem: a roster assembled only from rows the system happened to capture is a
roster that is *confidently wrong*, and the fix is not to capture more but to let
a human write the missing rows down and then **say which rows those are**.

So this module reads three things and keeps them apart:

* **planned** — `bom_lines.qty_per_assembly_milli * assembly_count`, derived on
  every read, so raising the assembly count changes the roster with nothing
  written (ADR 0004's "demand is derived");
* **reserved / staged / consumed** — the three `stock_allocations` states that
  assert something about physical parts, never merged, because ADR 0004 is
  explicit that merging them lets a build look accounted-for off parts that are
  still in a drawer;
* **off-BOM consumption** — `stock_allocations.bom_line_id IS NULL`. On an
  iterating prototype this is the normal case, not an error: it is the signal
  that the BOM is out of date, and the reason `bom_line_id` is nullable at all.

**Provenance is read from `stock_ledger.source`, not from a flag here.** An
after-the-fact entry is written by `reservations.record_used` with
`LedgerSource.RECONCILED`, and the roster follows `consumed_ledger_seq` /
`staged_ledger_seq` to that row to find out. A second copy of the fact on the
allocation would be one more thing to keep in step with the ledger, and the
ledger is the record that cannot be edited — so it is the one worth asking.

This is a read path over one build's allocations, which is bounded by its BOM.
The join to `stock_ledger` is by primary key, one row per allocation — **not** a
`SUM(delta_milli)`, which is the query this design must never put in an API path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import AllocationState, LedgerSource
from app.models.projects import BomLine, ProjectBuild, StockAllocation
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location

#: The states that say something about parts that exist somewhere. `PLANNED`
#: (demand restated) and `RELEASED` (a claim given back) are still *listed* as
#: entries — hiding a withdrawal that was put back would make the history lie by
#: omission — but they contribute to no total.
_HOLDING_STATES = (
    AllocationState.RESERVED,
    AllocationState.STAGED,
    AllocationState.CONSUMED,
)


@dataclass(frozen=True)
class RosterEntry:
    """One `stock_allocations` row, with the evidence behind it resolved.

    `is_after_the_fact` is derived here rather than left to the client so that
    the write path and every reader agree on one definition: it is true exactly
    when the ledger row this points at was written by
    `reservations.record_used`. A UI computing it from `ledger_source` itself
    would silently start disagreeing the day another retroactive source exists.
    """

    allocation_id: int
    part_id: int
    part_name: str
    part_mpn: str | None
    lot_id: int | None
    qty_milli: int
    state: AllocationState
    #: The movement this row is evidence of: `consumed_ledger_seq` for a pick,
    #: `staged_ledger_seq` for a withdrawal to the project box. NULL for a
    #: `PLANNED` row (nothing moved) and for the remainder row a partial
    #: consumption leaves behind (no single movement corresponds to it).
    ledger_seq: int | None
    ledger_source: LedgerSource | None
    is_after_the_fact: bool
    #: Where the lot is **now**, not where it was when the row was written — the
    #: ledger keeps that. For a staged row this is the project's box, which is
    #: the whole point of staging being a real move.
    location_id: int | None
    location_label_path: str | None
    reserved_at: datetime | None
    consumed_at: datetime | None
    note: str | None


@dataclass(frozen=True)
class RosterLine:
    """One BOM line's account, or one part consumed with no line at all.

    The four quantities are never collapsed into a single "done" number. A line
    with 30 required and 30 consumed is finished; one with 30 required and 30
    reserved has not had a single part fitted, and the parts are still in a
    drawer somebody else's build can be told about. Same numbers, opposite
    situations.
    """

    #: NULL together with `line_no` for an off-BOM group — parts this build used
    #: that no line asked for, keyed by part.
    bom_line_id: int | None
    line_no: int | None
    designators: str | None
    part_id: int | None
    part_name: str | None
    part_mpn: str | None
    is_dnp: bool
    is_off_bom: bool
    #: `qty_per_assembly_milli * assembly_count`. Zero for a DNP line (in the
    #: file, not on the board) and for an off-BOM group (nobody planned it).
    required_milli: int
    reserved_milli: int
    staged_milli: int
    consumed_milli: int
    #: The part of `consumed_milli` that was reconstructed rather than tracked.
    #: Reported per line and not only per build, because "the roster was edited"
    #: is not useful — "*this* line was edited" is what a reader acts on.
    after_the_fact_milli: int
    entries: tuple[RosterEntry, ...]

    @property
    def accounted_milli(self) -> int:
        """ADR 0004's `accounted`: what this line has laid hands on."""
        return self.reserved_milli + self.staged_milli + self.consumed_milli

    @property
    def needed_milli(self) -> int:
        """ADR 0004's `needed`: `max(0, demand - accounted)`.

        Derived here, and on the wire, rather than left to the client — ADR 0007
        names *per build* and *in use* as the two numbers the UI must show, and a
        screen doing this subtraction itself is a second definition of demand that
        can disagree with `reservations.shortage_for_build`'s. It is a property
        rather than a field for the same reason `required_milli` is multiplied on
        every read: nothing about it is stored, so raising `assembly_count`
        changes it with nothing backfilled.

        Not identical to `LineShortage.needed_milli`, which additionally subtracts
        the part of a hold its lot can no longer deliver. The roster is a record
        of what was really done; whether a hold is still fillable is a question
        about *free stock now*, which is the shortage report's job.
        """
        return max(0, self.required_milli - self.accounted_milli)


@dataclass(frozen=True)
class BuildRoster:
    build_id: int
    assembly_count: int
    lines: tuple[RosterLine, ...]

    @property
    def off_bom_lines(self) -> tuple[RosterLine, ...]:
        return tuple(line for line in self.lines if line.is_off_bom)

    @property
    def after_the_fact_milli(self) -> int:
        return sum(line.after_the_fact_milli for line in self.lines)


def roster_for_build(session: Session, build: ProjectBuild) -> BuildRoster:
    """Every BOM line plus every off-BOM part, with its allocations attached.

    **Every** line, including ones nothing has been allocated against: a roster
    that listed only the lines with activity would read as complete while half
    the board was unaccounted for, which is the same false-green
    `shortage_for_build` refuses to produce for an unidentified line.

    Off-BOM groups come last and are keyed by part, so "two extra 10k resistors
    because one went flying" is one row rather than one row per pick.
    """
    lines = list(
        session.execute(
            select(BomLine)
            .where(BomLine.project_id == build.project_id)
            .order_by(BomLine.line_no, BomLine.id)
        ).scalars()
    )
    by_line: dict[int, list[RosterEntry]] = {}
    off_bom: dict[int, list[RosterEntry]] = {}
    for line_id, entry in _entries(session, build.id):
        if line_id is None:
            off_bom.setdefault(entry.part_id, []).append(entry)
        else:
            by_line.setdefault(line_id, []).append(entry)

    parts = _parts(session, _line_part_ids(lines))

    rendered = [
        _line(line, build.assembly_count, tuple(by_line.get(line.id, ())), parts) for line in lines
    ]
    rendered.extend(
        _off_bom_line(part_id, tuple(rows), parts) for part_id, rows in sorted(off_bom.items())
    )
    return BuildRoster(
        build_id=build.id, assembly_count=build.assembly_count, lines=tuple(rendered)
    )


def _line_part_ids(lines: list[BomLine]) -> set[int]:
    return {line.part_id for line in lines if line.part_id is not None}


def _parts(session: Session, part_ids: set[int]) -> dict[int, Part]:
    """Name and MPN for the parts the BOM lines themselves name, in one query.

    Fetched rather than left to the client because a roster is read to be read:
    a list of `part #42`s with no MPN is a list nobody can check against the
    board in their hand, and N round trips to make it readable is the shape that
    makes a screen feel broken over a phone connection.
    """
    if not part_ids:
        return {}
    rows = session.execute(select(Part).where(Part.id.in_(part_ids))).scalars()
    return {part.id: part for part in rows}


def _entries(session: Session, build_id: int) -> list[tuple[int | None, RosterEntry]]:
    """This build's allocations, each with its ledger row and current location.

    `Part` is joined inner because `stock_allocations.part_id` is NOT NULL with a
    `RESTRICT` delete — the part behind a roster row cannot go away. Everything
    else is outer, and each one is optional for a real reason: a `PLANNED` row
    has no lot, a lot with no location is a corrupt row, and a hold nobody has
    acted on has no ledger row at all. An inner join on any of those would drop
    exactly the rows a reconciliation screen exists to show.
    """
    rows = session.execute(
        select(StockAllocation, Part, StockLot, Location, StockLedger)
        .join(Part, Part.id == StockAllocation.part_id)
        .join(StockLot, StockLot.id == StockAllocation.lot_id, isouter=True)
        .join(Location, Location.id == StockLot.location_id, isouter=True)
        .join(
            StockLedger,
            # One `seq` per row: whichever movement this state is evidence of. A
            # row is never both staged and consumed at once — `consume_staged`
            # overwrites the state — and the consumed seq is the later, more
            # specific fact, so it wins the coalesce.
            StockLedger.seq
            == func.coalesce(
                StockAllocation.consumed_ledger_seq, StockAllocation.staged_ledger_seq
            ),
            isouter=True,
        )
        .where(StockAllocation.build_id == build_id)
        .order_by(StockAllocation.id)
    ).all()

    out: list[tuple[int | None, RosterEntry]] = []
    for allocation, part, lot, location, movement in rows:
        source = None if movement is None else LedgerSource(movement.source)
        out.append(
            (
                allocation.bom_line_id,
                RosterEntry(
                    allocation_id=allocation.id,
                    part_id=allocation.part_id,
                    part_name=part.name,
                    part_mpn=part.mpn,
                    lot_id=allocation.lot_id,
                    qty_milli=allocation.qty_milli,
                    state=AllocationState(allocation.state),
                    ledger_seq=None if movement is None else movement.seq,
                    ledger_source=source,
                    is_after_the_fact=source is LedgerSource.RECONCILED,
                    location_id=None if lot is None else lot.location_id,
                    location_label_path=None if location is None else location.label_path,
                    reserved_at=allocation.reserved_at,
                    consumed_at=allocation.consumed_at,
                    note=allocation.note,
                ),
            )
        )
    return out


def _totals(entries: tuple[RosterEntry, ...]) -> tuple[int, int, int, int]:
    """`(reserved, staged, consumed, after the fact)` over one line's entries."""
    per_state = {
        state: sum(entry.qty_milli for entry in entries if entry.state is state)
        for state in _HOLDING_STATES
    }
    return (
        per_state[AllocationState.RESERVED],
        per_state[AllocationState.STAGED],
        per_state[AllocationState.CONSUMED],
        sum(
            entry.qty_milli
            for entry in entries
            if entry.state is AllocationState.CONSUMED and entry.is_after_the_fact
        ),
    )


def _line(
    line: BomLine,
    assembly_count: int,
    entries: tuple[RosterEntry, ...],
    parts: dict[int, Part],
) -> RosterLine:
    reserved, staged, consumed, corrected = _totals(entries)
    part = None if line.part_id is None else parts.get(line.part_id)
    return RosterLine(
        bom_line_id=line.id,
        line_no=line.line_no,
        designators=line.designators,
        part_id=line.part_id,
        part_name=None if part is None else part.name,
        part_mpn=None if part is None else part.mpn,
        is_dnp=line.is_dnp,
        is_off_bom=False,
        # A DNP line generates no demand, exactly as the shortage report has it —
        # but it is still listed, and it can still legitimately carry consumed
        # rows: fitting a DNP part by hand is a thing that happens, and the
        # roster is where it becomes visible.
        required_milli=0 if line.is_dnp else line.qty_per_assembly_milli * assembly_count,
        reserved_milli=reserved,
        staged_milli=staged,
        consumed_milli=consumed,
        after_the_fact_milli=corrected,
        entries=entries,
    )


def _off_bom_line(
    part_id: int, entries: tuple[RosterEntry, ...], parts: dict[int, Part]
) -> RosterLine:
    reserved, staged, consumed, corrected = _totals(entries)
    part = parts.get(part_id)
    return RosterLine(
        bom_line_id=None,
        line_no=None,
        designators=None,
        part_id=part_id,
        part_name=None if part is None else part.name,
        part_mpn=None if part is None else part.mpn,
        is_dnp=False,
        is_off_bom=True,
        # Nobody planned it, and inventing a requirement here would be the
        # synthetic BOM line `stock_allocations.bom_line_id`'s nullability exists
        # to avoid: the board does not have this component on it.
        required_milli=0,
        reserved_milli=reserved,
        staged_milli=staged,
        consumed_milli=consumed,
        after_the_fact_milli=corrected,
        entries=entries,
    )
