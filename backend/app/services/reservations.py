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

Nothing in this module writes `stock_ledger`. `consume`, `stage`, `unstage`,
`consume_staged` and `record_used` all move stock through `app.services.ledger`,
which is the sole writer.

**Staging (ADR 0004) deliberately adds no fourth thing to that list.** Parts set
aside for a project are an ordinary ledger move to an ordinary location, so the
one number that changes is `qty_milli_cached` — the reserved predicate is
untouched, because a staged row's parts are not in the source lot any more and
counting them there would double-count them against their new home. `stage` is
therefore the one write path here that *decrements* the reserved cache without
anything replacing it: the hold has become stock somewhere else.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.enums import (
    AllocationState,
    BuildStatus,
    LedgerSource,
    LotStatus,
    ShortageKind,
)
from app.models.projects import (
    RESERVED_SUM_SQL,
    BomLine,
    BomLineSubstitute,
    Project,
    ProjectBuild,
    StockAllocation,
)
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location
from app.models.types import utcnow
from app.services import ledger, staging

#: The one lot's worth of the bulk rebuild, as the same expression with a bind
#: parameter where the correlated column goes. See `RESERVED_SUM_SQL`.
_RESERVED_FOR_LOT = text(f"SELECT {RESERVED_SUM_SQL.format(lot=':lot_id')}")

