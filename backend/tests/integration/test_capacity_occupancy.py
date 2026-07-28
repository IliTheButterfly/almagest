"""Occupancy caching against a real migrated database: the triggers that
dirty it, and the bulk rebuild that recomputes it.

Runs against the real Alembic migrations (see `tests/conftest.py`), which is
the only reason `trg_locations_seed_occupancy`, `trg_stock_ledger_dirty_occupancy`
and `trg_stock_lots_dirty_occupancy` exist at all — they are invisible to the
models, exactly like `stock_ledger`'s append-only triggers.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.maintenance import mark_location_occupancy_dirty, rebuild_location_occupancy
from app.models.enums import (
    CapacityModel,
    LayoutSuggestionKind,
    LayoutSuggestionStatus,
    LedgerKind,
)
from app.models.layout import LayoutSuggestion
from app.models.stock import StockLot
from app.models.storage import Location, LocationOccupancy
from app.services.tree import location_tree
from tests.factories import (
    make_container_type,
    make_location,
    make_lot,
    make_packaging,
    make_part,
    post,
)


def _occupancy(db: Session, location_id: int) -> LocationOccupancy:
    return db.execute(
        select(LocationOccupancy).where(LocationOccupancy.location_id == location_id)
    ).scalar_one()


def _cabinet(db: Session) -> tuple[Location, Location, Location]:
    """room -> cabinet -> drawer, indexed so ancestor lookups work."""
    tree = location_tree(db)
    room = tree.insert_and_index(Location(name="Room"))
    cabinet = tree.insert_and_index(Location(name="Cabinet", parent_id=room.id))
    drawer = tree.insert_and_index(Location(name="Drawer", parent_id=cabinet.id))
    return room, cabinet, drawer


# ---------------------------------------------------------------------------
# The seeding trigger
# ---------------------------------------------------------------------------


def test_every_new_location_gets_a_fresh_dirty_occupancy_row(db: Session) -> None:
    location = make_location(db, "Fresh bin")
    db.commit()
    row = _occupancy(db, location.id)
    assert row.is_dirty is True
    assert row.capacity_model == CapacityModel.NONE
    assert row.used == 0.0


# ---------------------------------------------------------------------------
# Dirtying propagates to ancestors
# ---------------------------------------------------------------------------


def test_ledger_insert_dirties_the_lots_own_location_and_every_ancestor(db: Session) -> None:
    room, cabinet, drawer = _cabinet(db)
    part = make_part(db)
    lot = make_lot(db, part, drawer)
    db.commit()

    # Clean the slate: every row starts dirty from the seeding trigger.
    rebuild_location_occupancy(db)
    db.commit()
    assert _occupancy(db, drawer.id).is_dirty is False
    assert _occupancy(db, cabinet.id).is_dirty is False
    assert _occupancy(db, room.id).is_dirty is False

    # A plain RECEIVE never sets from/to; only the lot's own location does —
    # this is exactly why the trigger also resolves `stock_lots.location_id`.
    post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()

    assert _occupancy(db, drawer.id).is_dirty is True
    assert _occupancy(db, cabinet.id).is_dirty is True
    assert _occupancy(db, room.id).is_dirty is True


def test_ledger_insert_does_not_dirty_an_unrelated_sibling(db: Session) -> None:
    _room, cabinet, drawer = _cabinet(db)
    other_drawer = location_tree(db).insert_and_index(
        Location(name="Other drawer", parent_id=cabinet.id)
    )
    part = make_part(db)
    lot = make_lot(db, part, drawer)
    db.commit()
    rebuild_location_occupancy(db)
    db.commit()

    post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()

    assert _occupancy(db, other_drawer.id).is_dirty is False


def test_lot_relocation_dirties_ancestors_of_both_old_and_new_location(db: Session) -> None:
    room, cabinet, drawer = _cabinet(db)
    shelf = location_tree(db).insert_and_index(Location(name="Shelf", parent_id=room.id))
    part = make_part(db)
    lot = make_lot(db, part, drawer)
    db.commit()
    rebuild_location_occupancy(db)
    db.commit()
    assert _occupancy(db, shelf.id).is_dirty is False

    lot.location_id = shelf.id
    db.commit()

    assert _occupancy(db, drawer.id).is_dirty is True  # old location
    assert _occupancy(db, shelf.id).is_dirty is True  # new location
    assert _occupancy(db, cabinet.id).is_dirty is True  # ancestor of the old
    assert _occupancy(db, room.id).is_dirty is True  # ancestor of both


def test_relocating_to_the_same_location_does_not_fire_the_trigger(db: Session) -> None:
    """`WHEN OLD.location_id IS NOT NEW.location_id` — a no-op write must not
    manufacture dirty work."""
    _, _, drawer = _cabinet(db)
    part = make_part(db)
    lot = make_lot(db, part, drawer)
    db.commit()
    rebuild_location_occupancy(db)
    db.commit()

    lot.location_id = drawer.id  # unchanged
    db.commit()

    assert _occupancy(db, drawer.id).is_dirty is False


def test_mark_location_occupancy_dirty_python_helper_covers_ancestors_too(db: Session) -> None:
    """The direct Python equivalent of the triggers, for any future write path
    that mutates state without going through the ledger."""
    room, cabinet, drawer = _cabinet(db)
    db.commit()
    rebuild_location_occupancy(db)
    db.commit()

    mark_location_occupancy_dirty(db, [drawer.id])
    db.commit()

    assert _occupancy(db, drawer.id).is_dirty is True
    assert _occupancy(db, cabinet.id).is_dirty is True
    assert _occupancy(db, room.id).is_dirty is True


# ---------------------------------------------------------------------------
# Rebuild: correctness and idempotence
# ---------------------------------------------------------------------------


def test_rebuild_computes_the_right_snapshot_for_a_slots_container(db: Session) -> None:
    ct = make_container_type(
        db, "assortment-box", capacity_model=CapacityModel.SLOTS, capacity_slots=4
    )
    location = make_location(db, "Box", container_type_id=ct.id)
    part_a = make_part(db, "Part A")
    part_b = make_part(db, "Part B")
    make_lot(db, part_a, location)
    make_lot(db, part_b, location)
    db.commit()

    rebuild_location_occupancy(db)
    db.commit()

    row = _occupancy(db, location.id)
    assert row.capacity_model == CapacityModel.SLOTS
    assert row.capacity == 4.0
    assert row.used == 2.0
    assert row.is_dirty is False
    assert row.computed_at is not None


def test_rebuild_is_idempotent(db: Session) -> None:
    ct = make_container_type(
        db,
        "reel-rack",
        capacity_model=CapacityModel.POSITIONS,
        capacity_slots=10,
        inner_width_mm=100.0,
    )
    location = make_location(db, "Rack", container_type_id=ct.id)
    # "reel" is already seeded by the core migration's reference packagings;
    # a distinct code avoids colliding with it.
    packaging = make_packaging(db, "custom-reel", package_volume_mm3=1_200_000.0, pitch_mm=14.0)
    part = make_part(db)
    lot = make_lot(db, part, location)
    lot.packaging_id = packaging.id
    db.commit()

    first_pass = rebuild_location_occupancy(db)
    db.commit()
    snapshot_1 = {
        row.location_id: (row.capacity, row.used, row.fill_ratio, row.is_full)
        for row in db.execute(select(LocationOccupancy)).scalars()
    }

    second_pass = rebuild_location_occupancy(db)
    db.commit()
    snapshot_2 = {
        row.location_id: (row.capacity, row.used, row.fill_ratio, row.is_full)
        for row in db.execute(select(LocationOccupancy)).scalars()
    }

    assert first_pass == second_pass  # same number of rows touched both times
    assert snapshot_1 == snapshot_2


def test_rebuild_only_dirty_skips_clean_rows(db: Session) -> None:
    # `mark_location_occupancy_dirty`'s ancestor lookup reads `id_path`, which
    # only `TreeRepository` populates — a location inserted without it (as
    # `make_location` does) has an empty `id_path` and resolves to *no*
    # ancestors at all, itself included. Real write paths always go through
    # `TreeRepository`, so these are indexed the same way.
    tree = location_tree(db)
    location_1 = tree.insert_and_index(Location(name="Bin 1"))
    location_2 = tree.insert_and_index(Location(name="Bin 2"))
    db.commit()

    rebuild_location_occupancy(db)  # clean both
    db.commit()
    mark_location_occupancy_dirty(db, [location_1.id])
    db.commit()

    touched = rebuild_location_occupancy(db, only_dirty=True)
    db.commit()

    assert touched == 1
    assert _occupancy(db, location_1.id).is_dirty is False
    assert _occupancy(db, location_2.id).is_dirty is False  # untouched, already clean


def test_full_rebuild_covers_every_location_regardless_of_dirty_state(db: Session) -> None:
    make_location(db, "Bin 1")
    make_location(db, "Bin 2")
    db.commit()
    rebuild_location_occupancy(db)
    db.commit()

    touched = rebuild_location_occupancy(db)  # only_dirty defaults to False
    db.commit()

    assert touched >= 2


# ---------------------------------------------------------------------------
# `overfull` flagging and the suggestion it generates
# ---------------------------------------------------------------------------


def _make_overfull_location(db: Session) -> Location:
    ct = make_container_type(
        db,
        "tiny-tray",
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=1.0,
        inner_width_mm=1.0,
        inner_height_mm=1.0,
        default_fill_factor=1.0,
    )
    location = make_location(db, "Tiny tray", container_type_id=ct.id)
    part = make_part(db)
    part.unit_volume_mm3 = 10.0  # 10mm^3 unit, tray capacity is 1mm^3
    db.flush()
    lot = make_lot(db, part, location)
    post(db, lot, 1000, LedgerKind.RECEIVE)  # one unit = 10mm^3 used
    db.commit()
    return location


def test_overfull_location_is_flagged_and_gets_a_suggestion(db: Session) -> None:
    location = _make_overfull_location(db)

    rebuild_location_occupancy(db)
    db.commit()
    db.refresh(location)

    assert location.is_overfull is True
    suggestion = db.execute(
        select(LayoutSuggestion).where(
            LayoutSuggestion.location_id == location.id,
            LayoutSuggestion.kind == LayoutSuggestionKind.OVERFULL,
        )
    ).scalar_one()
    assert suggestion.status == LayoutSuggestionStatus.PENDING
    assert suggestion.move_plan_json is not None


def test_resolving_overfull_clears_the_flag_and_the_suggestion(db: Session) -> None:
    location = _make_overfull_location(db)
    rebuild_location_occupancy(db)
    db.commit()

    elsewhere = make_location(db, "Elsewhere")
    stock_lot = db.execute(select(StockLot).where(StockLot.location_id == location.id)).scalar_one()
    stock_lot.location_id = elsewhere.id
    db.commit()

    rebuild_location_occupancy(db)
    db.commit()
    db.refresh(location)

    assert location.is_overfull is False
    remaining = db.execute(
        select(LayoutSuggestion).where(
            LayoutSuggestion.location_id == location.id,
            LayoutSuggestion.kind == LayoutSuggestionKind.OVERFULL,
        )
    ).scalar_one_or_none()
    assert remaining is None


def test_dismissed_overfull_suggestion_is_not_resurrected_by_the_next_rebuild(
    db: Session,
) -> None:
    location = _make_overfull_location(db)
    rebuild_location_occupancy(db)
    db.commit()

    suggestion = db.execute(
        select(LayoutSuggestion).where(
            LayoutSuggestion.location_id == location.id,
            LayoutSuggestion.kind == LayoutSuggestionKind.OVERFULL,
        )
    ).scalar_one()
    suggestion.status = LayoutSuggestionStatus.DISMISSED
    db.commit()

    # Still overfull; rebuild again.
    rebuild_location_occupancy(db)
    db.commit()

    rows = (
        db.execute(
            select(LayoutSuggestion).where(
                LayoutSuggestion.location_id == location.id,
                LayoutSuggestion.kind == LayoutSuggestionKind.OVERFULL,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == LayoutSuggestionStatus.DISMISSED
