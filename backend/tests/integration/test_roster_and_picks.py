"""The as-built roster and the pick list (ADR 0004's roster section).

Two features, one theme: both are worthless if they are *plausible*. So the
load-bearing assertions here are the ones that fail on the version of this code
that looks right:

* an entry somebody typed in after the fact must be **visibly** different from a
  movement the system captured — otherwise the roster is a document that quietly
  claims more than it knows;
* recording an untracked part must still move `stock_lots.qty_milli_cached`, or
  the roster gets right by making the drawer wrong;
* the pick list must be ordered by `locations.id_path` and not by BOM line. That
  single assertion *is* the feature: BOM order sends the user across the room
  once per line;
* a line that cannot be picked has to appear anyway. A walk that silently omits
  its own gaps reads as complete, and the user finds out at the bench.

Everything runs against a database built by the real migrations, so the ledger's
append-only triggers and every `RESTRICT` are live.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.maintenance import rebuild_reserved_quantities
from app.models.enums import (
    AllocationState,
    BuildStatus,
    LedgerKind,
    LedgerSource,
    LotStatus,
    ShortageKind,
)
from app.models.projects import StockAllocation
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location
from app.services import ledger, picking, reservations, roster
from app.services.tree import location_tree
from tests.factories import (
    make_bom_line,
    make_build,
    make_location,
    make_lot,
    make_part,
    make_project,
)

ATTRIBUTION = ledger.Attribution()


def _tree(db: Session, *names: str) -> list[Location]:
    """A chain of locations, parent to child, with the path cache rebuilt.

    Paths matter here more than anywhere else in the suite — the pick order *is*
    `id_path` — so they are built through the same repository the routes use
    rather than hand-written.
    """
    parent: Location | None = None
    made: list[Location] = []
    for name in names:
        parent = make_location(db, name, parent_id=None if parent is None else parent.id)
        made.append(parent)
    location_tree(db).rebuild_paths()
    return made


def _entry(report: roster.BuildRoster, allocation_id: int) -> roster.RosterEntry:
    for line in report.lines:
        for entry in line.entries:
            if entry.allocation_id == allocation_id:
                return entry
    raise AssertionError(f"allocation {allocation_id} is not in the roster")


def _line(report: roster.BuildRoster, bom_line_id: int) -> roster.RosterLine:
    for line in report.lines:
        if line.bom_line_id == bom_line_id:
            return line
    raise AssertionError(f"bom line {bom_line_id} is not in the roster")


# ---------------------------------------------------------------------------
# Recording what was really used
# ---------------------------------------------------------------------------


def test_recording_a_part_used_moves_the_drawer_as_well_as_the_roster(db: Session) -> None:
    """The whole reason this is a ledger write and not a note on the build.

    ADR 0004 rejects a bookkeeping-only "floating" flag because it leaves
    `qty_milli_cached` lying about a bin. An after-the-fact roster entry has
    exactly the same hazard: the parts really are gone.
    """
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    line = make_bom_line(db, project, qty_per_assembly_milli=2_000, part_id=part.id)

    allocation, row = reservations.record_used(
        db, build, lot, 2_000, attribution=ATTRIBUTION, bom_line=line
    )

    assert lot.qty_milli_cached == 8_000
    assert (row.kind, row.delta_milli) == (LedgerKind.CONSUME, -2_000)
    assert row.ref_type == reservations.BUILD_REF_TYPE
    assert row.ref_id == build.id
    assert AllocationState(allocation.state) is AllocationState.CONSUMED
    assert allocation.consumed_ledger_seq == row.seq
    # Never reserved, so there is nothing for the reserved cache to have moved —
    # and `reserved_at` staying NULL is what says "this hold never existed".
    assert lot.qty_reserved_milli_cached == 0
    assert allocation.reserved_at is None


def test_an_after_the_fact_entry_is_visibly_different_from_a_tracked_one(db: Session) -> None:
    """**The requirement.** One line, two consumed rows, two provenances.

    The roster is only worth reading because it admits which of its rows were
    reconstructed. Both rows below say `consumed 2` and both are true; only one of
    them was witnessed by the system.
    """
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    line = make_bom_line(db, project, qty_per_assembly_milli=4_000, part_id=part.id)

    tracked = reservations.reserve(db, build, lot, 2_000, bom_line=line)
    reservations.consume(db, tracked, attribution=ATTRIBUTION)
    corrected, _ = reservations.record_used(
        db, build, lot, 2_000, attribution=ATTRIBUTION, bom_line=line
    )

    report = roster.roster_for_build(db, build)
    assert _entry(report, tracked.id).is_after_the_fact is False
    assert _entry(report, tracked.id).ledger_source is LedgerSource.MANUAL
    assert _entry(report, corrected.id).is_after_the_fact is True
    assert _entry(report, corrected.id).ledger_source is LedgerSource.RECONCILED

    # Both count as consumed; only the correction is called out as one.
    assert _line(report, line.id).consumed_milli == 4_000
    assert _line(report, line.id).after_the_fact_milli == 2_000
    assert report.after_the_fact_milli == 2_000


def test_the_source_label_is_forced_not_taken_from_the_caller(db: Session) -> None:
    """A correction that could call itself a scan would erase the one fact that
    makes the roster trustworthy, so the service overrides it."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)

    _, row = reservations.record_used(
        db,
        build,
        lot,
        1_000,
        attribution=ledger.Attribution(source=LedgerSource.SCAN),
    )

    assert LedgerSource(row.source) is LedgerSource.RECONCILED


