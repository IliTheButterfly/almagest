"""Availability, reservations, and what a build is short.

Three things live here and they belong together, because each one is only
correct in terms of the other two:

* **`available(lot)` is `qty_milli_cached - qty_reserved_milli_cached`** — two
  caches, subtracted, never a sum over `stock_ledger` or `stock_allocations`.
  Summing either in an API path is how this design dies at 200k rows.
* **The reservation write paths maintain `qty_reserved_milli_cached`** in the
  same transaction as the `stock_allocations` row, exactly as
  `app.services.ledger.post` maintains `qty_milli_cached` beside its ledger
  row. Nothing else may touch that column.
* **Shortage is derived** from the first two and stored nowhere. A stored
  shortage would be a cache of a cache, wrong the moment anything is received.

The cache rebuild and its nightly drift check are **not** here — they live with
every other cache rebuild in `app.db.maintenance`
(`rebuild_reserved_quantities`, `check_reserved_quantity_drift`), which is where
`cache_state` is written and where an operator already looks. What is here is
`reserved_milli`, the per-lot recomputation, and it is built by formatting the
same `RESERVED_SUM_SQL` fragment the bulk rebuild formats. That indirection is
the whole point: this repo has already shipped a bug of exactly the shape where
a bulk rebuild and a single-row read compute the same quantity two different
ways, disagree, and the bulk path is the one that persists.

**Incrementally maintaining a derived counter is only safe because the rebuild
exists.** A crashed pick, a half-applied cancel or a future write path that
forgets leaves a number no user can see is wrong; the rebuild turns that into a
stale value a nightly job repairs instead of lost state. If you add a write path
here, add it to the rebuild's predicate too — or better, make it a state
transition the existing predicate already covers.

Nothing in this module writes `stock_ledger`. `consume` moves stock through
`app.services.ledger`, which is the sole writer.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.enums import AllocationState, BuildStatus, LotStatus, ShortageKind
from app.models.projects import (
    RESERVED_SUM_SQL,
    BomLine,
    BomLineSubstitute,
    ProjectBuild,
    StockAllocation,
)
from app.models.stock import StockLedger, StockLot
from app.models.types import utcnow
from app.services import ledger

#: The one lot's worth of the bulk rebuild, as the same expression with a bind
#: parameter where the correlated column goes. See `RESERVED_SUM_SQL`.
_RESERVED_FOR_LOT = text(f"SELECT {RESERVED_SUM_SQL.format(lot=':lot_id')}")

#: The states that mean "this build has already laid hands on these parts", and
#: therefore reduce what it still needs. `PLANNED` is deliberately absent: it is
#: demand restated, not supply, and counting it would make an entirely unfilled
#: build report as fully covered.
_SATISFYING_STATES = (AllocationState.RESERVED, AllocationState.CONSUMED)

#: `stock_ledger.ref_type` for a movement caused by building something, so
#: "what did this build actually use" is answerable from the ledger alone —
#: which matters because allocations cascade away with their project and ledger
#: rows never do.
BUILD_REF_TYPE = "project_build"


class ReservationError(ValueError):
    """A refused reservation. `reason` is machine-readable for the API layer.

    Unlike `app.services.ledger.LedgerError`, which is almost never raised
    because *a scan is never rejected*, refusals here are routine — see
    `reserve` for why a promise about the future is allowed to be refused when a
    record of the past is not.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Reading availability
# ---------------------------------------------------------------------------


def available(lot: StockLot) -> int:
    """What of this lot nobody has promised: `on hand - reserved`.

    A pure function of two cached columns, so it is free to call in a loop and
    correct only because both caches are rebuildable.

    **Deliberately not clamped at zero.** Negative means over-commitment — an
    explicit `allow_overcommit` reservation, a recount that came up short after
    stock was promised, or drift — and every one of those is something a
    dashboard should show rather than something to round away. Aggregates clamp
    per lot instead; see `available_by_part`.
    """
    return lot.qty_milli_cached - lot.qty_reserved_milli_cached


