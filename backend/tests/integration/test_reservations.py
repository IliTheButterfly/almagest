"""Reservations, availability and shortage.

The tests fall into three groups, and each one covers a way this design fails
silently rather than loudly:

* **cache agreement.** `qty_reserved_milli_cached` is maintained incrementally by
  the write paths and reconstructed by one statement. If those two ever compute
  a different number, the wrong one is the one that persists — a bug of exactly
  this shape has already shipped in this repo — so a randomised sequence of
  operations pins write path, per-lot read and bulk rebuild against each other
  after *every* step.
* **the two kinds of shortage.** "You need three more" and "nobody has said what
  this is" are different problems, and conflating them makes a BOM report
  buildable for a board nobody can build.
* **over-commitment.** A reservation is a promise, so it is refused rather than
  accepted-and-flagged. That is the one deliberate departure from "a scan is
  never rejected", and it needs a test or the next reader will "fix" it.

Everything runs on a database built by the real migrations, so the FKs,
`RESTRICT`s and ledger triggers are all live.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.maintenance import (
    RESERVATIONS,
    check_reserved_quantity_drift,
    rebuild_reserved_quantities,
)
from app.models.enums import AllocationState, BuildStatus, LedgerKind, LotStatus, ShortageKind
from app.models.projects import BomLineSubstitute, StockAllocation
from app.models.stock import StockLedger, StockLot
from app.services import ledger, reservations
from tests.factories import (
    make_allocation,
    make_bom_line,
    make_build,
    make_location,
    make_lot,
    make_part,
    make_project,
    post,
)

ATTRIBUTION = ledger.Attribution()


def _cached(db: Session, lot: StockLot) -> int:
    db.expire(lot)
    return lot.qty_reserved_milli_cached


# ---------------------------------------------------------------------------
# available()
# ---------------------------------------------------------------------------


def test_available_is_on_hand_minus_reserved(db: Session) -> None:
    """Two caches, subtracted. Never a sum over the ledger or over allocations."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    build = make_build(db, make_project(db))

    assert reservations.available(lot) == 10_000

    reservations.reserve(db, build, lot, 4_000)

    assert reservations.available(lot) == 6_000


