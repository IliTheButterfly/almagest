"""What `seed_demo` puts on the screen.

Tested because the seed is the only thing standing between a fresh install and
an empty UI, and because **its failure mode is silence.** The first version of
the storage seed asked for a drawer named "Drawer 3" and a slot named "Slot 1",
found neither, skipped both, and printed `seeded: ... 32 locations, 0 lots` — a
success line for a run that seeded no stock and none of the nesting it exists to
demonstrate. Nothing in the app was broken, so nothing else would ever have
noticed.

So the assertions here are about *visible structure*, not row counts: a grid to
draw, a container inside a container, and more than one lot in a bin.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.stock import StockLedger, StockLot
from app.models.storage import ContainerType, Location
from app.scripts.seed_demo import seed_all


def _seeded(db: Session) -> Session:
    seed_all(db)
    db.commit()
    return db


def test_the_storage_tree_has_something_to_draw(db: Session) -> None:
    """Before this seed a fresh database held exactly one location, `INBOX`, so
    the storage screen rendered an empty tree."""
    _seeded(db)
    locations = list(db.execute(select(Location)).scalars())

    assert len(locations) > 30
    assert {"INBOX", "Workshop", "Workbench cabinet"} <= {row.name for row in locations}


def test_the_cabinet_materialised_its_own_slots(db: Session) -> None:
    """Instances own concrete rows, never a live link back to the type."""
    _seeded(db)
    cabinet = db.execute(select(Location).where(Location.name == "Workbench cabinet")).scalar_one()

    slots = list(db.execute(select(Location).where(Location.parent_id == cabinet.id)).scalars())
    assert len(slots) == 30
    # Zero-padded, so they sort correctly and read correctly on a printed card.
    assert [row.slot_label for row in slots[:3]] == ["01", "02", "03"]


def test_a_container_sits_inside_a_container(db: Session) -> None:
    """ADR 0002's recursion, which nothing demonstrates until something nests.

    A Gridfinity baseplate occupies one drawer's footprint while presenting its
    own grid to the bins above it — two independent questions, which is the whole
    reason container types recurse.
    """
    _seeded(db)
    tray = db.execute(select(Location).where(Location.name == "Gridfinity tray")).scalar_one()

    assert tray.depth == 3, tray.label_path
    assert tray.label_path.startswith("Workshop / Workbench cabinet / 03")

    cells = list(db.execute(select(Location).where(Location.parent_id == tray.id)).scalars())
    assert len(cells) == 24  # a 4x6 baseplate
    assert all(cell.depth == 4 for cell in cells)


def test_the_nested_tray_uses_the_grid_units_capacity_model(db: Session) -> None:
    """The capacity model that measures *area*, not compartments — so a 2x1 bin
    consumes two units. It is the one that would look fine while being wrong."""
    _seeded(db)
    tray = db.execute(select(Location).where(Location.name == "Gridfinity tray")).scalar_one()

    assert tray.container_type_id is not None
    container_type = db.get(ContainerType, tray.container_type_id)
    assert container_type is not None
    assert container_type.capacity_model == "grid_units"


def test_stock_lands_in_drawers_with_more_than_one_lot_in_a_bin(db: Session) -> None:
    """Quantity lives on the lot, never on the part, so a single-lot-per-bin seed
    would let a screen that assumed otherwise look correct."""
    _seeded(db)
    lots = list(db.execute(select(StockLot)).scalars())
    assert len(lots) == 5

    per_location: dict[int, int] = {}
    for lot in lots:
        per_location[lot.location_id] = per_location.get(lot.location_id, 0) + 1
    assert max(per_location.values()) >= 2


def test_quantities_were_written_through_the_ledger(db: Session) -> None:
    """Not by setting `qty_milli_cached` directly, which would seed cache drift
    on day one and make the nightly reconciliation job report a bug that is
    actually in the seed."""
    _seeded(db)
    for lot in db.execute(select(StockLot)).scalars():
        summed = db.execute(
            select(func.coalesce(func.sum(StockLedger.delta_milli), 0)).where(
                StockLedger.lot_id == lot.id
            )
        ).scalar_one()
        assert lot.qty_milli_cached == summed
        assert lot.qty_milli_cached > 0


def test_re_seeding_changes_nothing(db: Session) -> None:
    """Documented as idempotent, and it is run against existing databases."""
    first = seed_all(db)
    db.commit()
    assert first.locations > 0 and first.lots > 0

    second = seed_all(db)
    db.commit()

    assert (second.locations, second.lots, second.parts, second.categories) == (0, 0, 0, 0)
    assert db.execute(select(func.count()).select_from(StockLot)).scalar_one() == 5