def test_a_part_nobody_planned_for_becomes_an_off_bom_row(db: Session) -> None:
    """`bom_line_id IS NULL` is the signal the BOM is stale, which on an
    iterating prototype is the normal case — so it is reported, not hidden, and
    it satisfies no line's requirement."""
    project = make_project(db)
    build = make_build(db, project)
    planned = make_part(db, name="planned part")
    surprise = make_part(db, name="the fix nobody drew")
    (bin_a,) = _tree(db, "Drawer A")
    make_bom_line(db, project, qty_per_assembly_milli=1_000, part_id=planned.id)
    lot = make_lot(db, surprise, bin_a, qty_milli=5_000)

    reservations.record_used(db, build, lot, 2_000, attribution=ATTRIBUTION)

    report = roster.roster_for_build(db, build)
    off_bom = report.off_bom_lines
    assert len(off_bom) == 1
    assert off_bom[0].part_id == surprise.id
    assert off_bom[0].consumed_milli == 2_000
    # Nobody planned it, so inventing a requirement for it would be the synthetic
    # BOM line the nullable column exists to avoid.
    assert off_bom[0].required_milli == 0
    assert off_bom[0].bom_line_id is None
    # And it is last, after the real lines.
    assert report.lines[-1].is_off_bom is True


def test_two_off_bom_picks_of_one_part_are_one_row(db: Session) -> None:
    """Grouped by part, because "how many extra 10k did this eat" is the
    question, not "how many times did somebody reach for the drawer"."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)

    reservations.record_used(db, build, lot, 1_000, attribution=ATTRIBUTION)
    reservations.record_used(db, build, lot, 2_000, attribution=ATTRIBUTION)

    off_bom = roster.roster_for_build(db, build).off_bom_lines
    assert len(off_bom) == 1
    assert off_bom[0].consumed_milli == 3_000
    assert len(off_bom[0].entries) == 2


def test_recording_use_is_accepted_on_a_closed_build(db: Session) -> None:
    """Where `reserve` and `stage` refuse. A closed build is *precisely* when
    somebody sits down and reconciles what it really used, so refusing here would
    guarantee the roster stays wrong."""
    project = make_project(db)
    build = make_build(db, project, status=BuildStatus.COMPLETED)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)

    with pytest.raises(reservations.ReservationError) as reserved:
        reservations.reserve(db, build, lot, 1_000)
    assert reserved.value.reason == "build_closed"
    with pytest.raises(reservations.ReservationError) as staged:
        reservations.stage(db, build, lot, 1_000, attribution=ATTRIBUTION)
    assert staged.value.reason == "build_closed"

    allocation, _ = reservations.record_used(db, build, lot, 1_000, attribution=ATTRIBUTION)
    assert AllocationState(allocation.state) is AllocationState.CONSUMED


def test_recording_use_accepts_a_quarantined_lot_and_an_over_draw(db: Session) -> None:
    """A record of the past is not a promise about the future.

    `reserve` refuses both of these because promising stock nobody may touch is a
    lie; recording that those parts went into a board months ago is not, and the
    negative balance is the alarm this design wants raised rather than a reason to
    drop the record of what happened.
    """
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=1_000)
    lot.status = LotStatus.QUARANTINED
    db.flush()

    reservations.record_used(db, build, lot, 3_000, attribution=ATTRIBUTION)

    assert lot.qty_milli_cached == -2_000


@pytest.mark.parametrize(
    ("qty_milli", "part_id", "reason"),
    [
        (0, None, "non_positive_qty"),
        (-1, None, "non_positive_qty"),
        (1_000, 999_999, "part_lot_mismatch"),
    ],
)
def test_a_correction_that_cannot_be_interpreted_is_refused(
    db: Session, qty_milli: int, part_id: int | None, reason: str
) -> None:
    """The only refusals: a quantity that is not one, and a part that is not the
    lot's. Everything else about a record of the past is accepted."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)

    with pytest.raises(reservations.ReservationError) as error:
        reservations.record_used(
            db, build, lot, qty_milli, attribution=ATTRIBUTION, part_id=part_id
        )
    assert error.value.reason == reason
    assert lot.qty_milli_cached == 10_000