#: The states that mean "this build has already laid hands on these parts", and
#: therefore reduce what it still needs — ADR 0004's `accounted = reserved +
#: staged + consumed`. `PLANNED` is deliberately absent: it is demand restated,
#: not supply, and counting it would make an entirely unfilled build report as
#: fully covered.
_SATISFYING_STATES = (
    AllocationState.RESERVED,
    AllocationState.STAGED,
    AllocationState.CONSUMED,
)

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

    Three decisions worth stating:

    * **Non-`ACTIVE` lots do not count.** Quarantined stock is physically
      present and must not be promised to a build, and a retired lot is not
      there at all. `reserve` refuses those lots for the same reason, so the
      shortage report and the write path agree about what exists.
    * **Each lot is clamped at zero before summing.** One bin whose balance went
      negative through a bad recount must not silently eat another bin's real
      stock: that would turn one visibly wrong number into a fabricated
      shortage somewhere else. The per-lot anomaly stays visible through
      `available`.
    * **Stock inside a project's staging boxes does not count** (ADR 0004).
      Those parts are still stock and still findable, but they are spoken for,
      and here they would be counted a second time: a staged allocation already
      reduces its line's requirement, so crediting the same units as free stock
      too makes a BOM read buildable off parts that are sitting in another
      project's box. `reserve` refuses such a lot for the same reason.

      Filtered by **position in the tree**, not by `is_staging` — INBOX carries
      that same flag and its stock is ordinary free stock, so excluding the flag
      would hide real inventory from every build. `id_path LIKE :prefix || '%'`
      is the left-anchored pattern the `id_path` index serves, and the whole
      join is skipped when nothing has ever been staged.
    """
    if not part_ids:
        return {}
    free_per_lot = func.max(StockLot.qty_milli_cached - StockLot.qty_reserved_milli_cached, 0)
    query = (
        select(StockLot.part_id, func.coalesce(func.sum(free_per_lot), 0))
        .where(StockLot.part_id.in_(part_ids), StockLot.status == LotStatus.ACTIVE)
        .group_by(StockLot.part_id)
    )
    prefix = staging.staging_subtree_prefix(session)
    if prefix is not None:
        query = query.join(Location, Location.id == StockLot.location_id).where(
            Location.id_path.not_like(f"{prefix}%")
        )
    rows = session.execute(query).all()
    return {int(part_id): int(total) for part_id, total in rows}


def _lot_is_in_staging(prefix: str | None, id_path: str | None) -> bool:
    """The prefix test, as a pure function of two already-loaded strings.

    Split out from :func:`is_project_staged` so a loop over many lots asks the
    question without a `session.get` per row, and so the two callers cannot
    drift into testing it differently — which is the failure shape this module's
    own docstring warns about for `RESERVED_SUM_SQL`.
    """
    return prefix is not None and id_path is not None and id_path.startswith(prefix)


def is_project_staged(session: Session, lot: StockLot) -> bool:
    """Whether this lot sits in a project's staging box.

    One lookup of the staging root plus one of the lot's location, so it is
    cheap enough for a write path to ask before promising the stock to someone
    else — and it consults the same prefix `available_by_part` filters on, so
    the report and the refusal cannot disagree about what is spoken for.
    """
    prefix = staging.staging_subtree_prefix(session)
    location = None if prefix is None else session.get(Location, lot.location_id)
    return _lot_is_in_staging(prefix, None if location is None else location.id_path)


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
    * the lot must not be sitting in a project's staging box. Those parts are
      already spoken for and `available_by_part` does not count them, so
      accepting the hold would let the shortage report and this write path
      disagree about what exists — the same reason a non-`ACTIVE` lot is
      refused.
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
    if is_project_staged(session, lot):
        raise ReservationError(
            f"lot {lot.id} is staged for a project; those parts are already spoken for",
            reason="lot_in_project_staging",
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


def staged_claim_is_contradicted(session: Session, allocation: StockAllocation) -> bool:
    """Whether a `STAGED` row's parts are provably no longer where it says.

    True when the lot it names has left the staging subtree, or holds nothing at
    all. Either way the row is a bookkeeping claim with no physical fact left to
    contradict, which is what makes releasing it honest — and it is the same
    condition `_holdings_by_line` reports as `undeliverable_milli`, so exactly the
    rows the shortage report flags are the rows `release` will clear.

    **This exists because those rows were otherwise unclearable.** Review walked
    the loop: `release` refused a staged row and said "un-stage it"; `unstage`
    refused a remainder with no `staged_ledger_seq` and said "move the stock back
    from its staging location instead"; doing that left the row still `STAGED`,
    still refused by both, and `release_build` never selected it, so closing or
    abandoning the build left it behind forever — permanently double-counting its
    quantity against every other line that wanted the same part.

    A partially-emptied box is deliberately *not* contradicted: some of those
    parts really are still there, and dropping the whole claim would lose the
    part that is true. Reconciling the box (a recount, or `consume-staged` for
    what really went in) is the honest move, and the report says so by reporting
    the shortfall rather than by hiding the row.
    """
    if AllocationState(allocation.state) is not AllocationState.STAGED:
        return False
    lot = None if allocation.lot_id is None else session.get(StockLot, allocation.lot_id)
    if lot is None:
        # A staged row naming no lot cannot be describing parts in a box.
        return True
    return lot.qty_milli_cached == 0 or not is_project_staged(session, lot)


def release(session: Session, allocation: StockAllocation, *, note: str | None = None) -> None:
    """Give a hold back, keeping the row.

    `RELEASED` rather than deleted: "we planned this and dropped it" is a
    different statement from "this never happened", an undo needs something to
    point at, and the rebuild does not care either way because its predicate
    excludes the state.

    Three refusals, all about not letting a bookkeeping change contradict a
    physical fact:

    * a `CONSUMED` allocation cannot be released. The parts left the bin; the
      correction for that is a compensating `stock_ledger` row, not a
      bookkeeping change here.
    * a `STAGED` one **whose parts are still in its box** cannot either, and for
      the same reason — dropping the claim without moving them would leave real
      stock in a staging location nothing accounts for. `unstage` is the
      operation that does both. One whose parts have provably left is a different
      matter and *is* released: see `staged_claim_is_contradicted` for the loop of
      mutual refusals that used to strand exactly those rows forever.
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
    if state is AllocationState.STAGED and not staged_claim_is_contradicted(session, allocation):
        raise ReservationError(
            f"allocation {allocation.id} is staged at a project location;"
            " un-stage it so the parts go back to the shelf",
            reason="is_staged",
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

    **A `STAGED` row whose parts really are in a project box is untouched**, which
    is not an oversight: a hold is a promise and closing the build ends it, but
    that row records parts physically on a shelf. Releasing it would silently
    claim they came back. Abandoning a build with parts still staged leaves them
    visible in that box — which is the point — until someone un-stages them.

    **A staged row whose parts have provably left is released**, and that is not a
    softening of the rule above but the reason it can be stated at all. Review
    found such rows unclearable by any operation and therefore left behind by a
    closed build forever, silently double-counting against every other line that
    wanted the part. See `staged_claim_is_contradicted`.
    """
    open_rows = (
        session.execute(
            select(StockAllocation)
            .where(
                StockAllocation.build_id == build.id,
                StockAllocation.state.in_(
                    (AllocationState.PLANNED, AllocationState.RESERVED, AllocationState.STAGED)
                ),
            )
            .order_by(StockAllocation.id)
        )
        .scalars()
        .all()
    )
    # Filtered rather than caught: `release`'s refusal is the contract, so asking
    # the same question here keeps "how many were released" honest instead of
    # counting rows a swallowed exception left alone.
    releasable = [
        allocation
        for allocation in open_rows
        if AllocationState(allocation.state) is not AllocationState.STAGED
        or staged_claim_is_contradicted(session, allocation)
    ]
    for allocation in releasable:
        release(session, allocation, note=note)
    return len(releasable)


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


# ---------------------------------------------------------------------------
# Staging: parts set aside for a project (ADR 0004)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagingMove:
    """What one withdrawal to a project did, in enough detail to render it.

    Both lots are returned because they are both interesting and the caller
    cannot infer one from the other: `source_lot` is the drawer whose count just
    dropped, `staging_lot` is the project box that now holds the parts. For a
    whole-lot move they are the **same object** — the lot itself relocated,
    keeping its identity and its per-lot cost, exactly as
    `ledger.move_whole_lot` describes.
    """

    allocation: StockAllocation
    source_lot: StockLot
    staging_lot: StockLot
    location: Location
    rows: tuple[StockLedger, ...]
    #: Set only for a partial withdrawal, whose two ledger rows are one undoable
    #: unit. A whole-lot move is a single row and needs no group.
    group_uuid: str | None


def stage(
    session: Session,
    build: ProjectBuild,
    lot: StockLot,
    qty_milli: int,
    *,
    attribution: ledger.Attribution,
    allocation: StockAllocation | None = None,
    bom_line: BomLine | None = None,
    assembly_no: int | None = None,
    note: str | None = None,
) -> StagingMove:
    """Withdraw parts to a project (or one of its assemblies). **A real move.**

    This is ADR 0004's central decision and the reason there is no "floating"
    flag: the parts have physically left the drawer, so anything that leaves
    them counted there makes `stock_lots.qty_milli_cached` — the number every
    screen reads — a lie until someone notices. So the withdrawal is an ordinary
    `app.services.ledger` movement to an ordinary location, and the drawer's
    count drops in the same instant. Undo, "where are my project's parts", and
    the six-month-old project box being *visible* rather than implicitly missing
    all fall out of that for free.

    Two shapes, picked by what is actually happening rather than by a flag:

    * **the whole lot, with nothing else held against it** — `move_whole_lot`.
      One row, `delta_milli = 0`, `location_id` rewritten. Minting a new lot
      here would destroy lot identity and per-lot cost continuity for the most
      common case there is: putting the whole bag in the project box.
    * **part of it** — `find_or_create_lot` at the destination plus
      `split_to_lot`: two rows summing to zero, so the move is provably
      conservative. The destination lot carries the batch, date code and unit
      cost across (a project's cost has to be answerable) but **not** the
      packaging: a handful of parts in a project tray is not a reel, and saying
      otherwise would have the project box claiming to hold a 5000-piece reel.

    Staging **from** an existing `RESERVED` hold consumes that hold: the
    reserved cache drops by the whole held quantity in the same transaction the
    parts move, because those parts are not in the source lot any anymore and
    counting them there would double-count them against their new location. A
    partial withdrawal re-holds the remainder as a fresh `RESERVED` row, exactly
    as `consume` does, for the same reason: the row that changes state must
    state what actually moved.

    Staging with no prior hold is equally legitimate — "take these out and put
    them in the project box" is one gesture at a bench, not two — and writes a
    `STAGED` row directly with no reserved-cache change at all.

    **Not refused for insufficient stock.** Unlike `reserve`, which refuses
    because a promise about the future costs nothing to decline, this records
    something that already physically happened; refusing would delete the truth
    to protect a number. The refusals below are only for requests that cannot be
    *interpreted*.
    """
    if qty_milli <= 0:
        raise ReservationError("qty_milli must be greater than zero", reason="non_positive_qty")
    if BuildStatus(build.status) in (BuildStatus.COMPLETED, BuildStatus.ABANDONED):
        # Not "the parts did not move" — they did — but a closed build is not
        # somewhere to move them *to*: nothing will ever consume or release the
        # row. Recording what a finished build really used is the roster's job
        # (ADR 0004), which writes a `CONSUMED` row against the same ledger.
        raise ReservationError(
            f"build {build.id} is {build.status}; reopen it or plan another build",
            reason="build_closed",
        )
    if LotStatus(lot.status) is not LotStatus.ACTIVE:
        # Quarantine exists so a lot cannot be used or relocated without someone
        # deciding about it, and a withdrawal to a project is both.
        raise ReservationError(
            f"lot {lot.id} is {lot.status}; release it before staging it to a project",
            reason="lot_not_active",
        )
    if bom_line is not None and bom_line.project_id != build.project_id:
        raise ReservationError(
            f"bom line {bom_line.id} belongs to project {bom_line.project_id},"
            f" not {build.project_id}",
            reason="line_not_in_build",
        )

    remainder = 0
    other_holds = lot.qty_reserved_milli_cached
    if allocation is not None:
        if allocation.build_id != build.id:
            raise ReservationError(
                f"allocation {allocation.id} belongs to build {allocation.build_id}",
                reason="allocation_not_in_build",
            )
        if AllocationState(allocation.state) is not AllocationState.RESERVED:
            raise ReservationError(
                f"allocation {allocation.id} is {allocation.state};"
                " only a reserved hold is staged from",
                reason="not_reserved",
            )
        if allocation.lot_id != lot.id:
            raise ReservationError(
                f"allocation {allocation.id} holds lot {allocation.lot_id}, not {lot.id}",
                reason="allocation_lot_mismatch",
            )
        if qty_milli > allocation.qty_milli:
            raise ReservationError(
                f"allocation {allocation.id} holds {allocation.qty_milli},"
                f" cannot stage {qty_milli}",
                reason="exceeds_hold",
            )
        remainder = allocation.qty_milli - qty_milli
        other_holds -= allocation.qty_milli

    project = session.get(Project, build.project_id)
    if project is None:  # pragma: no cover - build.project_id is a NOT NULL FK
        raise ReservationError(
            f"build {build.id} references missing project {build.project_id}",
            reason="unknown_project",
        )
    destination = staging.destination_for(session, build, project, assembly_no)
    if lot.location_id == destination.id:
        # A double-tapped withdrawal, almost always. Writing a no-op move would
        # put a movement in the history that never happened — the same refusal
        # `ledger.move_whole_lot` makes, made here so the reserved cache is not
        # decremented first.
        raise ReservationError(
            f"lot {lot.id} is already at staging location {destination.id}",
            reason="same_location",
        )

    if allocation is not None:
        lot.qty_reserved_milli_cached -= allocation.qty_milli

    group: str | None = None
    rows: tuple[StockLedger, ...]
    if qty_milli == lot.qty_milli_cached and other_holds == 0 and remainder == 0:
        row = ledger.move_whole_lot(
            session,
            lot,
            destination.id,
            attribution=_stage_attribution(attribution, build),
        )
        staging_lot = lot
        rows = (row,)
        staged_seq = row.seq
    else:
        staging_lot, _ = ledger.find_or_create_lot(
            session,
            part_id=lot.part_id,
            location=destination,
            batch_code=lot.batch_code,
            serial=lot.serial,
            date_code=lot.date_code,
            unit_cost_micro=lot.unit_cost_micro,
            currency=lot.currency,
        )
        group = attribution.group_uuid or ledger.new_group_uuid()
        out_row, in_row = ledger.split_to_lot(
            session,
            lot,
            staging_lot,
            qty_milli,
            attribution=replace(_stage_attribution(attribution, build), group_uuid=group),
        )
        rows = (out_row, in_row)
        # The `split_in` half, so a reversal starts from the row that put the
        # parts *in* the box; `group_uuid` pulls the `split_out` in with it.
        staged_seq = in_row.seq

    if allocation is None:
        allocation = StockAllocation(
            build_id=build.id,
            bom_line_id=None if bom_line is None else bom_line.id,
            part_id=lot.part_id,
            qty_milli=qty_milli,
            note=note,
        )
        session.add(allocation)
    elif note is not None:
        allocation.note = note

    allocation.state = AllocationState.STAGED
    # **The lot the row names is the project box now**, not the drawer. That is
    # what makes `consume_staged` a plain consume of what is physically there,
    # and what keeps "exactly one state holds stock in the lot it names" true.
    allocation.lot_id = staging_lot.id
    allocation.qty_milli = qty_milli
    allocation.staged_ledger_seq = staged_seq

    if remainder:
        session.add(
            StockAllocation(
                build_id=allocation.build_id,
                bom_line_id=allocation.bom_line_id,
                part_id=allocation.part_id,
                lot_id=lot.id,
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
    return StagingMove(
        allocation=allocation,
        source_lot=lot,
        staging_lot=staging_lot,
        location=destination,
        rows=rows,
        group_uuid=group,
    )


@dataclass(frozen=True)
class UnstageMove:
    """What putting a withdrawal back did.

    `restored_lot` is derived from the compensating rows rather than from the
    allocation, and it has to be: a staged allocation names the lot in the
    project box, while the parts go back into the lot they were split out of.
    Making the caller work that out from `delta_milli` signs would put ledger
    semantics in a route.
    """

    allocation: StockAllocation
    #: The lot the parts went back into — the original bin's lot for a split, and
    #: the relocated lot itself for a whole-lot move.
    restored_lot: StockLot
    staging_lot: StockLot
    #: The compensating rows, newest movement first, as `ledger.reverse` wrote them.
    compensations: tuple[StockLedger, ...]


def unstage(
    session: Session,
    allocation: StockAllocation,
    *,
    attribution: ledger.Attribution,
    note: str | None = None,
) -> UnstageMove:
    """Put a staged withdrawal back on the shelf. **The existing undo.**

    No new mechanism: `ledger.reverse` appends compensating rows for the exact
    movement `staged_ledger_seq` names, which is why ADR 0004 can say "put it
    back is not a new feature". The history then reads "this happened, then it
    was undone", which is a different statement from "this never happened" — and
    only one of the two is true.

    The allocation becomes `RELEASED` rather than being deleted, for the same
    reason a released hold is: "we set these aside and changed our mind" is worth
    keeping, and an undo needs something to point at.

    Two refusals, both because a compensating row can only be honest about the
    movement it compensates:

    * **no `staged_ledger_seq`** — the row is a remainder left by a partial
      consumption, so no single movement corresponds to what it now holds.
      Reversing the original would put back more parts than are in the box. The
      answer is an ordinary move from the staging location back to the bin.
    * **the box no longer holds it all** — some of these parts are already in a
      board. Reversing would drive the staging lot negative and claim parts came
      back that are soldered in.
    """
    if AllocationState(allocation.state) is not AllocationState.STAGED:
        raise ReservationError(
            f"allocation {allocation.id} is {allocation.state}; only a staged row is un-staged",
            reason="not_staged",
        )
    if allocation.staged_ledger_seq is None:
        raise ReservationError(
            f"allocation {allocation.id} has no staging movement to compensate;"
            " move the stock back from its staging location instead",
            reason="no_staging_movement",
        )

    staging_lot = _require_lot(session, allocation)
    if staging_lot.qty_milli_cached < allocation.qty_milli:
        raise ReservationError(
            f"staging lot {staging_lot.id} holds {staging_lot.qty_milli_cached},"
            f" less than the {allocation.qty_milli} staged; some is already built in",
            reason="partly_consumed",
        )

    row = session.get(StockLedger, allocation.staged_ledger_seq)
    if row is None:  # pragma: no cover - the FK is RESTRICT and the ledger cannot be deleted
        raise ReservationError(
            f"allocation {allocation.id} names missing ledger row {allocation.staged_ledger_seq}",
            reason="no_staging_movement",
        )
    # A partial withdrawal is two rows sharing a group, and both halves have to
    # unwind together or stock is created out of nothing.
    rows = ledger.rows_of_group(session, row.group_uuid) if row.group_uuid is not None else [row]
    compensations = ledger.reverse(session, rows, attribution=attribution)

    allocation.state = AllocationState.RELEASED
    if note is not None:
        allocation.note = note
    session.flush()

    # The row that put stock *back* is the one with a positive delta — the
    # reversed `split_out`. A whole-lot move has no delta at all, and its single
    # compensation carries the lot that was relocated, which is the same lot.
    restored_id = next(
        (row.lot_id for row in compensations if row.delta_milli > 0),
        staging_lot.id,
    )
    restored = session.get(StockLot, restored_id) if restored_id is not None else None
    if restored is None:  # pragma: no cover - a compensation always names its lot
        raise ReservationError(
            f"allocation {allocation.id} was un-staged into no lot", reason="no_lot"
        )
    return UnstageMove(
        allocation=allocation,
        restored_lot=restored,
        staging_lot=staging_lot,
        compensations=tuple(compensations),
    )


def consume_staged(
    session: Session,
    allocation: StockAllocation,
    *,
    attribution: ledger.Attribution,
    qty_milli: int | None = None,
) -> tuple[StockAllocation, StockLedger]:
    """Build staged parts into the assembly: `staged -> consumed`.

    An ordinary `ledger.consume` of the *staging* lot, because that is where the
    parts physically are — the drawer's count dropped when they were staged, and
    decrementing it again here would remove the same units twice.

    **Nothing touches the reserved cache**, and that is the payoff of `STAGED`
    not being part of its predicate: the transition into and out of `staged`
    moves stock, not holds, so there is no counter to keep in step.

    A partial build leaves the remainder `STAGED` on the same lot with
    `staged_ledger_seq` **cleared** — see `unstage` for why a remainder has no
    movement it could honestly compensate for.
    """
    if AllocationState(allocation.state) is not AllocationState.STAGED:
        raise ReservationError(
            f"allocation {allocation.id} is {allocation.state};"
            " only staged parts are built into an assembly",
            reason="not_staged",
        )
    built = allocation.qty_milli if qty_milli is None else qty_milli
    if built <= 0:
        raise ReservationError("qty_milli must be greater than zero", reason="non_positive_qty")
    if built > allocation.qty_milli:
        raise ReservationError(
            f"allocation {allocation.id} staged {allocation.qty_milli}, cannot consume {built}",
            reason="exceeds_hold",
        )

    lot = _require_lot(session, allocation)
    remainder = allocation.qty_milli - built
    row = ledger.consume(
        session, lot, built, attribution=_build_attribution(attribution, allocation)
    )

    allocation.qty_milli = built
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
                state=AllocationState.STAGED,
                staged_ledger_seq=None,
                note=allocation.note,
            )
        )

    session.flush()
    return allocation, row


# ---------------------------------------------------------------------------
# The as-built roster: recording what was really used (ADR 0004)
# ---------------------------------------------------------------------------


def record_used(
    session: Session,
    build: ProjectBuild,
    lot: StockLot,
    qty_milli: int,
    *,
    attribution: ledger.Attribution,
    bom_line: BomLine | None = None,
    part_id: int | None = None,
    note: str | None = None,
) -> tuple[StockAllocation, StockLedger]:
    """Record a part that was **actually used but never tracked**.

    One `stock_ledger` consume plus one `CONSUMED` allocation, with no hold in
    between — the roster correction ADR 0004 requires, because the requirement is
    explicitly that reality will not always have been tracked. Refusing to record
    an untracked part would guarantee the roster is wrong, which is worse than a
    roster that admits it was edited.

    Three things make this deliberately *more* permissive than every other write
    here, and each one is the difference between a roster and a wish:

    * **A closed build is fine.** `reserve` and `stage` refuse one because a hold
      or a withdrawal against a finished build is a promise nothing will keep —
      but "here is what build 2 really used" is a statement about the past, and a
      build being finished is precisely when someone sits down and reconciles it.
    * **A non-`ACTIVE` lot is fine**, and so is a lot in a project's staging box.
      Both are refused by `reserve` because promising stock nobody may touch is a
      lie about the future; recording that those parts were consumed months ago
      is not.
    * **Insufficient stock is fine**, exactly as it is for `ledger.consume`. The
      balance going negative is the alarm this design wants raised, not a reason
      to reject the record of what physically happened. Reconciling a bin that
      was over-drawn is what a recount is for.

    What is refused is only what cannot be *interpreted*: a non-positive
    quantity, a part that is not the lot's, and a BOM line belonging to another
    project. `bom_line=None` is legitimate and expected — the two extra resistors
    nobody planned for are the signal that the BOM is out of date, which on an
    iterating prototype is the normal case rather than an error.

    **`source` is forced to `LedgerSource.RECONCILED`**, never taken from the
    caller. See that member: the roster is only worth reading because an
    after-the-fact entry is visibly an after-the-fact entry, and a client able to
    label one `scan` would quietly destroy that.
    """
    if qty_milli <= 0:
        raise ReservationError("qty_milli must be greater than zero", reason="non_positive_qty")
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

    allocation = StockAllocation(
        build_id=build.id,
        bom_line_id=None if bom_line is None else bom_line.id,
        part_id=lot.part_id,
        lot_id=lot.id,
        qty_milli=qty_milli,
        state=AllocationState.CONSUMED,
        # No `reserved_at`: this hold never existed. Leaving it NULL is what makes
        # "was this ever reserved?" answerable from the row itself, rather than
        # only from the ledger row's source.
        consumed_at=utcnow(),
        note=note,
    )
    row = ledger.consume(
        session,
        lot,
        qty_milli,
        attribution=replace(
            _build_attribution(attribution, allocation),
            source=LedgerSource.RECONCILED,
        ),
    )
    allocation.consumed_ledger_seq = row.seq
    session.add(allocation)
    session.flush()
    return allocation, row


def _stage_attribution(attribution: ledger.Attribution, build: ProjectBuild) -> ledger.Attribution:
    """Point a staging movement back at the build that caused it.

    Same rule as `_build_attribution` — only fills what the caller left empty —
    but keyed on the build rather than on an allocation, because a withdrawal
    with no prior hold has no allocation to ask until after the rows are written.
    """
    if attribution.ref_type is not None or attribution.ref_id is not None:
        return attribution
    return replace(attribution, ref_type=BUILD_REF_TYPE, ref_id=build.id)


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

    More than one number because none of them substitutes for another: the
    recorded hold is what a user has to release, the deliverable part is the
    only thing that may reduce a requirement, and the per-state split is what
    ADR 0004 requires the UI to keep distinguishable — merging "held in a
    drawer", "in the project box" and "soldered in" lets a BOM look buildable
    when the parts are already spent.
    """

    held_milli: int
    undeliverable_milli: int
    reserved_milli: int = 0
    staged_milli: int = 0
    consumed_milli: int = 0


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
    #: The BOM line's own per-assembly quantity, reported rather than left to be
    #: divided back out of `required_milli`. A UI has to be able to say "3 each for
    #: 5 boards = 15", and the division is not available to it: a DNP line
    #: reports `required_milli == 0` on purpose, so `required / assembly_count`
    #: is 0 there and the per-assembly figure would silently disappear from the
    #: one kind of line whose quantity is only meaningful per assembly.
    qty_per_assembly_milli: int
    #: What this build already holds, has set aside or has built in:
    #: `RESERVED + STAGED + CONSUMED`, exactly as `stock_allocations` records it
    #: — ADR 0004's `accounted`. Reported raw; a hold this line's lot can no
    #: longer fill is still a hold somebody has to act on. Broken out below
    #: because the three are different problems: a reservation can be released,
    #: staged parts are in a box on a shelf, and a consumed one is gone.
    allocated_milli: int
    #: `allocated_milli`, split. Kept as separate numbers rather than merged
    #: because ADR 0004 is explicit that merging them lets a BOM look buildable
    #: when it is not — "held in the drawer" and "already soldered in" cannot be
    #: acted on the same way, and the shortage line is where a user decides.
    reserved_milli: int
    staged_milli: int
    consumed_milli: int
    #: `max(0, required - (allocated - undeliverable))` — the demand no
    #: allocation covers yet, *before* free stock is netted against it. This is
    #: ADR 0004's `needed`, and it is what makes raising `assembly_count` show up
    #: with nothing written: it is computed from the count on every read.
    needed_milli: int
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

    **Staged parts are credited to their line and excluded from the free pool**
    (ADR 0004), which is one fact stated twice on purpose: the requirement drops
    because those parts are set aside for it, and the pool drops because they are
    no longer free for anything else. Doing only the first would report a line as
    satisfied *and* leave its parts available to the next line; doing only the
    second would report the line short of parts already sitting in its own box.

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
    substitutes = substitutes_by_line(session, line_ids)
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
            qty_per_assembly_milli=line.qty_per_assembly_milli,
            allocated_milli=held,
            reserved_milli=holding.reserved_milli,
            staged_milli=holding.staged_milli,
            consumed_milli=holding.consumed_milli,
            needed_milli=0,
            undeliverable_milli=holding.undeliverable_milli,
            available_milli=None,
            shortfall_milli=0,
        )

    needed = max(0, required - (held - holding.undeliverable_milli))

    if line.part_id is None:
        # A substitute list without a matched part is not a fallback: nobody has
        # said the alternate is equivalent *to what*, so there is nothing to
        # satisfy. It stays unidentified until a human matches the line.
        #
        # `needed_milli` is still reported: the board needs three of *something*
        # and nothing has been set aside for it, which is a real number even
        # though `available_milli` and `shortfall_milli` are not computable.
        return LineShortage(
            bom_line_id=line.id,
            line_no=line.line_no,
            part_id=None,
            kind=ShortageKind.UNIDENTIFIED,
            required_milli=required,
            qty_per_assembly_milli=line.qty_per_assembly_milli,
            allocated_milli=held,
            reserved_milli=holding.reserved_milli,
            staged_milli=holding.staged_milli,
            consumed_milli=holding.consumed_milli,
            needed_milli=needed,
            undeliverable_milli=holding.undeliverable_milli,
            available_milli=None,
            shortfall_milli=None,
        )

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
        qty_per_assembly_milli=line.qty_per_assembly_milli,
        allocated_milli=held,
        reserved_milli=holding.reserved_milli,
        staged_milli=holding.staged_milli,
        consumed_milli=holding.consumed_milli,
        needed_milli=needed,
        undeliverable_milli=holding.undeliverable_milli,
        available_milli=free,
        shortfall_milli=shortfall,
        substitute_part_ids=tuple(contributors),
    )