def available_by_part(session: Session, part_ids: Collection[int]) -> dict[int, int]:
    """Free stock per part, over `ACTIVE` lots only, as one query.

    Two decisions worth stating:

    * **Non-`ACTIVE` lots do not count.** Quarantined stock is physically
      present and must not be promised to a build, and a retired lot is not
      there at all. `reserve` refuses those lots for the same reason, so the
      shortage report and the write path agree about what exists.
    * **Each lot is clamped at zero before summing.** One bin whose balance went
      negative through a bad recount must not silently eat another bin's real
      stock: that would turn one visibly wrong number into a fabricated
      shortage somewhere else. The per-lot anomaly stays visible through
      `available`.
    """
    if not part_ids:
        return {}
    free_per_lot = func.max(StockLot.qty_milli_cached - StockLot.qty_reserved_milli_cached, 0)
    rows = session.execute(
        select(StockLot.part_id, func.coalesce(func.sum(free_per_lot), 0))
        .where(StockLot.part_id.in_(part_ids), StockLot.status == LotStatus.ACTIVE)
        .group_by(StockLot.part_id)
    ).all()
    return {int(part_id): int(total) for part_id, total in rows}


def reserved_milli(session: Session, lot_id: int) -> int:
    """Recompute one lot's reservations from `stock_allocations`.

    **Not** what an API path should call — that reads `qty_reserved_milli_cached`
    off the lot. This is the verification door: it is what a test, a repair
    script or a per-lot consistency check compares the cache against, and it is
    generated from the same SQL fragment as the bulk rebuild so the two cannot
    drift into computing different things.
    """
    return int(session.execute(_RESERVED_FOR_LOT, {"lot_id": lot_id}).scalar_one())


# ---------------------------------------------------------------------------
# Reserving and releasing
# ---------------------------------------------------------------------------


def reserve(
    session: Session,
    build: ProjectBuild,
    lot: StockLot,
    qty_milli: int,
    *,
    bom_line: BomLine | None = None,
    part_id: int | None = None,
    note: str | None = None,
    allow_overcommit: bool = False,
) -> StockAllocation:
    """Hold `qty_milli` of `lot` for `build`. **Refuses to over-commit.**

    This is the one place in the system that says no to something a user asked
    for, so the reasoning matters. CLAUDE.md's rule is that *a scan is never
    rejected* and *capacity is advisory* — but both are about the record of
    something that physically happened. A put-away occurred whether or not the
    drawer had room; refusing it would delete the truth to protect a number, and
    would teach the user to stop scanning.

    A reservation is the opposite kind of statement: a promise about the future,
    with no physical event to lose by refusing. Accepting a silent over-commit
    means two builds are each promised the same 40 resistors, and the failure is
    discovered at the bench with half a board populated — the reservation
    machinery's entire purpose, defeated quietly. Refusing costs the user one
    dialog, and the number they need in order to act ("only 25 free") is in the
    message.

    `allow_overcommit` is therefore an **explicit user decision, never a
    fallback on refusal**: "reserve it anyway, more is on order" is legitimate,
    and it is honest precisely because it is recorded as a deliberate choice.
    It is what makes `available` go negative, which the shortage report then
    clamps per lot rather than hiding.

    The rest of the refusals are invariants the schema cannot express without a
    `CHECK`, which is forbidden here:

    * `part_id`, if given, must match the lot's part. The API receives both from
      a client, and trusting the lot silently would file a pick of one part
      under another.
    * a `bom_line` must belong to this build's project — there is no composite
      FK that can say so.
    * the lot must be `ACTIVE`, and the build must not be closed. Reserving into
      a completed build creates a hold nothing will ever release.
    """
    if qty_milli <= 0:
        raise ReservationError("qty_milli must be greater than zero", reason="non_positive_qty")
    if BuildStatus(build.status) in (BuildStatus.COMPLETED, BuildStatus.ABANDONED):
        raise ReservationError(
            f"build {build.id} is {build.status}; closing a build releases its reservations",
            reason="build_closed",
        )
    if LotStatus(lot.status) is not LotStatus.ACTIVE:
        raise ReservationError(
            f"lot {lot.id} is {lot.status}; only active stock can be promised to a build",
            reason="lot_not_active",
        )
    if part_id is not None and part_id != lot.part_id:
        raise ReservationError(
            f"lot {lot.id} holds part {lot.part_id}, not {part_id}",
            reason="part_lot_mismatch",
        )
    if bom_line is not None and bom_line.project_id != build.project_id:
        raise ReservationError(
            f"bom line {bom_line.id} belongs to project {bom_line.project_id},"
            f" not {build.project_id}",
            reason="line_not_in_build",
        )

    free = available(lot)
    if not allow_overcommit and qty_milli > free:
        raise ReservationError(
            f"lot {lot.id} has {free} milli free, cannot reserve {qty_milli}",
            reason="insufficient_available",
        )

    allocation = StockAllocation(
        build_id=build.id,
        bom_line_id=None if bom_line is None else bom_line.id,
        part_id=lot.part_id,
        lot_id=lot.id,
        qty_milli=qty_milli,
        state=AllocationState.RESERVED,
        reserved_at=utcnow(),
        note=note,
    )
    # The counter and the row it is derived from, in one transaction — the same
    # pairing `ledger.post` keeps, and the reason a rebuild is a repair rather
    # than a routine necessity.
    lot.qty_reserved_milli_cached += qty_milli
    session.add(allocation)
    session.flush()
    return allocation