def test_a_bom_line_from_another_project_is_refused(db: Session) -> None:
    """There is no composite FK that can say a line belongs to this build's
    project, so the service says it."""
    project = make_project(db)
    other = make_project(db, name="someone else's board")
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    foreign = make_bom_line(db, other, part_id=part.id)

    with pytest.raises(reservations.ReservationError) as error:
        reservations.record_used(db, build, lot, 1_000, attribution=ATTRIBUTION, bom_line=foreign)
    assert error.value.reason == "line_not_in_build"


def test_a_correction_leaves_the_reserved_cache_rebuildable(db: Session) -> None:
    """The bug shape this repo has already shipped once: a write path that moves
    stock and forgets that the reserved cache is derived from a predicate it must
    still satisfy. `record_used` writes a `CONSUMED` row, which the predicate
    excludes — so the rebuild must change nothing."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    reservations.reserve(db, build, lot, 4_000)

    reservations.record_used(db, build, lot, 1_000, attribution=ATTRIBUTION)

    before = lot.qty_reserved_milli_cached
    rebuild_reserved_quantities(db)
    db.expire(lot)
    assert (before, lot.qty_reserved_milli_cached) == (4_000, 4_000)
    assert reservations.reserved_milli(db, lot.id) == 4_000


# ---------------------------------------------------------------------------
# The roster as a report
# ---------------------------------------------------------------------------


def test_the_roster_keeps_reserved_staged_and_consumed_apart(db: Session) -> None:
    """ADR 0004: merging them lets a build look accounted-for off parts that are
    still in a drawer, or already soldered into last week's board."""
    project = make_project(db)
    build = make_build(db, project, assembly_count=3)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=100_000)
    line = make_bom_line(db, project, qty_per_assembly_milli=10_000, part_id=part.id)

    reservations.reserve(db, build, lot, 5_000, bom_line=line)
    reservations.stage(db, build, lot, 7_000, attribution=ATTRIBUTION, bom_line=line)
    reservations.record_used(db, build, lot, 3_000, attribution=ATTRIBUTION, bom_line=line)

    row = _line(roster.roster_for_build(db, build), line.id)
    assert (row.reserved_milli, row.staged_milli, row.consumed_milli) == (5_000, 7_000, 3_000)
    assert row.accounted_milli == 15_000
    # Demand is derived from `assembly_count` on every read, so this needed no
    # backfill when the count was set to 3.
    assert row.required_milli == 30_000


