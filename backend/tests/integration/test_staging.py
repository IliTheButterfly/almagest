"""Staged parts: withdrawal to a project is a real move (ADR 0004).

Every test here exists because the *cheap* implementation of "floating parts" —
a flag on the allocation, stock left where it is — passes a naive test suite and
fails the user. So the load-bearing assertions are all about the numbers a flag
would have got wrong:

* the drawer's count drops the **instant** parts are staged, because they left it;
* staged parts do **not** inflate the source lot's reserved quantity, because
  they are not in it any more and counting them there would double-count them
  against their new location;
* the bulk rebuild, the drift check and the per-lot read still agree afterwards —
  the bug shape that has already shipped in this repo once;
* un-staging is the existing compensating undo, not a second mechanism;
* raising `assembly_count` raises what is `needed` with **no allocation row
  written at all**, which is what "demand is derived" has to mean;
* deleting a project with parts still in its box is refused, because the parts
  are real and on a shelf.

Everything runs against a database built by the real migrations, so the ledger's
append-only triggers, the `RESTRICT`s and the reserved-cache delete trigger are
all live.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.maintenance import check_reserved_quantity_drift, rebuild_reserved_quantities
from app.models.enums import AllocationState, BuildStatus, LedgerKind, LotStatus
from app.models.projects import StockAllocation
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location
from app.services import ledger, reservations, staging
from app.services.assignment import assign_location
from tests.factories import (
    inbox_location,
    make_bom_line,
    make_build,
    make_location,
    make_lot,
    make_part,
    make_project,
)

ATTRIBUTION = ledger.Attribution()


def _reserved(db: Session, lot: StockLot) -> int:
    db.expire(lot)
    return lot.qty_reserved_milli_cached


def _on_hand(db: Session, lot: StockLot) -> int:
    db.expire(lot)
    return lot.qty_milli_cached


def _allocation_rows(db: Session) -> list[tuple[int, str, int, int | None, int | None]]:
    """Every allocation as a comparable tuple — the evidence for "nothing was
    written". Timestamps are excluded deliberately: `updated_at` has an
    `onupdate`, so including it would make the test pass for the wrong reason
    (any write would show) *and* fail for the right one (a no-op flush would
    not)."""
    return [
        (row.id, str(row.state), row.qty_milli, row.lot_id, row.staged_ledger_seq)
        for row in db.execute(select(StockAllocation).order_by(StockAllocation.id)).scalars()
    ]


# ---------------------------------------------------------------------------
# The location layout
# ---------------------------------------------------------------------------


def test_nothing_is_created_until_the_first_withdrawal(db: Session) -> None:
    """Lazy, per ADR 0004: a project that never takes anything out of stock must
    not litter the storage tree with an empty box."""
    project = make_project(db, name="Blinky", revision="v2")
    make_build(db, project)

    assert staging.staging_root(db, create=False) is None
    assert staging.staging_locations_of_project(db, project) == []
    assert staging.staging_subtree_prefix(db) is None


def test_staging_builds_root_project_and_assembly_levels(db: Session) -> None:
    """Three ordinary locations, and the *tree* carries the granularity — "a
    project, or one of its assemblies" needs no new column."""
    project = make_project(db, name="Blinky", revision="v2")
    build = make_build(db, project, assembly_count=2)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=10_000)

    floating = reservations.stage(db, build, lot, 2_000, attribution=ATTRIBUTION)
    committed = reservations.stage(db, build, lot, 3_000, attribution=ATTRIBUTION, assembly_no=2)

    assert floating.location.name == "Blinky v2"
    assert floating.location.label_path == "PROJECTS / Blinky v2"
    assert committed.location.label_path == "PROJECTS / Blinky v2 / Build 1 Assembly 2"
    assert committed.location.parent_id == floating.location.id

    # Every level is furniture, not somewhere auto-assignment may put stock.
    for node in (floating.location, committed.location):
        assert node.is_staging is True
        assert node.is_placeable is False
    root = staging.staging_root(db, create=False)
    assert root is not None
    assert (root.is_staging, root.is_placeable) == (True, False)

    # And the build records where its parts went.
    assert build.staging_location_id == floating.location.id