def release(session: Session, allocation: StockAllocation, *, note: str | None = None) -> None:
    """Give a hold back, keeping the row.

    `RELEASED` rather than deleted: "we planned this and dropped it" is a
    different statement from "this never happened", an undo needs something to
    point at, and the rebuild does not care either way because its predicate
    excludes the state.

    Two refusals, both about not decrementing the cache twice:

    * a `CONSUMED` allocation cannot be released. The parts left the bin; the
      correction for that is a compensating `stock_ledger` row, not a
      bookkeeping change here.
    * an already-`RELEASED` one cannot be released again. A double-tapped button
      that decremented twice is precisely the drift this design exists to make
      impossible.
    """
    state = AllocationState(allocation.state)
    if state is AllocationState.CONSUMED:
        raise ReservationError(
            f"allocation {allocation.id} was consumed; undo the ledger row instead",
            reason="already_consumed",
        )
    if state is AllocationState.RELEASED:
        raise ReservationError(
            f"allocation {allocation.id} is already released", reason="already_released"
        )

    if state is AllocationState.RESERVED:
        lot = _require_lot(session, allocation)
        lot.qty_reserved_milli_cached -= allocation.qty_milli
    # PLANNED holds no lot and no quantity in the cache, so there is nothing to
    # decrement — only the state to record.

    allocation.state = AllocationState.RELEASED
    if note is not None:
        allocation.note = note
    session.flush()


def release_build(session: Session, build: ProjectBuild, *, note: str | None = None) -> int:
    """Release every open allocation of a build. Returns how many.

    This is what closing a build means for stock: a `COMPLETED` or `ABANDONED`
    build holding reservations reads as missing inventory forever, and nothing
    else would ever come back to free them. `CONSUMED` rows are untouched — they
    are the record of what was actually used.
    """
    open_rows = (
        session.execute(
            select(StockAllocation)
            .where(
                StockAllocation.build_id == build.id,
                StockAllocation.state.in_((AllocationState.PLANNED, AllocationState.RESERVED)),
            )
            .order_by(StockAllocation.id)
        )
        .scalars()
        .all()
    )
    for allocation in open_rows:
        release(session, allocation, note=note)
    return len(open_rows)