def test_the_roster_lists_a_line_nothing_has_happened_to(db: Session) -> None:
    """A roster of only the lines with activity would read as complete while half
    the board was unaccounted for."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    touched = make_bom_line(db, project, line_no=1, qty_per_assembly_milli=1_000, part_id=part.id)
    untouched = make_bom_line(db, project, line_no=2, qty_per_assembly_milli=2_000)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    reservations.record_used(db, build, lot, 1_000, attribution=ATTRIBUTION, bom_line=touched)

    report = roster.roster_for_build(db, build)
    assert [line.bom_line_id for line in report.lines] == [touched.id, untouched.id]
    blank = _line(report, untouched.id)
    assert (blank.required_milli, blank.consumed_milli, blank.entries) == (2_000, 0, ())


def test_a_withdrawal_that_was_put_back_stays_in_the_history_and_in_no_total(
    db: Session,
) -> None:
    """`RELEASED` is listed but sums to nothing. Deleting the row would say the
    withdrawal never happened, which is a different — and false — statement."""
    project = make_project(db)
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    line = make_bom_line(db, project, qty_per_assembly_milli=5_000, part_id=part.id)

    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION, bom_line=line)
    reservations.unstage(db, move.allocation, attribution=ATTRIBUTION)

    row = _line(roster.roster_for_build(db, build), line.id)
    assert row.staged_milli == 0
    assert row.accounted_milli == 0
    assert [entry.state for entry in row.entries] == [AllocationState.RELEASED]


def test_a_staged_entry_reports_the_project_box_it_is_sitting_in(db: Session) -> None:
    """The payoff of staging being a real move: "where are my project's parts" is
    answered by an ordinary location path."""
    project = make_project(db, name="Blinky", revision="v2")
    build = make_build(db, project)
    part = make_part(db)
    (bin_a,) = _tree(db, "Drawer A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    line = make_bom_line(db, project, qty_per_assembly_milli=5_000, part_id=part.id)

    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION, bom_line=line)

    entry = _entry(roster.roster_for_build(db, build), move.allocation.id)
    assert entry.location_label_path == "PROJECTS / Blinky v2"
    assert entry.ledger_seq == move.allocation.staged_ledger_seq
    assert entry.is_after_the_fact is False


# ---------------------------------------------------------------------------
# The pick list: the walk
# ---------------------------------------------------------------------------


def _walkable_room(db: Session) -> tuple[Location, Location, Location]:
    """Two cabinets, three drawers, built so `id_path` order is known.

    `Cabinet A` is created first, so its subtree sorts ahead of `Cabinet B`'s and
    its own two drawers sort in creation order.
    """
    cabinet_a = make_location(db, "Cabinet A")
    drawer_a1 = make_location(db, "Drawer A1", parent_id=cabinet_a.id)
    drawer_a2 = make_location(db, "Drawer A2", parent_id=cabinet_a.id)
    cabinet_b = make_location(db, "Cabinet B")
    drawer_b1 = make_location(db, "Drawer B1", parent_id=cabinet_b.id)
    location_tree(db).rebuild_paths()
    return drawer_a1, drawer_a2, drawer_b1


def test_stops_are_ordered_for_walking_and_not_by_bom_line(db: Session) -> None:
    """**This assertion is the feature.**

    The BOM asks for the far cabinet first and the near drawers last. Answering
    in that order is what makes a user cross the room once per line; answering in
    `id_path` order crosses it once.
    """
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, drawer_a2, drawer_b1 = _walkable_room(db)

    far = make_part(db, name="in the far cabinet")
    middle = make_part(db, name="in drawer A2")
    near = make_part(db, name="in drawer A1")
    for line_no, (part, place) in enumerate(
        [(far, drawer_b1), (middle, drawer_a2), (near, drawer_a1)], start=1
    ):
        make_bom_line(db, project, line_no=line_no, qty_per_assembly_milli=1_000, part_id=part.id)
        make_lot(db, part, place, qty_milli=10_000)

    plan = picking.pick_list_for_build(db, build)

    assert [stop.label_path for stop in plan.stops] == [
        "Cabinet A / Drawer A1",
        "Cabinet A / Drawer A2",
        "Cabinet B / Drawer B1",
    ]
    # …which is the reverse of BOM order, so the ordering cannot be an accident of
    # how the takes were computed.
    assert [take.line_no for stop in plan.stops for take in stop.takes] == [3, 2, 1]
    assert [stop.id_path for stop in plan.stops] == sorted(stop.id_path for stop in plan.stops)
    assert plan.is_complete is True


def test_two_lines_in_one_bin_are_one_stop(db: Session) -> None:
    """The cost being optimised is opening the drawer, not reading the line."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, _, _ = _walkable_room(db)
    for line_no in (1, 2):
        part = make_part(db, name=f"part {line_no}")
        make_bom_line(db, project, line_no=line_no, qty_per_assembly_milli=1_000, part_id=part.id)
        make_lot(db, part, drawer_a1, qty_milli=5_000)

    plan = picking.pick_list_for_build(db, build)

    assert len(plan.stops) == 1
    assert [take.line_no for take in plan.stops[0].takes] == [1, 2]
    assert plan.stops[0].qty_milli == 2_000


def test_one_line_from_two_bins_says_how_many_from_each(db: Session) -> None:
    """ "It is in these three drawers" is not an instruction. A quantity per bin
    is."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, drawer_a2, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=9_000, part_id=part.id)
    make_lot(db, part, drawer_a1, qty_milli=4_000)
    make_lot(db, part, drawer_a2, qty_milli=6_000)

    plan = picking.pick_list_for_build(db, build)

    # Largest first, so A2 is emptied and only the remainder is cut out of A1.
    assert [(stop.label_path, stop.qty_milli) for stop in plan.stops] == [
        ("Cabinet A / Drawer A1", 3_000),
        ("Cabinet A / Drawer A2", 6_000),
    ]
    # The bin that was emptied says so; the one that was cut into does not.
    whole = {stop.label_path: stop.takes[0].whole_lot for stop in plan.stops}
    assert whole == {"Cabinet A / Drawer A1": False, "Cabinet A / Drawer A2": True}


def test_an_exact_fit_is_taken_whole_instead_of_splitting_a_bigger_lot(db: Session) -> None:
    """ "Prefer whole lots over splitting where it does not cost extra walking" —
    one stop either way, so the one that leaves no cut tape wins."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, drawer_a2, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=3_000, part_id=part.id)
    reel = make_lot(db, part, drawer_a1, qty_milli=50_000)
    strip = make_lot(db, part, drawer_a2, qty_milli=3_000)

    plan = picking.pick_list_for_build(db, build)

    assert len(plan.stops) == 1
    assert plan.stops[0].takes[0].lot_id == strip.id
    assert plan.stops[0].takes[0].whole_lot is True
    assert reel.qty_milli_cached == 50_000  # untouched, and a plan is a plan