def test_a_second_withdrawal_reuses_the_same_boxes(db: Session) -> None:
    """Idempotent by a DB-unique `slot_label`, not by a name match: project names
    are deliberately not unique, so a name lookup would eventually pour two
    projects' parts into one box."""
    project = make_project(db, name="Blinky")
    build = make_build(db, project, assembly_count=1)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)

    first = reservations.stage(db, build, lot, 1_000, attribution=ATTRIBUTION, assembly_no=1)
    second = reservations.stage(db, build, lot, 1_000, attribution=ATTRIBUTION, assembly_no=1)

    assert first.location.id == second.location.id
    assert len(staging.staging_locations_of_project(db, project)) == 2

    twin = make_project(db, name="Blinky")  # same name, different project
    twin_build = make_build(db, twin)
    other = reservations.stage(db, twin_build, lot, 1_000, attribution=ATTRIBUTION)
    assert other.location.id != first.location.parent_id or other.location.id != second.location.id
    assert other.location.parent_id == staging.staging_root(db, create=False).id  # type: ignore[union-attr]


def test_an_assembly_the_build_does_not_make_is_refused(db: Session) -> None:
    """ "Assembly 7 of 3" names a unit that does not exist, so there is nothing a
    later correction could attach the parts to."""
    project = make_project(db)
    build = make_build(db, project, assembly_count=3)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)

    with pytest.raises(staging.StagingError) as excinfo:
        reservations.stage(db, build, lot, 1_000, attribution=ATTRIBUTION, assembly_no=7)
    assert excinfo.value.reason == "unknown_assembly"


def test_a_project_box_is_never_proposed_as_a_home(db: Session) -> None:
    """`is_placeable = False` is the whole mechanism — no special case in the
    assignment ladder, and `INBOX` is still the fallback it always was."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    move = reservations.stage(db, build, lot, 1_000, attribution=ATTRIBUTION)
    db.flush()

    result = assign_location(db, part)
    assert result.location_id != move.location.id
    assert result.location_id != move.location.parent_id
    # The flag alone does not decide: INBOX carries it and is still placeable.
    assert inbox_location(db).is_staging is True


def test_the_reserved_delete_trigger_survived_the_column_addition(db: Session) -> None:
    """`batch_alter_table` recreates the table on SQLite, and a recreated table
    loses its triggers — Alembic restores the indexes and knows nothing about
    `trg_stock_allocations_deleted_reserved`. Losing it silently reintroduces the
    bug it was written for, and the loss is invisible until a nightly drift check
    fires. `test_phase2_review_findings` proves the *behaviour*; this pins the
    trigger by name at the revision that put it at risk.
    """
    names = (
        db.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table"),
            {"table": "stock_allocations"},
        )
        .scalars()
        .all()
    )
    assert "trg_stock_allocations_deleted_reserved" in names


# ---------------------------------------------------------------------------
# The drawer's count, and the reserved cache
# ---------------------------------------------------------------------------


def test_the_drawers_count_drops_the_instant_parts_are_staged(db: Session) -> None:
    """**The reason staging is a move and not a flag.** A flag would leave
    `qty_milli_cached` claiming parts are in a drawer they have left, and the
    user finds out by opening the drawer."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    drawer = make_location(db, "Drawer A")
    lot = make_lot(db, part, drawer, qty_milli=10_000)

    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)

    assert _on_hand(db, lot) == 6_000
    assert _on_hand(db, move.staging_lot) == 4_000
    assert move.staging_lot.location_id == move.location.id
    # Conservative: the two rows sum to zero, so nothing was created or lost.
    assert sum(row.delta_milli for row in move.rows) == 0
    assert {LedgerKind(row.kind) for row in move.rows} == {
        LedgerKind.SPLIT_OUT,
        LedgerKind.SPLIT_IN,
    }