def consume(
    session: Session,
    allocation: StockAllocation,
    *,
    attribution: ledger.Attribution,
    qty_milli: int | None = None,
) -> tuple[StockAllocation, StockLedger]:
    """Turn a hold into a pick: one `stock_ledger` row, via `ledger.consume`.

    Nothing here writes the ledger itself — `app.services.ledger` is the sole
    writer, so the ledger row and `qty_milli_cached` move together there while
    the reservation side moves here, all in one transaction.

    The two caches step in the same instant on purpose: `qty_reserved` drops by
    what was held and `qty_milli` drops by what was picked, so `available` never
    dips or spikes through an intermediate state that another reader could see.

    A **partial** pick (`qty_milli` below the hold) leaves the `CONSUMED` row
    stating exactly what the ledger row moved, and re-holds the remainder as a
    fresh `RESERVED` row on the same lot. Shrinking nothing and marking the whole
    hold consumed would make "what went into build 2" disagree with the ledger,
    which is the only question this table exists to answer.
    """
    if AllocationState(allocation.state) is not AllocationState.RESERVED:
        raise ReservationError(
            f"allocation {allocation.id} is {allocation.state}; only a reserved hold is picked",
            reason="not_reserved",
        )
    picked = allocation.qty_milli if qty_milli is None else qty_milli
    if picked <= 0:
        raise ReservationError("qty_milli must be greater than zero", reason="non_positive_qty")
    if picked > allocation.qty_milli:
        raise ReservationError(
            f"allocation {allocation.id} holds {allocation.qty_milli}, cannot consume {picked}",
            reason="exceeds_hold",
        )

    lot = _require_lot(session, allocation)
    remainder = allocation.qty_milli - picked

    lot.qty_reserved_milli_cached -= allocation.qty_milli
    row = ledger.consume(
        session, lot, picked, attribution=_build_attribution(attribution, allocation)
    )

    allocation.qty_milli = picked
    allocation.state = AllocationState.CONSUMED
    allocation.consumed_ledger_seq = row.seq
    allocation.consumed_at = utcnow()

    if remainder:
        session.add(
            StockAllocation(
                build_id=allocation.build_id,
                bom_line_id=allocation.bom_line_id,
                part_id=allocation.part_id,
                lot_id=allocation.lot_id,
                qty_milli=remainder,
                state=AllocationState.RESERVED,
                # Carried over, not re-stamped: the hold is the same hold, and a
                # fresh timestamp would reset the age that makes hoarding visible.
                reserved_at=allocation.reserved_at,
                note=allocation.note,
            )
        )
        lot.qty_reserved_milli_cached += remainder

    session.flush()
    return allocation, row


def _require_lot(session: Session, allocation: StockAllocation) -> StockLot:
    """The lot a hold names. Missing is a bug, not a user error.

    `stock_allocations.lot_id` is `RESTRICT`, so a reserved lot cannot be
    deleted out from under the cache. `NULL` means the row is `PLANNED`, which
    every caller here has already excluded.
    """
    lot = None if allocation.lot_id is None else session.get(StockLot, allocation.lot_id)
    if lot is None:
        raise ReservationError(
            f"allocation {allocation.id} in state {allocation.state} has no lot",
            reason="no_lot",
        )
    return lot


def _build_attribution(
    attribution: ledger.Attribution, allocation: StockAllocation
) -> ledger.Attribution:
    """Point the ledger row back at the build, without overwriting a caller.

    Only fills what the caller left empty: an explicit `ref_type`/`ref_id` is a
    deliberate statement about provenance, and silently replacing it would make
    the ledger lie about why the stock moved.
    """
    if attribution.ref_type is not None or attribution.ref_id is not None:
        return attribution
    return replace(attribution, ref_type=BUILD_REF_TYPE, ref_id=allocation.build_id)


# ---------------------------------------------------------------------------
# Shortages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LineHolding:
    """One BOM line's claim on stock, split into what is real and what is not.

    Two numbers rather than one because both are needed and neither substitutes
    for the other: the recorded hold is what a user has to release, and the
    deliverable part is the only thing that may reduce a requirement.
    """

    held_milli: int
    undeliverable_milli: int


#: A line no allocation names. Not a shortage of anything — the line simply holds
#: nothing yet.
_EMPTY_HOLDING = _LineHolding(held_milli=0, undeliverable_milli=0)