def test_with_no_exact_fit_the_largest_lot_is_drawn_first(db: Session) -> None:
    """Fewest stops, and at most one lot split — the last one drawn."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, drawer_a2, drawer_b1 = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=7_000, part_id=part.id)
    make_lot(db, part, drawer_a1, qty_milli=2_000)
    make_lot(db, part, drawer_a2, qty_milli=6_000)
    make_lot(db, part, drawer_b1, qty_milli=1_000)

    plan = picking.pick_list_for_build(db, build)

    # A2's six first (largest), then A1's two cut down to one. B1 is never
    # visited, which is the point of drawing largest-first.
    assert [(stop.label_path, stop.qty_milli) for stop in plan.stops] == [
        ("Cabinet A / Drawer A1", 1_000),
        ("Cabinet A / Drawer A2", 6_000),
    ]


def test_a_cut_in_an_open_cabinet_beats_an_exact_fit_across_the_room(db: Session) -> None:
    """ "Prefer whole lots **where it does not cost extra walking**" — and B1's exact
    fit costs a whole second cabinet.

    B1 holding precisely the remainder is the trap: taking it splits nothing, and a
    draw that ranked "no split" above "no extra cabinet" would take it every time.
    Two stops either way, so the tie is broken on the walk, and cutting tape in a
    drawer of a cabinet already standing open is the cheaper of the two.
    """
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, drawer_a2, drawer_b1 = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=7_000, part_id=part.id)
    make_lot(db, part, drawer_a2, qty_milli=6_000)
    make_lot(db, part, drawer_a1, qty_milli=5_000)
    exact = make_lot(db, part, drawer_b1, qty_milli=1_000)

    plan = picking.pick_list_for_build(db, build)

    assert [(stop.label_path, stop.qty_milli) for stop in plan.stops] == [
        ("Cabinet A / Drawer A1", 1_000),
        ("Cabinet A / Drawer A2", 6_000),
    ]
    assert exact.id not in {take.lot_id for stop in plan.stops for take in stop.takes}


def test_nearness_is_ancestors_shared_and_not_characters_shared(db: Session) -> None:
    """`/1/` and `/12/` share a character and no cabinet.

    Measuring nearness on the raw `id_path` string would read those two as
    neighbours purely because of how autoincrement numbered them, and the plan
    would drift as ids grew past nine — a bug that cannot be reproduced on a small
    database.
    """
    assert picking._nearness("/1/2/9/", {4: "/1/2/4/"}) == 2  # same cabinet
    assert picking._nearness("/1/2/9/", {40: "/12/40/"}) == 0  # only a digit in common
    # Nothing open yet: the first lot of a line is chosen on quantity alone.
    assert picking._nearness("/1/2/9/", {}) == 0


def test_an_unmatched_line_is_a_gap_rather_than_a_silent_omission(db: Session) -> None:
    """A pick list that dropped it would be read as complete. There is nothing to
    go and look for, and that is a fact worth printing."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, _, _ = _walkable_room(db)
    known = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=1_000, part_id=known.id)
    unmatched = make_bom_line(db, project, line_no=2, qty_per_assembly_milli=4_000)
    make_lot(db, known, drawer_a1, qty_milli=10_000)

    plan = picking.pick_list_for_build(db, build)

    assert plan.is_complete is False
    assert [(gap.bom_line_id, gap.kind) for gap in plan.gaps] == [
        (unmatched.id, ShortageKind.UNIDENTIFIED)
    ]
    assert plan.gaps[0].part_id is None
    assert (plan.gaps[0].needed_milli, plan.gaps[0].pickable_milli) == (4_000, 0)
    assert plan.gaps[0].shortfall_milli == 4_000