def test_staged_parts_do_not_count_against_the_source_lots_reserved(db: Session) -> None:
    """The subtle one. The parts are not in the source lot, so holding them there
    would count the same units twice — once as a hold on a drawer that no longer
    has them, once as real stock in the project box."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)

    allocation = reservations.reserve(db, build, lot, 4_000)
    assert _reserved(db, lot) == 4_000

    reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION, allocation=allocation)

    assert AllocationState(allocation.state) is AllocationState.STAGED
    assert _reserved(db, lot) == 0
    # ...and the reserved predicate itself never learned about `staged`.
    assert reservations.reserved_milli(db, lot.id) == 0
    staged_sum = db.execute(
        text("SELECT COALESCE(SUM(qty_milli), 0) FROM stock_allocations WHERE state = 'staged'")
    ).scalar_one()
    assert staged_sum == 4_000


def test_the_rebuild_and_the_per_lot_read_still_agree_after_a_stage(db: Session) -> None:
    """One predicate, three consumers. A staged row that the bulk rebuild and the
    per-lot read disagreed about is exactly the bug shape this repo has already
    shipped once."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=20_000)

    keep = reservations.reserve(db, build, lot, 5_000)
    move_from = reservations.reserve(db, build, lot, 6_000)
    reservations.stage(db, build, lot, 6_000, attribution=ATTRIBUTION, allocation=move_from)
    db.flush()

    maintained = _reserved(db, lot)
    assert maintained == keep.qty_milli == 5_000
    assert reservations.reserved_milli(db, lot.id) == maintained
    assert check_reserved_quantity_drift(db).is_clean
    rebuild_reserved_quantities(db)
    assert _reserved(db, lot) == maintained

    # The staging lot holds real stock and nothing is reserved against it.
    staging_lot = db.get(StockLot, move_from.lot_id)
    assert staging_lot is not None
    assert staging_lot.qty_milli_cached == 6_000
    assert reservations.reserved_milli(db, staging_lot.id) == 0


def test_a_partial_stage_re_holds_the_remainder(db: Session) -> None:
    """Same rule as a partial pick: the row that changes state has to state what
    actually moved, or "what went into build 2" disagrees with the ledger."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    allocation = reservations.reserve(db, build, lot, 5_000)

    reservations.stage(db, build, lot, 2_000, attribution=ATTRIBUTION, allocation=allocation)

    assert allocation.qty_milli == 2_000
    assert _reserved(db, lot) == 3_000
    assert reservations.reserved_milli(db, lot.id) == 3_000
    remainder = (
        db.execute(
            select(StockAllocation)
            .where(StockAllocation.state == AllocationState.RESERVED)
            .order_by(StockAllocation.id.desc())
        )
        .scalars()
        .first()
    )
    assert remainder is not None
    assert (remainder.qty_milli, remainder.lot_id) == (3_000, lot.id)


def test_staging_a_whole_lot_moves_it_and_keeps_its_identity(db: Session) -> None:
    """A whole bag going into the project box is a relocation, not a new lot:
    minting one would destroy lot identity and per-lot cost continuity, which is
    what `ledger.move_whole_lot` exists to protect."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    drawer = make_location(db, "Drawer A")
    lot = make_lot(db, part, drawer, qty_milli=7_000)
    lot.unit_cost_micro = 4_200
    db.flush()

    move = reservations.stage(db, build, lot, 7_000, attribution=ATTRIBUTION)

    assert move.staging_lot.id == lot.id
    assert lot.location_id == move.location.id
    assert lot.unit_cost_micro == 4_200
    assert [LedgerKind(row.kind) for row in move.rows] == [LedgerKind.MOVE]
    assert db.execute(select(StockLot).where(StockLot.location_id == drawer.id)).first() is None


def test_a_whole_lot_someone_else_holds_is_split_instead(db: Session) -> None:
    """Relocating a lot another build has reserved would move stock out from
    under that hold. Splitting leaves the hold pointing at a lot that is still
    where it was."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    other = make_build(db, make_project(db, name="Other board"))
    reservations.reserve(db, other, lot, 1_000)

    build = make_build(db, make_project(db, name="Mine"))
    move = reservations.stage(db, build, lot, 5_000, attribution=ATTRIBUTION)

    assert move.staging_lot.id != lot.id
    assert len(move.rows) == 2
    assert _reserved(db, lot) == 1_000


def test_staging_is_accepted_even_when_it_overdraws(db: Session) -> None:
    """A record of the past is never refused — unlike a reservation, which is a
    promise about the future. The parts physically left the bin; refusing would
    delete the truth to protect a number that is meant to raise an alarm."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=1_000)

    reservations.stage(db, build, lot, 3_000, attribution=ATTRIBUTION)

    assert _on_hand(db, lot) == -2_000


