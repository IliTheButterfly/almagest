"""Minimal object factories for tests.

Deliberately thin — just enough to satisfy NOT NULL columns so a test can say
what it is actually about. Anything cleverer becomes a second, untested model
layer.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part, PartKind
from app.models.enums import LedgerKind, LedgerSource
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location


def component_kind(db: Session) -> PartKind:
    """The 'component' row seeded by the initial migration."""
    return db.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()


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
