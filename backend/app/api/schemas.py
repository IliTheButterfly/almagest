"""Wire types shared by more than one route module.

Kept small on purpose — a route's request and response models belong next to the
route. What lands here is the handful of shapes that would otherwise be defined
twice and drift: a lot looks the same whether it was reached by moving stock, by
reading a part, or by reading a bin, and three spellings of that would mean three
different answers to "how much is in there".
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.stock import StockLot
from app.models.storage import Location


class ReplayableResponse(BaseModel):
    """Base for every write response that `app.api.idempotency` can store.

    `replayed` is the only difference a client can observe between having the
    work done and being handed the answer from an earlier identical request —
    and it has to be observable, because "your take was recorded just now" and
    "your take was already recorded a minute ago" are otherwise identical
    responses, and a UI that cannot tell them apart will offer an undo for a
    movement whose undo window closed long ago.
    """

    replayed: bool = Field(
        default=False,
        description=(
            "True when this is the stored response of an earlier request carrying "
            "the same client_op_id; no new movement was recorded."
        ),
    )


class LotRead(BaseModel):
    """One physical package of one part at one location."""

    id: int
    part_id: int
    location_id: int
    #: Read from `stock_lots.qty_milli_cached`. **Never** a `SUM(delta_milli)`
    #: over the ledger — that is the query that stops being sub-second somewhere
    #: around 200k rows, and it is on every screen.
    qty_milli: int
    qty_reserved_milli: int
    status: str
    packaging_id: int | None = None
    batch_code: str | None = None
    serial: str | None = None
    date_code: str | None = None
    unit_cost_micro: int | None = None
    currency: str | None = None
    #: Derived here and now from the location tree, never stored on the lot and
    #: never read off a tag: a container that moves would make an encoded path a
    #: lie the moment the drawer changed cabinet.
    location_label_path: str | None = None


def lot_read(session: Session, lot: StockLot) -> LotRead:
    """Render a lot for the wire, resolving its location path.

    `Session.get` is an identity-map lookup, so rendering every lot in one bin
    costs one query for the location, not one per lot.
    """
    location = session.get(Location, lot.location_id)
    return LotRead(
        id=lot.id,
        part_id=lot.part_id,
        location_id=lot.location_id,
        qty_milli=lot.qty_milli_cached,
        qty_reserved_milli=lot.qty_reserved_milli_cached,
        status=lot.status,
        packaging_id=lot.packaging_id,
        batch_code=lot.batch_code,
        serial=lot.serial,
        date_code=lot.date_code,
        unit_cost_micro=lot.unit_cost_micro,
        currency=lot.currency,
        location_label_path=location.label_path if location is not None else None,
    )