def test_quarantined_stock_is_not_staged(db: Session) -> None:
    """Quarantine exists so a lot cannot be used or relocated without someone
    deciding about it, and a withdrawal to a project is both."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    lot.status = LotStatus.QUARANTINED
    db.flush()

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.stage(db, build, lot, 1_000, attribution=ATTRIBUTION)
    assert excinfo.value.reason == "lot_not_active"


def test_staged_stock_is_not_available_to_another_build(db: Session) -> None:
    """Parts in a project box are still stock and still findable; they are not
    *free*. Counting them would let a second BOM read buildable off them."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    build = make_build(db, make_project(db, name="Mine"))

    move = reservations.stage(db, build, lot, 6_000, attribution=ATTRIBUTION)
    db.flush()

    assert reservations.available_by_part(db, [part.id]) == {part.id: 4_000}

    other = make_build(db, make_project(db, name="Other"))
    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.reserve(db, other, move.staging_lot, 1_000)
    assert excinfo.value.reason == "lot_in_project_staging"


def test_inbox_stock_stays_available(db: Session) -> None:
    """The exclusion is by position in the tree, not by `is_staging`: INBOX
    carries that flag and its stock is ordinary free stock. Filtering on the flag
    would hide real inventory from every build."""
    part = make_part(db)
    make_lot(db, part, inbox_location(db), qty_milli=8_000)
    # Force the staging root to exist, so the filter is actually applied.
    staging.staging_root(db)
    db.flush()

    assert reservations.available_by_part(db, [part.id]) == {part.id: 8_000}


# ---------------------------------------------------------------------------
# Un-staging: the existing compensating undo
# ---------------------------------------------------------------------------


def test_unstaging_is_the_existing_compensating_undo(db: Session) -> None:
    """No new mechanism: compensating rows against the movement the allocation
    names. The history says "this happened, then it was undone", which is not the
    same statement as "this never happened"."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    drawer = make_location(db, "Drawer A")
    lot = make_lot(db, part, drawer, qty_milli=10_000)

    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)
    assert _on_hand(db, lot) == 6_000

    undo = reservations.unstage(db, move.allocation, attribution=ATTRIBUTION)

    assert AllocationState(undo.allocation.state) is AllocationState.RELEASED
    assert undo.restored_lot.id == lot.id
    assert _on_hand(db, lot) == 10_000
    assert _on_hand(db, move.staging_lot) == 0
    # Newest first, so a multi-row operation unwinds in the order it was applied.
    assert [row.reversal_of_seq for row in undo.compensations] == [
        move.rows[1].seq,
        move.rows[0].seq,
    ]
    # Nothing was deleted: the original rows are still there, plus the undo.
    assert db.execute(select(StockLedger)).scalars().all()


def test_unstaging_a_whole_lot_move_puts_the_lot_back(db: Session) -> None:
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    drawer = make_location(db, "Drawer A")
    lot = make_lot(db, part, drawer, qty_milli=7_000)

    move = reservations.stage(db, build, lot, 7_000, attribution=ATTRIBUTION)
    reservations.unstage(db, move.allocation, attribution=ATTRIBUTION)

    assert lot.location_id == drawer.id
    assert _on_hand(db, lot) == 7_000


def test_un_staging_twice_is_refused(db: Session) -> None:
    """A double-tapped undo must not double the correction. The first un-stage
    left the row `RELEASED`, so the state guard catches it before the ledger's
    own `already_reversed` guard has to — two independent defences, and this is
    the one a user can actually reach."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)

    reservations.unstage(db, move.allocation, attribution=ATTRIBUTION)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.unstage(db, move.allocation, attribution=ATTRIBUTION)
    assert excinfo.value.reason == "not_staged"
    assert _on_hand(db, lot) == 10_000  # and the correction was applied once


def test_a_staged_allocation_cannot_just_be_released(db: Session) -> None:
    """Releasing the claim without moving the parts would leave real stock in a
    staging location that nothing accounts for."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.release(db, move.allocation)
    assert excinfo.value.reason == "is_staged"


def test_closing_a_build_leaves_staged_parts_where_they_are(db: Session) -> None:
    """A hold is a promise and closing the build ends it. Staged parts are in a
    box on a shelf, and releasing the row would claim they came back."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    held = reservations.reserve(db, build, lot, 1_000)
    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)

    released = reservations.release_build(db, build, note="closed")

    assert released == 1
    assert AllocationState(held.state) is AllocationState.RELEASED
    assert AllocationState(move.allocation.state) is AllocationState.STAGED