def test_available_stays_negative_when_stock_is_over_committed(db: Session) -> None:
    """Not clamped: an over-commit, or a recount that came up short after stock
    was promised, is something a dashboard must show rather than round away."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=1_000)
    build = make_build(db, make_project(db))
    reservations.reserve(db, build, lot, 1_000)

    post(db, lot, -600, kind=LedgerKind.COUNT, counted_qty_milli=400)

    assert reservations.available(lot) == -600


def test_a_negative_lot_does_not_eat_another_bins_stock(db: Session) -> None:
    """Clamped **per lot** in the aggregate. Letting one visibly wrong balance
    subtract from a good bin would fabricate a shortage somewhere else."""
    part = make_part(db)
    location = make_location(db)
    good = make_lot(db, part, location, qty_milli=5_000)
    bad = make_lot(db, part, location, qty_milli=-2_000)
    db.flush()

    assert reservations.available(bad) == -2_000
    assert reservations.available_by_part(db, [part.id]) == {part.id: 5_000}
    assert good.id != bad.id


def test_quarantined_and_retired_stock_is_not_available(db: Session) -> None:
    """Quarantined stock is physically present and must not be promised;
    `reserve` refuses the same lots, so report and write path agree."""
    part = make_part(db)
    location = make_location(db)
    make_lot(db, part, location, qty_milli=3_000)
    held = make_lot(db, part, location, qty_milli=8_000)
    held.status = LotStatus.QUARANTINED
    db.flush()

    assert reservations.available_by_part(db, [part.id]) == {part.id: 3_000}

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.reserve(db, make_build(db, make_project(db)), held, 1_000)
    assert excinfo.value.reason == "lot_not_active"


# ---------------------------------------------------------------------------
# The derived cache: bulk rebuild vs per-lot read vs write path
# ---------------------------------------------------------------------------


def test_the_rebuild_and_the_per_lot_read_agree_over_random_operations(db: Session) -> None:
    """**The regression test for the bug shape that already shipped here.**

    A bulk rebuild and a single-row read computed the same quantity two different
    ways, disagreed, and the bulk path was the one that persisted. So three
    independent computations are compared after every single operation:

    1. the counter the write paths maintain incrementally,
    2. `reserved_milli`, the per-lot recomputation, and
    3. what the one-statement bulk rebuild writes.

    Randomised because the failures in this shape are ordering-dependent: a
    release after a partial consume, a second reservation on a lot already picked
    from. A fixed script tests the sequence its author thought of.
    """
    rng = random.Random(20260728)
    part = make_part(db)
    location = make_location(db)
    lots = [make_lot(db, part, location, qty_milli=100_000) for _ in range(4)]
    builds = [make_build(db, make_project(db, name=f"Board {n}"), build_no=1) for n in range(3)]
    live: list[StockAllocation] = []

    for step in range(200):
        action = rng.random()
        if action < 0.5 or not live:
            lot = rng.choice(lots)
            free = reservations.available(lot)
            if free <= 0:
                continue
            live.append(
                reservations.reserve(db, rng.choice(builds), lot, rng.randint(1, min(free, 9_000)))
            )
        elif action < 0.75:
            allocation = live.pop(rng.randrange(len(live)))
            reservations.release(db, allocation)
        else:
            allocation = live.pop(rng.randrange(len(live)))
            whole = rng.random() < 0.5
            qty = allocation.qty_milli if whole else max(1, allocation.qty_milli // 2)
            reservations.consume(db, allocation, attribution=ATTRIBUTION, qty_milli=qty)
            if not whole:
                # The remainder is a fresh RESERVED row on the same lot, which is
                # the state the next operation has to cope with.
                remainder = (
                    db.execute(
                        select(StockAllocation)
                        .where(
                            StockAllocation.lot_id == allocation.lot_id,
                            StockAllocation.state == AllocationState.RESERVED,
                        )
                        .order_by(StockAllocation.id.desc())
                    )
                    .scalars()
                    .first()
                )
                assert remainder is not None
                live.append(remainder)

        maintained = {lot.id: lot.qty_reserved_milli_cached for lot in lots}
        recomputed = {lot.id: reservations.reserved_milli(db, lot.id) for lot in lots}
        assert maintained == recomputed, f"step {step}: write path vs per-lot read"

        assert check_reserved_quantity_drift(db).is_clean, f"step {step}: drift check"
        rebuild_reserved_quantities(db)
        rebuilt = {lot.id: _cached(db, lot) for lot in lots}
        assert rebuilt == maintained, f"step {step}: bulk rebuild vs write path"

    # The randomisation has to have actually exercised something.
    assert db.execute(select(StockAllocation)).scalars().all()
    assert any(value > 0 for value in maintained.values())


def test_the_rebuild_repairs_a_counter_nothing_maintained(db: Session) -> None:
    """The factory writes allocations without touching the counter, so the value
    can only have come from the rebuild — which is what makes incremental
    maintenance safe in the first place."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=50_000)
    build = make_build(db, make_project(db))
    make_allocation(db, build, part, 6_000, AllocationState.RESERVED, lot=lot)
    make_allocation(db, build, part, 2_000, AllocationState.RELEASED, lot=lot)
    db.flush()

    assert _cached(db, lot) == 0
    drift = check_reserved_quantity_drift(db)
    assert drift.drift_count == 1
    assert drift.sample_ids == (lot.id,)

    rebuild_reserved_quantities(db)

    assert _cached(db, lot) == 6_000
    assert check_reserved_quantity_drift(db).is_clean


def test_the_drift_check_records_itself_in_cache_state(db: Session) -> None:
    """Same contract as the lot-balance check: drift is a visible number in
    `cache_state`, not a log line nobody greps."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    make_allocation(
        db, make_build(db, make_project(db)), part, 1_000, AllocationState.RESERVED, lot=lot
    )
    db.flush()

    check_reserved_quantity_drift(db)
    row = db.execute(
        text("SELECT drift_count, detail, last_checked_at FROM cache_state WHERE name = :n"),
        {"n": RESERVATIONS},
    ).one()

    assert row.drift_count == 1
    assert row.detail == str(lot.id)
    assert row.last_checked_at is not None

    rebuild_reserved_quantities(db)
    is_dirty = db.execute(
        text("SELECT is_dirty FROM cache_state WHERE name = :n"), {"n": RESERVATIONS}
    ).scalar_one()
    assert is_dirty == 0


# ---------------------------------------------------------------------------
# Reserving: over-commitment is refused
# ---------------------------------------------------------------------------


def test_a_reservation_beyond_available_is_refused(db: Session) -> None:
    """The deliberate departure from "a scan is never rejected": a scan records
    something that happened, a reservation promises something that has not. The
    refusal carries the number the user needs in order to act."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    build = make_build(db, make_project(db))

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.reserve(db, build, lot, 5_001)

    assert excinfo.value.reason == "insufficient_available"
    assert "5000" in str(excinfo.value)
    # Nothing was written, so a refused reservation cannot leave the cache moved.
    assert _cached(db, lot) == 0
    assert db.execute(select(StockAllocation)).scalars().all() == []


