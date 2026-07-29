"""Structural guards on the projects/BOM schema.

Two properties are worth this much test code, because both are expensive to
retrofit and neither shows up as an error when broken:

* **`bom_lines.part_id` is nullable and stays actionable.** If an unmatched line
  could not land, import would be all-or-nothing and the user would go back to a
  spreadsheet — which is the failure mode that killed the prior art.
* **`stock_lots.qty_reserved_milli_cached` is derived.** These tests write
  allocations without ever touching the counter and then reconstruct it, so a
  future change that starts hand-maintaining it — and therefore starts drifting
  — has something that goes red.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models.enums import AllocationState, BuildStatus, LedgerKind, ProjectStatus
from app.models.projects import RESERVED_CACHE_REBUILD_SQL, BomLine, BomLineSubstitute
from app.models.stock import StockLot
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


def _reserved(db: Session, lot: StockLot) -> int:
    """Read the cache the way an API path must: straight off the lot."""
    db.expire(lot)
    return lot.qty_reserved_milli_cached


def _rebuild(db: Session) -> None:
    db.execute(text(RESERVED_CACHE_REBUILD_SQL))


# --------------------------------------------------------------------------
# bom_lines: a messy import has to land
# --------------------------------------------------------------------------


def test_a_bom_line_lands_with_no_part_at_all(db: Session) -> None:
    """The whole reason import is cheap. A KiCad line naming a part we do not
    stock is a legal row carrying everything the file said, not a failed
    import."""
    project = make_project(db)
    line = make_bom_line(
        db,
        project,
        line_no=7,
        designators="R1,R4,R7",
        qty_per_assembly_milli=3_000,
        ref_value="4k7",
        footprint="R_0603_1608Metric",
        raw_fields_json='{"Reference": "R1,R4,R7", "Value": "4k7"}',
    )
    db.commit()

    assert line.id is not None
    assert line.part_id is None
    assert line.is_match_confirmed is False
    # Still actionable: the value and designators are what a human matches from.
    assert line.ref_value == "4k7"


def test_deleting_a_part_reverts_its_bom_lines_to_unmatched(db: Session) -> None:
    """`SET NULL`, not `CASCADE`. Removing a part from the catalogue must not
    delete the line — the board still has that component on it, so the line goes
    back to exactly the unmatched state an import produces."""
    project = make_project(db)
    part = make_part(db, name="Some resistor")
    line = make_bom_line(db, project, part_id=part.id, is_match_confirmed=True, designators="R9")
    db.commit()

    db.delete(part)
    db.commit()
    db.expire(line)

    assert db.get(BomLine, line.id) is not None
    assert line.part_id is None
    assert line.designators == "R9"


def test_a_malformed_bom_is_not_rejected_by_a_unique_constraint(db: Session) -> None:
    """No `UNIQUE(project_id, line_no)` and no uniqueness on designators. A
    hand-edited CSV with two "line 3"s, or the same designator twice, is exactly
    the file the nullable `part_id` exists to accept — rejecting it here would
    undo that."""
    project = make_project(db)
    make_bom_line(db, project, line_no=3, designators="C1")
    make_bom_line(db, project, line_no=3, designators="C1")
    db.commit()

    assert db.scalar(select(text("COUNT(*)")).select_from(BomLine)) == 2


def test_unmatched_lines_are_found_through_the_partial_index(db: Session) -> None:
    """ "What still needs a part?" is the worklist that makes a cheap import
    honest, so it must not degrade into a table scan as BOMs accumulate."""
    project = make_project(db)
    part = make_part(db)
    make_bom_line(db, project, line_no=1, part_id=part.id)
    make_bom_line(db, project, line_no=2)
    db.commit()

    plan = db.execute(
        text(
            "EXPLAIN QUERY PLAN SELECT id FROM bom_lines"
            " WHERE project_id = :p AND part_id IS NULL ORDER BY line_no"
        ),
        {"p": project.id},
    ).all()
    assert any("ix_bom_lines_unmatched" in str(row) for row in plan), plan


def test_a_substitute_needs_a_real_part(db: Session) -> None:
    """Unlike the line itself. An alternate with no `parts` row cannot be
    allocated from, so it is not yet a substitute — it stays in the line's
    `raw_fields_json` until someone creates the part."""
    project = make_project(db)
    line = make_bom_line(db, project)
    db.add(BomLineSubstitute(bom_line_id=line.id, part_id=None))  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_the_same_substitute_cannot_be_listed_twice_for_one_line(db: Session) -> None:
    project = make_project(db)
    line = make_bom_line(db, project)
    part = make_part(db)
    db.add(BomLineSubstitute(bom_line_id=line.id, part_id=part.id))
    db.commit()

    db.add(BomLineSubstitute(bom_line_id=line.id, part_id=part.id, preference=1))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# --------------------------------------------------------------------------
# project_builds: a build is a run, not the project
# --------------------------------------------------------------------------


def test_build_numbers_are_unique_within_a_project_and_free_across_them(db: Session) -> None:
    """Because you build v1 twice: the second run's allocations are not the
    first's, and "build 2" has to name one of them unambiguously."""
    first = make_project(db, name="Board A")
    second = make_project(db, name="Board B")
    make_build(db, first, build_no=1)
    make_build(db, second, build_no=1)
    db.commit()

    with pytest.raises(IntegrityError):
        make_build(db, first, build_no=1, label="oops")
    db.rollback()