@dataclass(frozen=True)
class LineShortage:
    """One BOM line, netted against real free stock.

    `available_milli` and `shortfall_milli` are `None` — not `0` — for an
    `UNIDENTIFIED` line. Zero would be a claim ("you have none of it"), and the
    honest statement is that no quantity is computable until somebody says what
    the part is. A UI that renders `None` as a dash cannot accidentally add it
    into a total; a `0` silently would.
    """

    bom_line_id: int
    line_no: int
    part_id: int | None
    kind: ShortageKind
    #: `qty_per_assembly_milli * assembly_count`. Known even when the part is
    #: not — the board needs three of *something*.
    required_milli: int
    #: What this build already holds or has picked: `RESERVED + CONSUMED`, exactly
    #: as `stock_allocations` records it. Reported raw — a hold this line's lot
    #: can no longer fill is still a hold somebody has to act on.
    allocated_milli: int
    #: The part of `allocated_milli` its lot cannot actually deliver: a hold on a
    #: bin a recount emptied, on stock another build over-committed first, or on
    #: a lot that is no longer `ACTIVE`. Subtracted from `allocated_milli` before
    #: netting, and reported so the arithmetic reconciles — see
    #: `_holdings_by_line` for why crediting the raw hold reports a build as
    #: buildable off stock the bin does not contain.
    undeliverable_milli: int
    #: Free stock this line could draw on when it was considered, its own part
    #: plus accepted alternates, after earlier lines took their share.
    available_milli: int | None
    #: `max(0, required - (allocated - undeliverable) - available)`.
    shortfall_milli: int | None
    #: The accepted alternates that actually contributed to `available_milli`,
    #: in preference order — so "why is this line not short" is answerable.
    substitute_part_ids: tuple[int, ...] = ()

    @property
    def is_blocking(self) -> bool:
        return self.kind in (ShortageKind.SHORT, ShortageKind.UNIDENTIFIED)


@dataclass(frozen=True)
class BuildShortage:
    build_id: int
    assembly_count: int
    lines: tuple[LineShortage, ...]

    @property
    def short_lines(self) -> tuple[LineShortage, ...]:
        return tuple(line for line in self.lines if line.kind is ShortageKind.SHORT)

    @property
    def unidentified_lines(self) -> tuple[LineShortage, ...]:
        return tuple(line for line in self.lines if line.kind is ShortageKind.UNIDENTIFIED)

    @property
    def is_buildable(self) -> bool:
        """True only if nothing is short **and** nothing is unidentified.

        The second half is the load-bearing one. An unmatched line contributes
        no computable shortfall, so a report that only added shortfalls up would
        call a BOM full of unidentified lines buildable — which is the exact
        false green this distinction exists to prevent.
        """
        return not self.short_lines and not self.unidentified_lines


def shortage_for_build(session: Session, build: ProjectBuild) -> BuildShortage:
    """What stands between this build and being built, line by line.

    Reads availability from the two caches (never a sum over the ledger) and
    **nets free stock across the lines of the report, in line order**. Netting
    is not a nicety: two lines calling for the same 0.1 µF, each independently
    told there are 400 free, would both report satisfied off one pile of 400 —
    a BOM that looks buildable and is not. The first line to be considered draws
    its share, and later lines see what is left.

    Availability already excludes other builds' holds, because those are inside
    `qty_reserved_milli_cached`. This build's own holds are excluded there too,
    which is exactly why they are added back and subtracted from the requirement
    — the same quantity is never counted twice. **What is added back is the
    deliverable part of the hold**, not the recorded one: `available_by_part`
    clamps each lot at zero and skips non-`ACTIVE` lots, so past that clamp the
    hold has already been thrown away once and crediting it whole counts stock
    that is not there. See `_holdings_by_line`.

    Substitutes are consulted before a shortage is reported (that is what
    `bom_line_substitutes` is for), in `preference` order, after the line's own
    part.
    """
    lines = list(
        session.execute(
            select(BomLine)
            .where(BomLine.project_id == build.project_id)
            .order_by(BomLine.line_no, BomLine.id)
        ).scalars()
    )
    line_ids = [line.id for line in lines]
    substitutes = _substitutes_by_line(session, line_ids)
    holdings = _holdings_by_line(session, build.id)

    candidate_part_ids = {line.part_id for line in lines if line.part_id is not None}
    for parts in substitutes.values():
        candidate_part_ids.update(parts)
    # The pool is mutated as lines draw from it, which is what makes this a
    # netted report rather than N independent lookups.
    pool = available_by_part(session, candidate_part_ids)

    results: list[LineShortage] = []
    for line in lines:
        results.append(_net_one_line(line, build, pool, substitutes.get(line.id, ()), holdings))
    return BuildShortage(
        build_id=build.id, assembly_count=build.assembly_count, lines=tuple(results)
    )