def test_one_build_cannot_reserve_what_another_already_holds(db: Session) -> None:
    """Availability is global, which is the entire reason the reserved cache
    exists: the second build sees the first build's hold as gone."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=1_000)
    first = make_build(db, make_project(db, name="Board A"))
    second = make_build(db, make_project(db, name="Board B"))
    reservations.reserve(db, first, lot, 700)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.reserve(db, second, lot, 400)
    assert excinfo.value.reason == "insufficient_available"

    # ...but what is genuinely left is still reservable.
    reservations.reserve(db, second, lot, 300)
    assert _cached(db, lot) == 1_000
    assert reservations.available(lot) == 0


def test_over_commitment_is_possible_only_as_an_explicit_choice(db: Session) -> None:
    """ "Reserve it anyway, more is on order" is legitimate. It is honest because
    it is recorded as a decision rather than reached by a fallback, and it is
    what makes `available` go negative."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=1_000)
    build = make_build(db, make_project(db))

    allocation = reservations.reserve(db, build, lot, 2_500, allow_overcommit=True)

    assert allocation.state == AllocationState.RESERVED
    assert reservations.available(lot) == -1_500
    # Still derived: the deliberate over-commit is in the source of truth too.
    assert reservations.reserved_milli(db, lot.id) == 2_500
    assert check_reserved_quantity_drift(db).is_clean


def test_a_reservation_records_the_lot_its_part_and_the_hold_time(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=9_000)
    project = make_project(db)
    line = make_bom_line(db, project, part_id=part.id)
    build = make_build(db, project)

    allocation = reservations.reserve(db, build, lot, 3_000, bom_line=line, note="bench")

    assert allocation.part_id == part.id
    assert allocation.lot_id == lot.id
    assert allocation.bom_line_id == line.id
    assert allocation.reserved_at is not None
    assert allocation.note == "bench"


def test_a_mismatched_part_and_lot_are_refused(db: Session) -> None:
    """The invariant `stock_allocations` cannot express without a `CHECK`: when
    `lot_id` is set, `part_id` must be the lot's part. The API receives both from
    a client, and trusting the lot silently would file a pick under the wrong
    part."""
    lot_part = make_part(db, name="The part in the bin")
    other = make_part(db, name="Something else")
    lot = make_lot(db, lot_part, make_location(db), qty_milli=5_000)
    build = make_build(db, make_project(db))

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.reserve(db, build, lot, 1_000, part_id=other.id)
    assert excinfo.value.reason == "part_lot_mismatch"


def test_a_line_from_another_project_is_refused(db: Session) -> None:
    """No composite FK can say a line belongs to this build's project, so the
    service says it."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    build = make_build(db, make_project(db, name="Board A"))
    foreign_line = make_bom_line(db, make_project(db, name="Board B"), part_id=part.id)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.reserve(db, build, lot, 1_000, bom_line=foreign_line)
    assert excinfo.value.reason == "line_not_in_build"


def test_a_closed_build_cannot_take_new_holds(db: Session) -> None:
    """A hold taken by a finished build is one nothing will ever come back to
    release, and it reads as missing inventory forever."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    build = make_build(db, make_project(db), status=BuildStatus.COMPLETED)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.reserve(db, build, lot, 1_000)
    assert excinfo.value.reason == "build_closed"


# ---------------------------------------------------------------------------
# Releasing
# ---------------------------------------------------------------------------