def test_deleting_a_project_takes_its_builds_and_lines_with_it(db: Session) -> None:
    """A BOM line has no existence apart from its project. Not lossy: what was
    actually consumed lives in `stock_ledger`, which nothing can delete."""
    project = make_project(db)
    part = make_part(db)
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=10_000)
    build = make_build(db, project)
    make_bom_line(db, project)
    post(db, lot, -1_000, kind=LedgerKind.CONSUME, ref_type="project_build", ref_id=build.id)
    make_allocation(db, build, part, 1_000, AllocationState.CONSUMED, lot=lot)
    db.commit()

    db.delete(project)
    db.commit()

    assert db.execute(text("SELECT COUNT(*) FROM bom_lines")).scalar_one() == 0
    assert db.execute(text("SELECT COUNT(*) FROM project_builds")).scalar_one() == 0
    assert db.execute(text("SELECT COUNT(*) FROM stock_allocations")).scalar_one() == 0
    # The movement itself survives, which is what makes the cascade acceptable.
    assert db.execute(text("SELECT COUNT(*) FROM stock_ledger")).scalar_one() == 1


# --------------------------------------------------------------------------
# stock_allocations: the reserved cache is derived, full stop
# --------------------------------------------------------------------------


def test_only_reserved_allocations_sum_into_the_reserved_cache(db: Session) -> None:
    """The single-equality predicate that makes the cache rebuildable at all.

    `PLANNED` holds no lot, `CONSUMED` has already moved `qty_milli_cached`
    (counting it twice would make available stock read low forever), and
    `RELEASED` gave the hold back — so exactly one state contributes.
    """
    part = make_part(db)
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=100_000)
    build = make_build(db, make_project(db))

    make_allocation(db, build, part, 5_000, AllocationState.PLANNED)
    make_allocation(db, build, part, 7_000, AllocationState.RESERVED, lot=lot)
    make_allocation(db, build, part, 11_000, AllocationState.CONSUMED, lot=lot)
    make_allocation(db, build, part, 13_000, AllocationState.RELEASED, lot=lot)
    db.commit()

    _rebuild(db)

    assert _reserved(db, lot) == 7_000


def test_the_rebuild_reconstructs_the_cache_from_nothing(db: Session) -> None:
    """A cache that cannot be rebuilt from its source is a second source of
    truth. Nothing in this test ever writes the counter — the factory
    deliberately does not — so the value can only have come from the rebuild.
    """
    part = make_part(db)
    location = make_location(db)
    first = make_lot(db, part, location, qty_milli=50_000)
    second = make_lot(db, part, location, qty_milli=50_000)
    build = make_build(db, make_project(db), status=BuildStatus.IN_PROGRESS)

    # One line legitimately satisfied out of two bins.
    make_allocation(db, build, part, 3_000, AllocationState.RESERVED, lot=first)
    make_allocation(db, build, part, 4_000, AllocationState.RESERVED, lot=second)
    # And a second pick from a bin already picked from, which is normal work and
    # is why there is no UNIQUE(build, line, lot).
    make_allocation(db, build, part, 500, AllocationState.RESERVED, lot=first)
    db.commit()

    assert _reserved(db, first) == 0  # nothing has maintained it

    _rebuild(db)

    assert _reserved(db, first) == 3_500
    assert _reserved(db, second) == 4_000