# ---------------------------------------------------------------------------
# Consuming staged parts into the assembly
# ---------------------------------------------------------------------------


def test_consuming_staged_parts_takes_them_out_of_the_project_box(db: Session) -> None:
    """The drawer's count already dropped when they were staged. Taking it down
    again here would remove the same units twice."""
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    part = make_part(db)
    drawer = make_location(db, "Drawer A")
    lot = make_lot(db, part, drawer, qty_milli=10_000)
    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION, assembly_no=1)

    allocation, row = reservations.consume_staged(db, move.allocation, attribution=ATTRIBUTION)

    assert AllocationState(allocation.state) is AllocationState.CONSUMED
    assert allocation.consumed_ledger_seq == row.seq
    assert _on_hand(db, move.staging_lot) == 0
    assert _on_hand(db, lot) == 6_000  # unchanged by the consumption
    assert _reserved(db, lot) == 0


def test_a_partial_build_leaves_the_rest_staged_and_unreversible(db: Session) -> None:
    """Soldering three of the five you fetched. The remainder keeps no
    `staged_ledger_seq`, because reversing the original movement would put back
    more parts than are in the box."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    move = reservations.stage(db, build, lot, 5_000, attribution=ATTRIBUTION)

    reservations.consume_staged(db, move.allocation, attribution=ATTRIBUTION, qty_milli=3_000)

    assert move.allocation.qty_milli == 3_000
    remainder = (
        db.execute(
            select(StockAllocation)
            .where(StockAllocation.state == AllocationState.STAGED)
            .order_by(StockAllocation.id.desc())
        )
        .scalars()
        .first()
    )
    assert remainder is not None
    assert (remainder.qty_milli, remainder.staged_ledger_seq) == (2_000, None)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.unstage(db, remainder, attribution=ATTRIBUTION)
    assert excinfo.value.reason == "no_staging_movement"


def test_parts_already_built_in_cannot_be_unstaged(db: Session) -> None:
    """The box no longer holds them, so a compensating row would drive the
    staging lot negative and claim soldered parts came back."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    move = reservations.stage(db, build, lot, 5_000, attribution=ATTRIBUTION)
    # Somebody consumed the staging lot outside the allocation — an
    # after-the-fact correction, or a second build sharing the box.
    ledger.consume(db, move.staging_lot, 5_000, attribution=ATTRIBUTION)

    with pytest.raises(reservations.ReservationError) as excinfo:
        reservations.unstage(db, move.allocation, attribution=ATTRIBUTION)
    assert excinfo.value.reason == "partly_consumed"


# ---------------------------------------------------------------------------
# Derived demand: changing the assembly count writes nothing
# ---------------------------------------------------------------------------


def test_raising_the_assembly_count_raises_needed_with_no_allocation_write(db: Session) -> None:
    """**The requirement, satisfied by construction rather than by an event
    handler that could be missed.** `needed = demand - accounted` is computed on
    every read, so raising the count moves the shortfall and nothing is
    backfilled, migrated or rewritten."""
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    part = make_part(db)
    line = make_bom_line(db, project, qty_per_assembly_milli=2_000, part_id=part.id)
    lot = make_lot(db, part, make_location(db), qty_milli=2_000)

    reservations.stage(db, build, lot, 2_000, attribution=ATTRIBUTION, bom_line=line)
    db.flush()

    before = reservations.shortage_for_build(db, build).lines[0]
    assert (before.staged_milli, before.needed_milli, before.shortfall_milli) == (2_000, 0, 0)
    rows_before = _allocation_rows(db)

    build.assembly_count = 3
    db.flush()

    after = reservations.shortage_for_build(db, build).lines[0]
    assert after.required_milli == 6_000
    assert after.staged_milli == 2_000  # unchanged: the parts did not move
    assert after.needed_milli == 4_000  # the shortfall moved with the count
    assert after.shortfall_milli == 4_000  # and no free stock is left to cover it
    assert _allocation_rows(db) == rows_before, "demand is derived; nothing may be written"

    # Lowering it again strands nothing — the physical facts are untouched.
    build.assembly_count = 1
    db.flush()
    assert reservations.shortage_for_build(db, build).lines[0].needed_milli == 0
    assert _allocation_rows(db) == rows_before