def test_releasing_gives_the_hold_back_and_keeps_the_row(db: Session) -> None:
    """`RELEASED`, not deleted: "we planned this and dropped it" is a different
    statement from "this never happened"."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=4_000)
    build = make_build(db, make_project(db))
    allocation = reservations.reserve(db, build, lot, 4_000)

    reservations.release(db, allocation, note="build cancelled")

    assert allocation.state == AllocationState.RELEASED
    assert allocation.note == "build cancelled"
    assert reservations.available(lot) == 4_000
    assert _cached(db, lot) == 0
    assert reservations.reserved_milli(db, lot.id) == 0
    assert db.get(StockAllocation, allocation.id) is not None


def test_releasing_twice_is_refused_rather_than_decrementing_twice(db: Session) -> None:
    """A double-tapped button that decremented twice is precisely the drift this
    whole design exists to make impossible."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=4_000)
    allocation = reservations.reserve(db, make_build(db, make_project(db)), lot, 1_000)
    reservations.release(db, allocation)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.release(db, allocation)
    assert excinfo.value.reason == "already_released"
    assert _cached(db, lot) == 0


def test_a_consumed_allocation_cannot_be_released(db: Session) -> None:
    """The parts left the bin. The correction for that is a compensating ledger
    row, not a bookkeeping change here."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=4_000)
    allocation = reservations.reserve(db, make_build(db, make_project(db)), lot, 1_000)
    reservations.consume(db, allocation, attribution=ATTRIBUTION)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.release(db, allocation)
    assert excinfo.value.reason == "already_consumed"


def test_closing_a_build_releases_its_open_holds_and_keeps_its_picks(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=20_000)
    build = make_build(db, make_project(db))
    picked = reservations.reserve(db, build, lot, 5_000)
    reservations.consume(db, picked, attribution=ATTRIBUTION)
    reservations.reserve(db, build, lot, 6_000)
    planned = make_allocation(db, build, part, 1_000, AllocationState.PLANNED)

    released = reservations.release_build(db, build)

    assert released == 2  # the reservation and the planned row
    assert _cached(db, lot) == 0
    assert picked.state == AllocationState.CONSUMED
    assert planned.state == AllocationState.RELEASED
    assert check_reserved_quantity_drift(db).is_clean


# ---------------------------------------------------------------------------
# Consuming: the only path that touches the ledger, and it delegates
# ---------------------------------------------------------------------------


def test_consuming_a_hold_writes_one_ledger_row_and_moves_both_caches(db: Session) -> None:
    """The reserved counter and the balance step in the same instant, so
    `available` never dips or spikes through an intermediate state."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    build = make_build(db, make_project(db))
    allocation = reservations.reserve(db, build, lot, 4_000)
    assert reservations.available(lot) == 6_000

    _, row = reservations.consume(db, allocation, attribution=ATTRIBUTION)

    assert allocation.state == AllocationState.CONSUMED
    assert allocation.consumed_ledger_seq == row.seq
    assert allocation.consumed_at is not None
    assert row.delta_milli == -4_000
    assert row.ref_type == reservations.BUILD_REF_TYPE
    assert row.ref_id == build.id
    assert lot.qty_milli_cached == 6_000
    assert reservations.available(lot) == 6_000  # unchanged: hold became a pick
    assert db.execute(select(StockLedger)).scalars().all() == [row]


def test_a_partial_pick_states_what_moved_and_re_holds_the_rest(db: Session) -> None:
    """The `CONSUMED` row's quantity must equal the ledger row's, or "what went
    into build 2" disagrees with the ledger — the only question this table
    exists to answer."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    build = make_build(db, make_project(db))
    allocation = reservations.reserve(db, build, lot, 6_000)

    _, row = reservations.consume(db, allocation, attribution=ATTRIBUTION, qty_milli=2_500)

    assert allocation.qty_milli == 2_500
    assert row.delta_milli == -2_500
    remainder = (
        db.execute(select(StockAllocation).where(StockAllocation.state == AllocationState.RESERVED))
        .scalars()
        .one()
    )
    assert remainder.qty_milli == 3_500
    assert remainder.lot_id == lot.id
    assert _cached(db, lot) == 3_500
    assert reservations.reserved_milli(db, lot.id) == 3_500
    assert lot.qty_milli_cached == 7_500


def test_consuming_more_than_is_held_is_refused(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    allocation = reservations.reserve(db, make_build(db, make_project(db)), lot, 1_000)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.consume(db, allocation, attribution=ATTRIBUTION, qty_milli=1_001)
    assert excinfo.value.reason == "exceeds_hold"


def test_a_planned_row_cannot_be_consumed(db: Session) -> None:
    """`PLANNED` names no lot, so there is nothing to take out of anything."""
    part = make_part(db)
    build = make_build(db, make_project(db))
    allocation = make_allocation(db, build, part, 1_000, AllocationState.PLANNED)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.consume(db, allocation, attribution=ATTRIBUTION)
    assert excinfo.value.reason == "not_reserved"


def test_an_explicit_attribution_is_not_overwritten(db: Session) -> None:
    """An explicit `ref_type`/`ref_id` is a statement about provenance; replacing
    it would make the ledger lie about why the stock moved."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    allocation = reservations.reserve(db, make_build(db, make_project(db)), lot, 1_000)

    _, row = reservations.consume(
        db,
        allocation,
        attribution=ledger.Attribution(ref_type="rework_order", ref_id=42),
    )

    assert row.ref_type == "rework_order"
    assert row.ref_id == 42