def test_a_partly_pickable_line_is_both_a_stop_and_a_gap(db: Session) -> None:
    """The case a pick list lies about most easily: it lists the takes it can and
    says nothing about the rest."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    line = make_bom_line(db, project, line_no=1, qty_per_assembly_milli=10_000, part_id=part.id)
    make_lot(db, part, drawer_a1, qty_milli=4_000)

    plan = picking.pick_list_for_build(db, build)

    assert plan.stops[0].qty_milli == 4_000
    assert [
        (gap.bom_line_id, gap.kind, gap.pickable_milli, gap.shortfall_milli) for gap in plan.gaps
    ] == [(line.id, ShortageKind.SHORT, 4_000, 6_000)]
    assert plan.is_complete is False


def test_a_lot_another_build_holds_is_not_offered(db: Session) -> None:
    """Read through `reservations.available`, so the same units are never
    promised to two builds — which is discovered at the bench otherwise."""
    project = make_project(db)
    mine = make_build(db, project, build_no=1)
    theirs = make_build(db, project, build_no=2)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=10_000, part_id=part.id)
    lot = make_lot(db, part, drawer_a1, qty_milli=10_000)
    reservations.reserve(db, theirs, lot, 8_000)

    plan = picking.pick_list_for_build(db, mine)

    assert plan.qty_milli == 2_000
    assert plan.gaps[0].shortfall_milli == 8_000


def test_stock_in_a_project_staging_box_is_not_offered(db: Session) -> None:
    """ADR 0004: those parts are still stock and still findable, but they are
    spoken for. Excluded by position in the tree, not by `is_staging` — which
    `INBOX` also carries."""
    project = make_project(db)
    build = make_build(db, project, build_no=1)
    hoarder = make_build(db, project, build_no=2)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=10_000, part_id=part.id)
    lot = make_lot(db, part, drawer_a1, qty_milli=10_000)
    reservations.stage(db, hoarder, lot, 6_000, attribution=ATTRIBUTION)

    plan = picking.pick_list_for_build(db, build)

    assert plan.qty_milli == 4_000
    assert [stop.label_path for stop in plan.stops] == ["Cabinet A / Drawer A1"]
    assert plan.gaps[0].shortfall_milli == 6_000


def test_a_hold_this_build_already_has_comes_back_with_its_allocation_id(db: Session) -> None:
    """The walk has to include what was reserved *for* this build — that is what
    a hold is for — and `allocation_id` is how staging consumes the hold instead
    of opening a second one beside it."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    line = make_bom_line(db, project, line_no=1, qty_per_assembly_milli=5_000, part_id=part.id)
    lot = make_lot(db, part, drawer_a1, qty_milli=10_000)
    allocation = reservations.reserve(db, build, lot, 5_000, bom_line=line)

    plan = picking.pick_list_for_build(db, build)

    assert plan.is_complete is True
    assert len(plan.stops) == 1
    (take,) = plan.stops[0].takes
    assert take.allocation_id == allocation.id
    assert take.qty_milli == 5_000
    # And the held quantity is not *also* proposed from free stock: exactly one
    # take for the line, not two summing to ten.
    assert plan.qty_milli == 5_000


def test_a_hold_its_bin_can_no_longer_fill_produces_no_take(db: Session) -> None:
    """Clamped to what the bin can hand over, mirroring the shortage report's
    `undeliverable` arithmetic — otherwise the walk and the report together
    promise the same units twice. The line still surfaces, as a gap."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    line = make_bom_line(db, project, line_no=1, qty_per_assembly_milli=5_000, part_id=part.id)
    lot = make_lot(db, part, drawer_a1, qty_milli=5_000)
    reservations.reserve(db, build, lot, 5_000, bom_line=line)
    ledger.recount(db, lot, 0, attribution=ATTRIBUTION)

    plan = picking.pick_list_for_build(db, build)

    assert plan.stops == ()
    assert [(gap.bom_line_id, gap.shortfall_milli) for gap in plan.gaps] == [(line.id, 5_000)]


def test_a_substitute_is_only_drawn_after_the_lines_own_part_and_is_flagged(db: Session) -> None:
    """Substituting is a decision a human made once and is now acted on blind at
    a drawer, so the take says so — and the line's own part goes first, exactly as
    the shortage report accumulates availability."""
    from app.models.projects import BomLineSubstitute

    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, drawer_a2, _ = _walkable_room(db)
    own = make_part(db, name="the specified part")
    alternate = make_part(db, name="the accepted alternate")
    line = make_bom_line(db, project, line_no=1, qty_per_assembly_milli=8_000, part_id=own.id)
    db.add(BomLineSubstitute(bom_line_id=line.id, part_id=alternate.id))
    db.flush()
    make_lot(db, own, drawer_a1, qty_milli=3_000)
    make_lot(db, alternate, drawer_a2, qty_milli=9_000)

    plan = picking.pick_list_for_build(db, build)

    takes = {stop.label_path: stop.takes[0] for stop in plan.stops}
    assert takes["Cabinet A / Drawer A1"].qty_milli == 3_000
    assert takes["Cabinet A / Drawer A1"].is_substitute is False
    # Only the remainder comes from the alternate, even though it holds more.
    assert takes["Cabinet A / Drawer A2"].qty_milli == 5_000
    assert takes["Cabinet A / Drawer A2"].is_substitute is True


def test_a_dnp_line_is_never_walked_for(db: Session) -> None:
    """In the file, not on the board: it generates no demand, so it is neither a
    stop nor a gap."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(
        db, project, line_no=1, qty_per_assembly_milli=5_000, part_id=part.id, is_dnp=True
    )
    make_lot(db, part, drawer_a1, qty_milli=10_000)

    plan = picking.pick_list_for_build(db, build)

    assert (plan.stops, plan.gaps) == ((), ())
    assert plan.is_complete is True