def test_the_rebuild_repairs_a_cache_that_is_too_high(db: Session) -> None:
    """The direction that actually hurts: an over-stated reservation reads as
    missing stock, so a released hold that never decremented the counter would
    hide a part forever. A rebuild that only ever added would not fix it, which
    is why the statement rewrites every row rather than the ones it finds
    allocations for."""
    part = make_part(db)
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=20_000)
    lot.qty_reserved_milli_cached = 999_000
    db.commit()

    _rebuild(db)

    assert _reserved(db, lot) == 0


def test_the_rebuild_uses_the_partial_reservation_index(db: Session) -> None:
    """`CONSUMED` rows accumulate for the lifetime of the install. Without the
    partial index the nightly rebuild gets slower with every build ever done,
    and with it the scan touches only live reservations."""
    plan = db.execute(text(f"EXPLAIN QUERY PLAN {RESERVED_CACHE_REBUILD_SQL}")).all()
    assert any("ix_stock_allocations_reserved_lot" in str(row) for row in plan), plan

    # Checked separately, because the plan looks identical if the index is still
    # there but no longer partial — which is the version that quietly grows
    # without bound. The predicate must also match the rebuild's, or SQLite
    # cannot use the index for it at all.
    sql = db.execute(
        text("SELECT sql FROM sqlite_master WHERE name = 'ix_stock_allocations_reserved_lot'")
    ).scalar_one()
    assert f"state = '{AllocationState.RESERVED.value}'" in sql, sql


def test_a_reserved_allocation_survives_a_lot_it_points_at(db: Session) -> None:
    """`RESTRICT`, so a lot cannot be deleted out from under a reservation —
    which would leave the cache referring to stock that no longer exists and no
    way to notice."""
    part = make_part(db)
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=10_000)
    build = make_build(db, make_project(db))
    make_allocation(db, build, part, 1_000, AllocationState.RESERVED, lot=lot)
    db.commit()

    db.delete(lot)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_a_consumed_allocation_cannot_invent_a_ledger_row(db: Session) -> None:
    """The pick's `stock_ledger.seq` is a real FK, so an allocation can never
    claim a movement that did not happen — and the ledger's append-only triggers
    then make the reference permanent."""
    part = make_part(db)
    build = make_build(db, make_project(db))
    with pytest.raises(IntegrityError):
        make_allocation(
            db, build, part, 1_000, AllocationState.CONSUMED, consumed_ledger_seq=999_999
        )
    db.rollback()


def test_an_unknown_allocation_state_is_refused_on_write(db: Session) -> None:
    """Adding a state must stay a one-line change *and* this build must not
    invent one — a typo'd state would silently stop counting as reserved."""
    part = make_part(db)
    build = make_build(db, make_project(db))
    make_allocation(db, build, part, 1_000)  # a valid row, to prove the setup works
    db.commit()

    with pytest.raises(StatementError, match="not a valid AllocationState"):
        make_allocation(db, build, part, 1_000, state="held")  # type: ignore[arg-type]
    db.rollback()


def test_project_and_build_statuses_are_plain_varchar(db: Session) -> None:
    """No `CHECK`, so adding a status stays additive. `test_schema_invariants`
    asserts the absence globally; this pins the columns by name so a later
    `sa.Enum` here is caught where it is introduced."""
    for table, column in (
        ("projects", "status"),
        ("project_builds", "status"),
        ("stock_allocations", "state"),
    ):
        info = {row[1]: row[2] for row in db.execute(text(f"PRAGMA table_info({table})")).all()}
        assert info[column].upper().startswith("VARCHAR"), f"{table}.{column} is {info[column]}"


def test_quantities_are_integer_milli_columns(db: Session) -> None:
    """Thousandths as INTEGER, so allocation sums stay exact. A REAL column here
    would drift against the ledger it is checked against."""
    lines = {row[1]: row[2] for row in db.execute(text("PRAGMA table_info(bom_lines)")).all()}
    assert lines["qty_per_assembly_milli"].upper() == "INTEGER"

    allocs = {
        row[1]: row[2] for row in db.execute(text("PRAGMA table_info(stock_allocations)")).all()
    }
    assert allocs["qty_milli"].upper() == "INTEGER"


def test_a_project_needs_only_a_name(db: Session) -> None:
    """Same reasoning as `parts`: a project gets created mid-thought, before its
    revision or description exists."""
    project = make_project(db, name="unnamed thing on the bench")
    db.commit()

    assert project.status == ProjectStatus.PLANNING
    assert project.revision is None