# ---------------------------------------------------------------------------
# Shortage: two kinds, and netting across lines
# ---------------------------------------------------------------------------


def test_a_matched_line_with_enough_stock_is_satisfied(db: Session) -> None:
    part = make_part(db)
    make_lot(db, part, make_location(db), qty_milli=10_000)
    project = make_project(db)
    make_bom_line(db, project, line_no=1, part_id=part.id, qty_per_assembly_milli=3_000)
    build = make_build(db, project, assembly_count=3)

    report = reservations.shortage_for_build(db, build)

    (line,) = report.lines
    assert line.kind is ShortageKind.SATISFIED
    assert line.required_milli == 9_000  # demand scales with assembly_count
    assert line.available_milli == 10_000
    assert line.shortfall_milli == 0
    assert report.is_buildable


def test_a_matched_line_short_of_stock_reports_the_number_missing(db: Session) -> None:
    part = make_part(db)
    make_lot(db, part, make_location(db), qty_milli=4_000)
    project = make_project(db)
    make_bom_line(db, project, line_no=1, part_id=part.id, qty_per_assembly_milli=5_000)
    build = make_build(db, project, assembly_count=2)

    report = reservations.shortage_for_build(db, build)

    (line,) = report.lines
    assert line.kind is ShortageKind.SHORT
    assert line.required_milli == 10_000
    assert line.available_milli == 4_000
    assert line.shortfall_milli == 6_000
    assert not report.is_buildable


def test_an_unmatched_line_is_a_different_kind_of_shortage(db: Session) -> None:
    """**The distinction that keeps the report honest.** An unmatched line is not
    "you need three more" — no quantity is computable at all — so availability
    and shortfall are `None` rather than a zero that a total would silently
    absorb, and the build is not buildable however much stock exists."""
    project = make_project(db)
    make_bom_line(
        db, project, line_no=1, qty_per_assembly_milli=3_000, ref_value="4k7", designators="R1"
    )
    build = make_build(db, project, assembly_count=2)

    report = reservations.shortage_for_build(db, build)

    (line,) = report.lines
    assert line.kind is ShortageKind.UNIDENTIFIED
    assert line.part_id is None
    assert line.required_milli == 6_000  # the board needs six of *something*
    assert line.available_milli is None
    assert line.shortfall_milli is None
    assert line.is_blocking
    assert report.unidentified_lines == (line,)
    assert report.short_lines == ()
    assert not report.is_buildable


def test_a_partly_unmatched_bom_separates_the_two_kinds(db: Session) -> None:
    """The realistic case straight out of a KiCad import: some lines matched,
    one short, one nobody has identified. All three must be distinguishable, or
    the user cannot tell "order this" from "decide what this is"."""
    plenty = make_part(db, name="0.1uF 0603", mpn="C0603C104K5R")
    scarce = make_part(db, name="LM358", mpn="LM358DR")
    location = make_location(db)
    make_lot(db, plenty, location, qty_milli=100_000)
    make_lot(db, scarce, location, qty_milli=1_000)

    project = make_project(db)
    make_bom_line(db, project, line_no=1, part_id=plenty.id, qty_per_assembly_milli=8_000)
    make_bom_line(db, project, line_no=2, part_id=scarce.id, qty_per_assembly_milli=2_000)
    make_bom_line(db, project, line_no=3, qty_per_assembly_milli=1_000, ref_value="FB-something")
    make_bom_line(
        db, project, line_no=4, part_id=plenty.id, qty_per_assembly_milli=1_000, is_dnp=True
    )
    build = make_build(db, project, assembly_count=1)

    report = reservations.shortage_for_build(db, build)
    kinds = {line.line_no: line.kind for line in report.lines}

    assert kinds == {
        1: ShortageKind.SATISFIED,
        2: ShortageKind.SHORT,
        3: ShortageKind.UNIDENTIFIED,
        4: ShortageKind.NOT_FITTED,
    }
    assert [line.line_no for line in report.short_lines] == [2]
    assert [line.line_no for line in report.unidentified_lines] == [3]
    assert not report.is_buildable


