"""Minimal object factories for tests.

Deliberately thin — just enough to satisfy NOT NULL columns so a test can say
what it is actually about. Anything cleverer becomes a second, untested model
layer.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Packaging, Part, PartCategory, PartKind
from app.models.enums import LedgerKind, LedgerSource
from app.models.stock import StockLedger, StockLot
from app.models.storage import ContainerType, Location


def component_kind(db: Session) -> PartKind:
    """The 'component' row seeded by the initial migration."""
    return db.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()


def inbox_location(db: Session) -> Location:
    """The permanent staging row seeded by the capacity/assignment migration."""
    return db.execute(select(Location).where(Location.is_staging.is_(True))).scalars().one()


def make_part(db: Session, name: str = "Test part", **kwargs: object) -> Part:
    part = Part(name=name, part_kind_id=component_kind(db).id, **kwargs)
    db.add(part)
    db.flush()
    return part


def make_location(db: Session, name: str = "Test bin", **kwargs: object) -> Location:
    location = Location(name=name, **kwargs)
    db.add(location)
    db.flush()
    return location


def make_container_type(
    db: Session, slug: str = "test-container", **kwargs: object
) -> ContainerType:
    kwargs.setdefault("display_name", slug)
    container_type = ContainerType(slug=slug, **kwargs)
    db.add(container_type)
    db.flush()
    return container_type


def make_packaging(db: Session, code: str = "test-packaging", **kwargs: object) -> Packaging:
    kwargs.setdefault("display_name", code)
    packaging = Packaging(code=code, **kwargs)
    db.add(packaging)
    db.flush()
    return packaging


def make_category(db: Session, name: str = "Test category", **kwargs: object) -> PartCategory:
    kwargs.setdefault("slug", name.lower().replace(" ", "-"))
    category = PartCategory(name=name, **kwargs)
    db.add(category)
    db.flush()
    return category


def make_lot(db: Session, part: Part, location: Location, qty_milli: int = 0) -> StockLot:
    lot = StockLot(part_id=part.id, location_id=location.id, qty_milli_cached=qty_milli)
    db.add(lot)
    db.flush()
    return lot


def post(
    db: Session,
    lot: StockLot,
    delta_milli: int,
    kind: LedgerKind = LedgerKind.ADJUST,
    **kwargs: object,
) -> StockLedger:
    """Append a ledger row and move the cached balance with it.

    This mirrors what a real write path must do — ledger row and cache updated
    together in one transaction. Tests that deliberately break the pairing do
    so explicitly, so drift detection has something to detect.
    """
    lot.qty_milli_cached += delta_milli
    row = StockLedger(
        lot_id=lot.id,
        part_id=lot.part_id,
        kind=kind,
        delta_milli=delta_milli,
        qty_after_milli=lot.qty_milli_cached,
        source=LedgerSource.MANUAL,
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row