def substitutes_by_line(session: Session, line_ids: Iterable[int]) -> dict[int, tuple[int, ...]]:
    """Accepted alternates per line, preference then id — one query, not N.

    `id` breaks preference ties so the order the user typed them in survives; an
    unordered candidate list is one they have to re-reason about every time.

    Public because `app.services.picking` draws stock in exactly this order: the
    pick list offering an alternate the shortage report did not count on would be
    two answers to "what satisfies this line", and the walk is the one the user
    physically acts on.
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

    **`STAGED` rows go through the same headroom fill, and the first version of
    this that credited them whole — reasoning that they were like `CONSUMED`,
    with "no headroom question to ask about a lot they are no longer in" — was
    exactly the bug above, reintroduced.** The difference is that `CONSUMED` is a
    statement about the past while `STAGED` is a claim about the *present*: these
    parts are in that box right now. A recount of the box, a partial build or an
    ordinary move can each falsify it, and `unstage` already refuses on precisely
    that test (`partly_consumed`), so a report that says `satisfied` while the
    write path says the box is empty is the report being wrong. Two shapes were
    reproduced through the HTTP API:

    * stage a whole lot, then recount the project box to zero — every lot in the
      database at zero, and the line still read `satisfied` / `is_buildable`;
    * a `STAGED` remainder whose lot is moved back out of the staging subtree, as
      `unstage`'s own `no_staging_movement` refusal instructs. The units were then
      counted twice: once as this line's `staged_milli`, and once as free stock
      for every other line, because `available_by_part` only excludes lots
      *inside* the subtree.

    So a staged row is filled from its lot's headroom like a reserved one, and a
    staged row whose lot has left the staging subtree is worth **zero** — that is
    the one case where `available_by_part` is already counting those units, so
    crediting them here as well is the double count. Inside the subtree the lot is
    excluded from availability entirely, which is why the whole on-hand balance is
    the headroom there and no other build can have claimed part of it.
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
            Location.id_path,
        )
        .join(StockLot, StockLot.id == StockAllocation.lot_id, isouter=True)
        .join(Location, Location.id == StockLot.location_id, isouter=True)
        .where(
            StockAllocation.build_id == build_id,
            StockAllocation.bom_line_id.is_not(None),
            StockAllocation.state.in_(_SATISFYING_STATES),
        )
        # `id` last so the fill order below is deterministic; `lot_id` first only
        # to keep one lot's rows together, which makes the loop readable.
        .order_by(StockAllocation.lot_id, StockAllocation.id)
    ).all()

    #: Read once, not per row: the prefix is a single lookup and every staged row
    #: asks the same question of it. `None` means nothing has ever been staged,
    #: in which case no `STAGED` row can exist to ask.
    prefix = staging.staging_subtree_prefix(session)

    held: dict[int, int] = {}
    #: Per state, so the report can keep the three apart. Summed here rather than
    #: with three more `SUM ... FILTER` columns, because the loop already has to
    #: visit every row for the headroom fill below.
    by_state: dict[tuple[int, AllocationState], int] = {}
    #: `(line, qty, lot_id, state)` for every row whose delivery is conditional —
    #: `RESERVED` and `STAGED`. Filled in list order, which the `ORDER BY` above
    #: makes `(lot_id, id)`: two lines drawing on one over-committed lot get a
    #: stable, oldest-claim-first answer.
    claims: list[tuple[int, int, int | None, AllocationState]] = []
    own_reserved: dict[int, int] = {}
    lot_state: dict[int, tuple[str, int, int, str | None]] = {}
    for line_id, state, qty_milli, lot_id, lot_status, on_hand, reserved, id_path in rows:
        line = int(line_id)
        qty = int(qty_milli)
        held[line] = held.get(line, 0) + qty
        key = (line, AllocationState(state))
        by_state[key] = by_state.get(key, 0) + qty
        if AllocationState(state) is AllocationState.CONSUMED:
            continue
        claims.append((line, qty, None if lot_id is None else int(lot_id), AllocationState(state)))
        if lot_id is not None:
            if AllocationState(state) is AllocationState.RESERVED:
                own_reserved[int(lot_id)] = own_reserved.get(int(lot_id), 0) + qty
            lot_state[int(lot_id)] = (
                str(lot_status),
                int(on_hand),
                int(reserved),
                None if id_path is None else str(id_path),
            )

    headroom: dict[int, int] = {}
    for lot_id, (status, on_hand, reserved, _) in lot_state.items():
        if LotStatus(status) is not LotStatus.ACTIVE:
            # Matches `available_by_part`, which drops the lot entirely: promising
            # quarantined stock to a build is exactly what `reserve` refuses.
            headroom[lot_id] = 0
        else:
            # One pool per lot, shared by its reserved and staged claims, because
            # one balance can only satisfy one of them. Other builds' holds are
            # honoured first; a staging lot has none, so there the pool is simply
            # what the box holds.
            headroom[lot_id] = max(0, on_hand - (reserved - own_reserved.get(lot_id, 0)))

    undeliverable: dict[int, int] = {}
    for line, qty, lot_id, state in claims:
        # A `RESERVED` row with no lot cannot deliver anything — `reserve` never
        # writes one, so this is a corrupt row rather than a state, and crediting
        # it would hide the corruption behind a satisfied line.
        if lot_id is None:
            fillable = 0
        else:
            fillable = min(qty, headroom[lot_id])
            if state is AllocationState.STAGED and not _lot_is_in_staging(
                prefix, lot_state[lot_id][3]
            ):
                # The claim survives as a claim — somebody has to clear it — but
                # the parts are back in the free pool `available_by_part` counts,
                # so crediting them here too is the double count. Headroom is
                # deliberately left alone: a reserved claim on the same lot may
                # still legitimately draw on it.
                fillable = 0
            headroom[lot_id] -= fillable
        if qty > fillable:
            undeliverable[line] = undeliverable.get(line, 0) + (qty - fillable)

    return {
        line: _LineHolding(
            held_milli=total,
            undeliverable_milli=undeliverable.get(line, 0),
            reserved_milli=by_state.get((line, AllocationState.RESERVED), 0),
            staged_milli=by_state.get((line, AllocationState.STAGED), 0),
            consumed_milli=by_state.get((line, AllocationState.CONSUMED), 0),
        )
        for line, total in held.items()
    }