def test_a_shortage_line_keeps_the_three_numbers_apart(db: Session) -> None:
    """ADR 0004: merging reserved, staged and consumed lets a BOM look buildable
    off parts that are already soldered into last week's board."""
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    part = make_part(db)
    line = make_bom_line(db, project, qty_per_assembly_milli=9_000, part_id=part.id)
    lot = make_lot(db, part, make_location(db), qty_milli=9_000)

    reservations.reserve(db, build, lot, 1_000, bom_line=line)
    staged = reservations.stage(db, build, lot, 2_000, attribution=ATTRIBUTION, bom_line=line)
    built = reservations.stage(db, build, lot, 3_000, attribution=ATTRIBUTION, bom_line=line)
    reservations.consume_staged(db, built.allocation, attribution=ATTRIBUTION)
    db.flush()

    report = reservations.shortage_for_build(db, build).lines[0]
    assert (report.reserved_milli, report.staged_milli, report.consumed_milli) == (
        1_000,
        2_000,
        3_000,
    )
    assert report.allocated_milli == 6_000
    assert report.needed_milli == 3_000
    assert staged.allocation.qty_milli == 2_000


# ---------------------------------------------------------------------------
# Deleting a project
# ---------------------------------------------------------------------------


def test_deleting_a_project_with_staged_stock_is_refused(client: TestClient, db: Session) -> None:
    """A refusal, not a cleanup: the parts are real and on a shelf. Cascading
    would delete the only record of why a box of components is sitting there."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)
    db.commit()

    response = client.delete(f"/api/projects/{project.id}")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "stock_in_project_staging"
    assert response.json()["detail"]["lot_ids"] == [move.staging_lot.id]
    assert client.get(f"/api/projects/{project.id}").status_code == 200


def test_deleting_a_project_whose_box_is_empty_succeeds(client: TestClient, db: Session) -> None:
    """Emptied is not the same as never used. The box itself is named by
    undeletable ledger rows, so it stays behind — visible, empty and harmless —
    while the project goes."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=10_000)
    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)
    reservations.consume_staged(db, move.allocation, attribution=ATTRIBUTION)
    db.commit()

    response = client.delete(f"/api/projects/{project.id}")

    assert response.status_code == 200, response.text
    assert client.get(f"/api/projects/{project.id}").status_code == 404
    # The ledger still says where those parts went.
    assert db.get(Location, move.location.id) is not None


# ---------------------------------------------------------------------------
# Through the wire
# ---------------------------------------------------------------------------