def _net_one_line(
    line: BomLine,
    build: ProjectBuild,
    pool: dict[int, int],
    substitute_part_ids: Sequence[int],
    holdings: dict[int, _LineHolding],
) -> LineShortage:
    required = line.qty_per_assembly_milli * build.assembly_count
    holding = holdings.get(line.id, _EMPTY_HOLDING)
    held = holding.held_milli

    if line.is_dnp:
        # Not on the board, so it draws nothing from the pool. Reported anyway:
        # a BOM view that silently omitted lines would not be the BOM.
        return LineShortage(
            bom_line_id=line.id,
            line_no=line.line_no,
            part_id=line.part_id,
            kind=ShortageKind.NOT_FITTED,
            required_milli=0,
            allocated_milli=held,
            undeliverable_milli=holding.undeliverable_milli,
            available_milli=None,
            shortfall_milli=0,
        )

    if line.part_id is None:
        # A substitute list without a matched part is not a fallback: nobody has
        # said the alternate is equivalent *to what*, so there is nothing to
        # satisfy. It stays unidentified until a human matches the line.
        return LineShortage(
            bom_line_id=line.id,
            line_no=line.line_no,
            part_id=None,
            kind=ShortageKind.UNIDENTIFIED,
            required_milli=required,
            allocated_milli=held,
            undeliverable_milli=holding.undeliverable_milli,
            available_milli=None,
            shortfall_milli=None,
        )

    needed = max(0, required - (held - holding.undeliverable_milli))
    contributors: list[int] = []
    free = 0
    for part_id in (line.part_id, *substitute_part_ids):
        share = pool.get(part_id, 0)
        if share <= 0:
            continue
        if part_id != line.part_id:
            contributors.append(part_id)
        free += share

    # Draw in the same order the total was accumulated, so an earlier line
    # consumes its own part before eating an alternate that a later line may be
    # the only claimant for.
    outstanding = min(needed, free)
    for part_id in (line.part_id, *substitute_part_ids):
        if outstanding <= 0:
            break
        take = min(outstanding, max(0, pool.get(part_id, 0)))
        if take:
            pool[part_id] = pool[part_id] - take
            outstanding -= take

    shortfall = needed - min(needed, free)
    return LineShortage(
        bom_line_id=line.id,
        line_no=line.line_no,
        part_id=line.part_id,
        kind=ShortageKind.SHORT if shortfall else ShortageKind.SATISFIED,
        required_milli=required,
        allocated_milli=held,
        undeliverable_milli=holding.undeliverable_milli,
        available_milli=free,
        shortfall_milli=shortfall,
        substitute_part_ids=tuple(contributors),
    )


def _substitutes_by_line(session: Session, line_ids: Iterable[int]) -> dict[int, tuple[int, ...]]:
    """Accepted alternates per line, preference then id — one query, not N.

    `id` breaks preference ties so the order the user typed them in survives; an
    unordered candidate list is one they have to re-reason about every time.
    """
    ids = list(line_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(BomLineSubstitute.bom_line_id, BomLineSubstitute.part_id)
        .where(BomLineSubstitute.bom_line_id.in_(ids))
        .order_by(BomLineSubstitute.preference, BomLineSubstitute.id)
    ).all()
    by_line: dict[int, tuple[int, ...]] = {}
    for line_id, part_id in rows:
        by_line[line_id] = (*by_line.get(line_id, ()), part_id)
    return by_line