def test_a_dnp_line_generates_no_demand_but_is_still_reported(db: Session) -> None:
    """It is in the file and gets fitted next revision, so it is shown — but it
    draws nothing, or a board would look short of parts it does not have."""
    part = make_part(db)
    make_lot(db, part, make_location(db), qty_milli=1_000)
    project = make_project(db)
    make_bom_line(
        db, project, line_no=1, part_id=part.id, qty_per_assembly_milli=99_000, is_dnp=True
    )
    build = make_build(db, project, assembly_count=5)

    report = reservations.shortage_for_build(db, build)

    (line,) = report.lines
    assert line.kind is ShortageKind.NOT_FITTED
    assert line.required_milli == 0
    assert line.shortfall_milli == 0
    assert report.is_buildable


def test_two_lines_of_the_same_part_cannot_both_claim_one_pile(db: Session) -> None:
    """Netting across the report, in line order. Two lines each told there are
    600 free would both report satisfied off one pile of 600 — a BOM that looks
    buildable and is not."""
    part = make_part(db)
    make_lot(db, part, make_location(db), qty_milli=600)
    project = make_project(db)
    make_bom_line(db, project, line_no=1, part_id=part.id, qty_per_assembly_milli=500)
    make_bom_line(db, project, line_no=2, part_id=part.id, qty_per_assembly_milli=500)
    build = make_build(db, project)

    report = reservations.shortage_for_build(db, build)
    first, second = report.lines

    assert first.kind is ShortageKind.SATISFIED
    assert first.available_milli == 600
    assert second.kind is ShortageKind.SHORT
    assert second.available_milli == 100  # what line 1 left behind
    assert second.shortfall_milli == 400
    assert not report.is_buildable


