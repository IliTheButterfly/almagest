"""How an entity is rendered once something has resolved to it.

One shared ID space means a scan resolves *without knowing what it scanned*, so
every consumer of a resolution faces the same question — what do I call this
row? — and there must be one answer. `/api/resolve/{short_id}` and
`/api/scan/resolve` both come through here, otherwise the same drawer would
label itself differently depending on which endpoint found it.

`label_path` is always computed here and **never** read from a tag or a printed
payload. A container that moves would make an encoded path a lie the moment the
drawer changed cabinet.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import EntityType
from app.models.identity import ObjectId
from app.models.stock import StockLot
from app.models.storage import Location
from app.services import shortid


@dataclass(frozen=True)
class EntityDescription:
    entity_type: str
    entity_pk: int
    #: Human-readable identity: a location's name, a part's MPN or name.
    label: str
    #: Full derived path for anything that sits in the location tree.
    label_path: str | None = None
    #: The printed identifier, when this row has one. `None` is normal, not an
    #: error — nobody labels all 96 cells of an assortment box.
    short_id: str | None = None
    #: `BIN 4K7T-92M8`. The type prefix is cosmetic and never parsed back.
    display: str | None = None


def describe(
    session: Session,
    entity_type: str,
    entity_pk: int,
    *,
    short_id: str | None = None,
) -> EntityDescription:
    """Render one entity. `short_id` is passed in when the caller already has it
    (a short-ID scan), and looked up otherwise."""
    label = f"{entity_type} {entity_pk}"
    label_path: str | None = None

    if entity_type == EntityType.LOCATION:
        location = session.get(Location, entity_pk)
        if location is not None:
            label = location.name
            label_path = location.label_path
    elif entity_type == EntityType.PART:
        part = session.get(Part, entity_pk)
        if part is not None:
            label = part.mpn or part.name
    elif entity_type == EntityType.STOCK_LOT:
        lot = session.get(StockLot, entity_pk)
        if lot is not None:
            part = session.get(Part, lot.part_id)
            if part is not None:
                label = part.mpn or part.name
            # A lot's useful "path" is where the lot physically is, which is why
            # this is derived from `location_id` rather than stored: a whole-lot
            # move rewrites that column and this answer must follow it.
            location = session.get(Location, lot.location_id)
            if location is not None:
                label_path = location.label_path

    if short_id is None:
        short_id = _printed_short_id(session, entity_type, entity_pk)

    return EntityDescription(
        entity_type=entity_type,
        entity_pk=entity_pk,
        label=label,
        label_path=label_path,
        short_id=short_id,
        display=shortid.format_display(short_id, entity_type) if short_id else None,
    )


def _printed_short_id(session: Session, entity_type: str, entity_pk: int) -> str | None:
    """The ID actually on the label, when there is one.

    An object may accumulate several IDs — a relabelled bin, a legacy code kept
    resolvable — so this asks for the primary one and falls back to any, which
    keeps a row whose `is_primary` bookkeeping went wrong displayable rather
    than anonymous.
    """
    rows = list(
        session.execute(
            select(ObjectId)
            .where(ObjectId.entity_type == entity_type, ObjectId.entity_pk == entity_pk)
            .order_by(ObjectId.is_primary.desc(), ObjectId.created_at)
        ).scalars()
    )
    return rows[0].short_id if rows else None