def test_raising_the_assembly_count_lengthens_the_walk_with_nothing_written(
    db: Session,
) -> None:
    """Demand is derived (ADR 0004), and the pick list reads the same derivation
    the shortage report does rather than a second copy of it."""
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=4_000, part_id=part.id)
    make_lot(db, part, drawer_a1, qty_milli=100_000)

    assert picking.pick_list_for_build(db, build).qty_milli == 4_000
    build.assembly_count = 3
    db.flush()
    assert picking.pick_list_for_build(db, build).qty_milli == 12_000
    assert db.execute(select(StockAllocation)).scalars().all() == []


def test_a_bin_with_a_printed_id_offers_it_for_scan_verification(db: Session) -> None:
    """A stop the user can confirm by scanning beats one they have to match by eye
    against a label."""
    from app.models.enums import EntityType
    from app.services import shortid

    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, _, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=1_000, part_id=part.id)
    make_lot(db, part, drawer_a1, qty_milli=10_000)
    code = shortid.allocate(db, EntityType.LOCATION, drawer_a1.id)

    plan = picking.pick_list_for_build(db, build)

    assert plan.stops[0].short_id == code


# ---------------------------------------------------------------------------
# Over the API
# ---------------------------------------------------------------------------


def _api_fixture(client: TestClient) -> tuple[int, int, int, int]:
    """A project with one matched line, one build, and a lot in a bin.

    Returns `(project_id, build_id, bom_line_id, lot_id)`.
    """
    location = client.post("/api/locations", json={"name": "API drawer"}).json()["location"]
    part = client.post("/api/parts", json={"name": "API part", "mpn": "API-1"}).json()["part"]
    lot = client.post(
        "/api/stock/receive",
        json={"part_id": part["id"], "location_id": location["id"], "qty_milli": 20_000},
    ).json()["lot"]

    project = client.post("/api/projects", json={"name": "API board"}).json()["project"]
    build = client.post(f"/api/projects/{project['id']}/builds", json={"assembly_count": 2}).json()[
        "build"
    ]
    imported = client.post(
        f"/api/projects/{project['id']}/bom/import",
        json={"content": "Reference,Value,Qty,MPN\nR1,10k,1,API-1\n"},
    )
    assert imported.status_code == 200, imported.text
    line_id = imported.json()["lines"][0]["id"]
    return project["id"], build["id"], line_id, lot["id"]


def test_the_roster_endpoint_marks_a_correction_as_one(client: TestClient) -> None:
    _, build_id, line_id, lot_id = _api_fixture(client)

    recorded = client.post(
        f"/api/builds/{build_id}/record-used",
        json={"lot_id": lot_id, "qty_milli": 2_000, "bom_line_id": line_id},
    )
    assert recorded.status_code == 200, recorded.text
    body = recorded.json()
    assert body["allocation"]["is_after_the_fact"] is True
    assert body["allocation"]["ledger_source"] == "reconciled"
    assert body["lot"]["qty_milli"] == 18_000

    roster_body = client.get(f"/api/builds/{build_id}/roster").json()
    assert roster_body["after_the_fact_milli"] == 2_000
    assert roster_body["off_bom_count"] == 0
    line = roster_body["lines"][0]
    assert line["consumed_milli"] == 2_000
    assert line["after_the_fact_milli"] == 2_000
    assert line["entries"][0]["is_after_the_fact"] is True