def _holdings_by_line(session: Session, build_id: int) -> dict[int, _LineHolding]:
    """What each BOM line of one build holds, and how much of it is fiction.

    Rows with `bom_line_id IS NULL` are filtered out rather than grouped: stock a
    build used that no line asked for — two extra resistors because one went
    flying — satisfies no line's requirement, so it must not reduce one.

    **Why this is not a `SUM` per line.** A hold reduces a line's requirement
    only insofar as its lot can still fill it, and `available_by_part` has
    already decided what a lot can fill: zero for a non-`ACTIVE` lot, and
    `max(0, on_hand - reserved)` otherwise. That clamp is what stops one bin's
    negative balance eating another bin's real stock — but it also throws the
    over-committed part of a hold away, so adding the recorded hold back whole
    counts the same milli-units twice and reports a build as buildable off an
    empty bin. Reproduced exactly that way: receive 100, reserve 100, recount to
    0, and the line still said `satisfied`.

    So each of this build's `RESERVED` rows is filled from a **headroom** figure
    per lot: `on_hand - (reserved by everyone - reserved by this build)`, floored
    at zero, and zero outright for a lot that is not `ACTIVE`. Other holders are
    honoured first, deliberately — there is no priority rule between two builds
    that were both promised the same parts, and over-reporting a shortage is the
    survivable direction (it is visible in this report and released with one
    action, while under-reporting is discovered at the bench with half a board
    populated). Rows are filled in `(lot_id, id)` order so two lines drawing on
    one over-committed lot get a stable, oldest-hold-first answer.

    `CONSUMED` rows are credited whole and unconditionally: those parts have left
    the bin, `stock_ledger` says so, and no later recount can un-pick them.
    """
    rows = session.execute(
        select(
            StockAllocation.bom_line_id,
            StockAllocation.state,
            StockAllocation.qty_milli,
            StockAllocation.lot_id,
            StockLot.status,
            StockLot.qty_milli_cached,
            StockLot.qty_reserved_milli_cached,
        )
        .join(StockLot, StockLot.id == StockAllocation.lot_id, isouter=True)
        .where(
            StockAllocation.build_id == build_id,
            StockAllocation.bom_line_id.is_not(None),
            StockAllocation.state.in_(_SATISFYING_STATES),
        )
        # `id` last so the fill order below is deterministic; `lot_id` first only
        # to keep one lot's rows together, which makes the loop readable.
        .order_by(StockAllocation.lot_id, StockAllocation.id)
    ).all()

    held: dict[int, int] = {}
    reserved_rows: list[tuple[int, int, int | None]] = []
    own_reserved: dict[int, int] = {}
    lot_state: dict[int, tuple[str, int, int]] = {}
    for line_id, state, qty_milli, lot_id, lot_status, on_hand, reserved in rows:
        line = int(line_id)
        qty = int(qty_milli)
        held[line] = held.get(line, 0) + qty
        if AllocationState(state) is not AllocationState.RESERVED:
            continue
        reserved_rows.append((line, qty, None if lot_id is None else int(lot_id)))
        if lot_id is not None:
            own_reserved[int(lot_id)] = own_reserved.get(int(lot_id), 0) + qty
            lot_state[int(lot_id)] = (str(lot_status), int(on_hand), int(reserved))

    headroom: dict[int, int] = {}
    for lot_id, own in own_reserved.items():
        status, on_hand, reserved = lot_state[lot_id]
        if LotStatus(status) is not LotStatus.ACTIVE:
            # Matches `available_by_part`, which drops the lot entirely: promising
            # quarantined stock to a build is exactly what `reserve` refuses.
            headroom[lot_id] = 0
        else:
            headroom[lot_id] = max(0, on_hand - (reserved - own))

    undeliverable: dict[int, int] = {}
    for line, qty, lot_id in reserved_rows:
        # A `RESERVED` row with no lot cannot deliver anything — `reserve` never
        # writes one, so this is a corrupt row rather than a state, and crediting
        # it would hide the corruption behind a satisfied line.
        fillable = 0 if lot_id is None else min(qty, headroom[lot_id])
        if lot_id is not None:
            headroom[lot_id] -= fillable
        if qty > fillable:
            undeliverable[line] = undeliverable.get(line, 0) + (qty - fillable)

    return {
        line: _LineHolding(held_milli=total, undeliverable_milli=undeliverable.get(line, 0))
        for line, total in held.items()
    }