def test_the_whole_workflow_over_http(client: TestClient, db: Session) -> None:
    """Stage from a hold, un-stage, stage again, build it in — the shape a bench
    session actually has."""
    part = make_part(db, "resistor")
    project = make_project(db, name="Blinky", revision="v1")
    build = make_build(db, project, assembly_count=2)
    line = make_bom_line(db, project, qty_per_assembly_milli=1_000, part_id=part.id)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=10_000)
    db.commit()

    held = client.post(
        f"/api/builds/{build.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": 4_000, "bom_line_id": line.id},
    )
    assert held.status_code == 200, held.text
    allocation_id = held.json()["allocation"]["id"]
    assert held.json()["lot"]["qty_reserved_milli"] == 4_000

    staged = client.post(
        f"/api/builds/{build.id}/stage",
        json={
            "lot_id": lot.id,
            "qty_milli": 4_000,
            "allocation_id": allocation_id,
            "assembly_no": 2,
            "client_op_id": "stage-1",
        },
    )
    assert staged.status_code == 200, staged.text
    body = staged.json()
    assert body["allocation"]["state"] == "staged"
    assert body["source_lot"]["qty_milli"] == 6_000
    assert body["source_lot"]["qty_reserved_milli"] == 0
    assert body["staging_lot"]["qty_milli"] == 4_000
    assert body["staging_lot"]["location_label_path"].endswith("Build 1 Assembly 2")
    assert len(body["seqs"]) == 2

    # A retried request is the stored response, not a second withdrawal.
    replay = client.post(
        f"/api/builds/{build.id}/stage",
        json={
            "lot_id": lot.id,
            "qty_milli": 4_000,
            "allocation_id": allocation_id,
            "assembly_no": 2,
            "client_op_id": "stage-1",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["seqs"] == body["seqs"]

    shortages = client.get(f"/api/builds/{build.id}/shortages").json()
    assert shortages["lines"][0]["staged_milli"] == 4_000
    assert shortages["lines"][0]["needed_milli"] == 0

    back = client.post(
        f"/api/builds/{build.id}/unstage",
        json={"allocation_id": allocation_id, "client_op_id": "unstage-1"},
    )
    assert back.status_code == 200, back.text
    assert back.json()["allocation"]["state"] == "released"
    assert back.json()["lot"]["qty_milli"] == 10_000
    assert back.json()["staging_lot"]["qty_milli"] == 0
    assert len(back.json()["reversed_seqs"]) == 2

    again = client.post(
        f"/api/builds/{build.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 2_000, "bom_line_id": line.id},
    )
    assert again.status_code == 200, again.text
    second_id = again.json()["allocation"]["id"]

    consumed = client.post(
        f"/api/builds/{build.id}/consume-staged",
        json={"allocation_id": second_id, "qty_milli": 2_000},
    )
    assert consumed.status_code == 200, consumed.text
    assert consumed.json()["allocation"]["state"] == "consumed"
    assert consumed.json()["lot"]["qty_milli"] == 0

    read = client.get(f"/api/builds/{build.id}").json()
    assert read["staging_location_id"] is not None


def test_staging_to_another_builds_allocation_is_a_404(client: TestClient, db: Session) -> None:
    """The id is not addressable under this build at all."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    mine = make_build(db, make_project(db, name="Mine"))
    theirs = make_build(db, make_project(db, name="Theirs"))
    allocation = reservations.reserve(db, theirs, lot, 1_000)
    db.commit()

    response = client.post(
        f"/api/builds/{mine.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 1_000, "allocation_id": allocation.id},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_allocation"


def test_staging_into_a_closed_build_is_refused(client: TestClient, db: Session) -> None:
    """A closed build is not somewhere to move parts *to*: nothing would ever
    consume or release the row."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    build = make_build(db, make_project(db), status=BuildStatus.COMPLETED)
    db.commit()

    response = client.post(
        f"/api/builds/{build.id}/stage", json={"lot_id": lot.id, "qty_milli": 1_000}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "build_closed"


def test_every_staging_numeric_field_rejects_an_absurd_value(
    client: TestClient, db: Session
) -> None:
    """The sweep every route module gets: a bare `int` overflows SQLite's
    parameter binding and 500s, so every numeric field is a bounded alias."""
    absurd = 10**30
    part = make_part(db)
    lot = make_lot(db, part, make_location(db), qty_milli=5_000)
    build = make_build(db, make_project(db))
    db.commit()

    cases: list[tuple[str, dict[str, object]]] = [
        (f"/api/builds/{build.id}/stage", {"lot_id": absurd, "qty_milli": 1_000}),
        (f"/api/builds/{build.id}/stage", {"lot_id": lot.id, "qty_milli": absurd}),
        (
            f"/api/builds/{build.id}/stage",
            {"lot_id": lot.id, "qty_milli": 1_000, "assembly_no": absurd},
        ),
        (
            f"/api/builds/{build.id}/stage",
            {"lot_id": lot.id, "qty_milli": 1_000, "allocation_id": absurd},
        ),
        (f"/api/builds/{build.id}/unstage", {"allocation_id": absurd}),
        (f"/api/builds/{build.id}/consume-staged", {"allocation_id": absurd}),
        (
            f"/api/builds/{build.id}/consume-staged",
            {"allocation_id": 1, "qty_milli": absurd},
        ),
    ]
    for path, payload in cases:
        response = client.post(path, json=payload)
        assert response.status_code == 422, f"{path} {payload} -> {response.status_code}"

    assert client.delete(f"/api/projects/{absurd}").status_code == 422