def test_a_replayed_correction_does_not_take_the_stock_twice(client: TestClient) -> None:
    """An append-only ledger cannot take back a doubled correction except by
    writing a third row, so this endpoint is idempotency-guarded."""
    _, build_id, line_id, lot_id = _api_fixture(client)
    body = {
        "lot_id": lot_id,
        "qty_milli": 2_000,
        "bom_line_id": line_id,
        "client_op_id": "b7a3f3f4-0000-4000-8000-00000000abcd",
    }

    first = client.post(f"/api/builds/{build_id}/record-used", json=body)
    second = client.post(f"/api/builds/{build_id}/record-used", json=body)

    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.json()["seq"] == first.json()["seq"]
    assert client.get(f"/api/stock/lots/{lot_id}").json()["qty_milli"] == 18_000


def test_the_pick_list_endpoint_returns_a_walk(client: TestClient) -> None:
    _, build_id, line_id, lot_id = _api_fixture(client)

    body = client.get(f"/api/builds/{build_id}/pick-list").json()

    assert body["is_complete"] is True
    assert body["qty_milli"] == 2_000  # 1 per assembly, 2 assemblies
    (stop,) = body["stops"]
    assert stop["label_path"] == "API drawer"
    assert stop["id_path"] != ""
    (take,) = stop["takes"]
    assert (take["lot_id"], take["qty_milli"], take["bom_line_id"]) == (lot_id, 2_000, line_id)
    assert take["allocation_id"] is None
    assert take["part_mpn"] == "API-1"


def test_recording_more_than_the_bom_asked_for_is_recorded_not_refused(
    client: TestClient,
) -> None:
    """A correction against no line at all is the "part nobody planned for", and
    it comes back as an off-BOM row rather than a 409."""
    _, build_id, _, lot_id = _api_fixture(client)

    response = client.post(
        f"/api/builds/{build_id}/record-used",
        json={"lot_id": lot_id, "qty_milli": 1_000, "note": "one went flying"},
    )
    assert response.status_code == 200, response.text

    body = client.get(f"/api/builds/{build_id}/roster").json()
    assert body["off_bom_count"] == 1
    off_bom = [line for line in body["lines"] if line["is_off_bom"]]
    assert off_bom[0]["entries"][0]["note"] == "one went flying"


def test_a_correction_against_someone_elses_lot_part_is_409_not_500(
    client: TestClient,
) -> None:
    _, build_id, _, lot_id = _api_fixture(client)

    response = client.post(
        f"/api/builds/{build_id}/record-used",
        json={"lot_id": lot_id, "qty_milli": 1_000, "part_id": 999_999},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "part_lot_mismatch"


@pytest.mark.parametrize("path", ["roster", "pick-list"])
def test_both_reads_404_on_an_unknown_build(client: TestClient, path: str) -> None:
    assert client.get(f"/api/builds/999999/{path}").status_code == 404


def test_an_absurd_correction_quantity_is_422_not_500(client: TestClient) -> None:
    """The bare-`int` overflow this project has already shipped once: a quantity
    past SQLite's binding range has to fail validation, not parameter binding —
    and nothing may be written on the way to failing."""
    from app.db.session import get_session_factory

    _, build_id, _, lot_id = _api_fixture(client)

    response = client.post(
        f"/api/builds/{build_id}/record-used",
        json={"lot_id": lot_id, "qty_milli": 10**30},
    )

    assert response.status_code == 422
    session = get_session_factory()()
    try:
        corrections = session.execute(
            select(StockLedger).where(StockLedger.source == LedgerSource.RECONCILED)
        ).all()
        assert corrections == []
    finally:
        session.close()


def test_the_walk_never_offers_a_lot_that_is_not_active(db: Session) -> None:
    """`reserve` refuses quarantined stock, so offering it here would have the
    walk and the write path disagree about what exists."""
    project = make_project(db)
    build = make_build(db, project)
    drawer_a1, drawer_a2, _ = _walkable_room(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, qty_per_assembly_milli=5_000, part_id=part.id)
    quarantined = make_lot(db, part, drawer_a1, qty_milli=50_000)
    quarantined.status = LotStatus.QUARANTINED
    make_lot(db, part, drawer_a2, qty_milli=5_000)
    db.flush()

    plan = picking.pick_list_for_build(db, build)

    assert [stop.label_path for stop in plan.stops] == ["Cabinet A / Drawer A2"]
    assert db.get(StockLot, quarantined.id) is not None  # still there, just not offered