def test_this_builds_own_holds_reduce_what_it_still_needs_without_double_counting(
    db: Session,
) -> None:
    """A hold is invisible in `available` (it is inside the reserved cache) and
    is added back as `allocated_milli`, so the same 4000 is never counted twice
    and never lost."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    project = make_project(db)
    line = make_bom_line(db, project, line_no=1, part_id=part.id, qty_per_assembly_milli=10_000)
    build = make_build(db, project)
    reservations.reserve(db, build, lot, 4_000, bom_line=line)

    report = reservations.shortage_for_build(db, build)

    (result,) = report.lines
    assert result.required_milli == 10_000
    assert result.allocated_milli == 4_000
    assert result.available_milli == 6_000  # the hold is not double-counted
    assert result.shortfall_milli == 0
    assert report.is_buildable


def test_another_builds_hold_makes_this_build_short(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    other = make_build(db, make_project(db, name="Board A"))
    reservations.reserve(db, other, lot, 7_000)

    project = make_project(db, name="Board B")
    make_bom_line(db, project, line_no=1, part_id=part.id, qty_per_assembly_milli=5_000)
    build = make_build(db, project)

    report = reservations.shortage_for_build(db, build)

    (line,) = report.lines
    assert line.kind is ShortageKind.SHORT
    assert line.available_milli == 3_000
    assert line.shortfall_milli == 2_000


def test_a_per_line_substitute_covers_a_shortage_and_says_so(db: Session) -> None:
    """`bom_line_substitutes` is consulted before a shortage is reported, and the
    contributing alternate is named so "why is this not short" is answerable."""
    specified = make_part(db, name="RC0603 4k7 1%")
    alternate = make_part(db, name="RT0603 4k7 0.1%")
    location = make_location(db)
    make_lot(db, specified, location, qty_milli=1_000)
    make_lot(db, alternate, location, qty_milli=9_000)

    project = make_project(db)
    line = make_bom_line(db, project, line_no=1, part_id=specified.id, qty_per_assembly_milli=5_000)
    db.add(BomLineSubstitute(bom_line_id=line.id, part_id=alternate.id))
    db.flush()
    build = make_build(db, project)

    report = reservations.shortage_for_build(db, build)

    (result,) = report.lines
    assert result.kind is ShortageKind.SATISFIED
    assert result.available_milli == 10_000
    assert result.substitute_part_ids == (alternate.id,)
    assert report.is_buildable


def test_a_substitute_naming_the_lines_own_part_is_counted_once(db: Session) -> None:
    """A line listing itself as its own alternate must not double its stock.

    `UniqueConstraint("bom_line_id", "part_id")` on `bom_line_substitutes` stops
    the same alternate appearing twice, but nothing joins that column to
    `bom_lines.part_id`, so a row naming the line's own part is accepted. Counted
    twice, 100 on the shelf answered a need for 150 with `available 200,
    satisfied` — and the pool was debited only once while the total was
    accumulated twice, so every later line sharing that part over-reported too.
    "A BOM that looks buildable and is not" is the failure the netting loop
    exists to prevent.

    Latent rather than live today: no route writes `bom_line_substitutes`. The
    realistic path to it is a human matching an unidentified line to one of the
    alternates already listed on it.
    """
    specified = make_part(db, name="LM317T")
    make_lot(db, specified, make_location(db), qty_milli=100_000)

    project = make_project(db)
    line = make_bom_line(
        db, project, line_no=1, part_id=specified.id, qty_per_assembly_milli=150_000
    )
    db.add(BomLineSubstitute(bom_line_id=line.id, part_id=specified.id))
    db.flush()
    build = make_build(db, project)

    report = reservations.shortage_for_build(db, build)

    (result,) = report.lines
    assert result.available_milli == 100_000
    assert result.shortfall_milli == 50_000
    assert result.kind is ShortageKind.SHORT
    # Named once as the line's own part, so it is not also an "alternate".
    assert result.substitute_part_ids == ()
    assert not report.is_buildable


def test_two_lines_sharing_a_self_named_substitute_do_not_over_draw(db: Session) -> None:
    """The knock-on half: a double-counted line leaves the pool wrong for the next.

    The first line draws what it can from the shared pool; the second must see
    only what is left. When the self-named substitute inflated line one's total,
    the pool was debited by less than the report claimed line one had taken, and
    line two inherited stock that was already spoken for.
    """
    shared = make_part(db, name="1N4148")
    make_lot(db, shared, make_location(db), qty_milli=100_000)

    project = make_project(db)
    first = make_bom_line(db, project, line_no=1, part_id=shared.id, qty_per_assembly_milli=80_000)
    db.add(BomLineSubstitute(bom_line_id=first.id, part_id=shared.id))
    make_bom_line(db, project, line_no=2, part_id=shared.id, qty_per_assembly_milli=80_000)
    db.flush()
    build = make_build(db, project)

    report = reservations.shortage_for_build(db, build)

    one, two = report.lines
    # What line one could *see*: the whole shelf, named once. Double-counted it
    # read 200k, which is the number this pins.
    assert one.available_milli == 100_000
    assert one.kind is ShortageKind.SATISFIED
    # 100k on the shelf, 80k drawn by line one, so 20k is all line two can see.
    assert two.available_milli == 20_000
    assert two.shortfall_milli == 60_000
    assert not report.is_buildable


def test_a_substitute_on_an_unmatched_line_does_not_make_it_buildable(db: Session) -> None:
    """Nobody has said the alternate is equivalent *to what*. The line stays
    unidentified until a human matches it."""
    alternate = make_part(db)
    make_lot(db, alternate, make_location(db), qty_milli=50_000)
    project = make_project(db)
    line = make_bom_line(db, project, line_no=1, qty_per_assembly_milli=1_000)
    db.add(BomLineSubstitute(bom_line_id=line.id, part_id=alternate.id))
    db.flush()
    build = make_build(db, project)

    report = reservations.shortage_for_build(db, build)

    (result,) = report.lines
    assert result.kind is ShortageKind.UNIDENTIFIED
    assert result.available_milli is None
    assert not report.is_buildable


def test_stock_used_outside_the_bom_does_not_satisfy_a_line(db: Session) -> None:
    """An allocation with no `bom_line_id` — two extra resistors because one went
    flying — satisfies no line's requirement, so it must not reduce one."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    project = make_project(db)
    make_bom_line(db, project, line_no=1, part_id=part.id, qty_per_assembly_milli=8_000)
    build = make_build(db, project)
    reservations.reserve(db, build, lot, 4_000)  # no bom_line

    report = reservations.shortage_for_build(db, build)

    (line,) = report.lines
    assert line.allocated_milli == 0
    assert line.available_milli == 6_000
    assert line.shortfall_milli == 2_000
