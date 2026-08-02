"""`/api/intake/pending` — the fast path's queue, on the server.

`PLAN.md`: *"the fast path is the point"*. One tap at RESOLVING parks a label and
returns straight to scanning, so a box of reels is scanned in under a minute and
curated at a desk afterwards — the countermeasure to the thing that actually
kills projects in this space, intake that costs a form per item.

**Why this exists at all.** The queue lived in the phone's `localStorage`. A
queue built standing at the shelf therefore could not be walked at the desktop,
and clearing browser storage lost it with no trace. For a queue whose whole
purpose is deferring work, "you will lose it if you defer too long" is a
contradiction.

Three decisions worth stating:

* **`client_op_id` is the identity**, not the server's `id`. It is minted on the
  device at scan time, before this row exists, so a phone that posted and lost
  the response can retry and get the same row rather than a duplicate — which is
  the normal case, not the edge case, for something used one-handed at a shelf
  with bad wifi. Re-posting a whole queue is therefore safe.
* **Nothing here touches the ledger.** Parking a scan records an intention, not
  stock. The desk pass is what commits, through the ordinary movement routes.
* **Resolving does not re-resolve.** The stored `decoded_kind` and `mpn` are what
  the scan looked like *then*; an alias taught or a parser added in the meantime
  may do better, so the desk pass re-runs the chain rather than trusting these.
  They are display and mining data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.api.limits import QTY_MILLI_MAX, ResultOffset, RowId
from app.db.session import get_db
from app.models.captures import Capture
from app.models.catalog import Part
from app.models.enums import PendingIntakeStatus, ScanDecodedKind
from app.models.scanning import PendingIntake, ScanEvent
from app.models.types import utcnow

router = APIRouter(prefix="/api/intake/pending", tags=["intake"])

#: A parked payload is not a bare code — an ECIA label is a few hundred bytes of
#: separated fields. Bounded anyway, because this column is written straight from
#: a request body and an unbounded Text field is a free upload endpoint.
_PAYLOAD_MAX = 4096


class PendingIntakeIn(BaseModel):
    """One parked scan, as the device recorded it."""

    #: Minted on the device at scan time. **This is the entry's identity**, so
    #: re-posting it is a no-op that returns the existing row rather than an
    #: error — a synced queue must be safely re-sendable.
    client_op_id: str = Field(min_length=1, max_length=36)
    #: Verbatim, control characters included. The bytes are the asset: a vendor
    #: format nobody parses yet is a parser waiting to be written, and stripping
    #: the GS/RS separators is the step that would make it unmineable.
    raw_payload: str = Field(min_length=1, max_length=_PAYLOAD_MAX)
    symbology: str | None = Field(default=None, max_length=32)
    decoded_kind: ScanDecodedKind | None = None
    scan_event_id: RowId | None = None
    #: The still taken alongside this scan, when one was. What makes deferring
    #: honest: the desk pass happens hours later at a machine with no reel in
    #: front of it, and everything the barcode did not encode — the printed
    #: manufacturer, a hand-written count — is otherwise gone by then.
    capture_id: RowId | None = None

    mpn: str | None = Field(default=None, max_length=128)
    manufacturer: str | None = Field(default=None, max_length=128)
    supplier_part_number: str | None = Field(default=None, max_length=128)
    date_code: str | None = Field(default=None, max_length=32)
    lot_code: str | None = Field(default=None, max_length=128)
    quantity_milli: int | None = Field(default=None, gt=0, le=QTY_MILLI_MAX)

    #: What the resolver matched at scan time — a hint carried forward, never a
    #: decision. `resolved_part_id` is the decision, and only the desk pass sets it.
    part_id: RowId | None = None
    note: str | None = Field(default=None, max_length=2000)
    device_id: str | None = Field(default=None, max_length=64)
    #: When the device recorded the scan, which is not when this row was created:
    #: an offline batch syncs minutes later. Stored because it is what the user
    #: experienced; **not** used for ordering, because a device clock can be
    #: wrong by years and a worklist a bad clock can scramble is untrustworthy.
    queued_at: datetime | None = None


class PendingIntakeRead(BaseModel):
    id: RowId
    client_op_id: str
    raw_payload: str
    symbology: str | None
    decoded_kind: str | None
    scan_event_id: int | None
    capture_id: int | None
    mpn: str | None
    manufacturer: str | None
    supplier_part_number: str | None
    date_code: str | None
    lot_code: str | None
    quantity_milli: int | None
    part_id: int | None
    resolved_part_id: int | None
    note: str | None
    device_id: str | None
    status: str
    queued_at: datetime | None
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class PendingIntakeCreated(BaseModel):
    entry: PendingIntakeRead
    #: True when this `client_op_id` was already stored. Observable on purpose:
    #: a sync that cannot tell "parked" from "already parked" either shows a
    #: false success or double-counts what it uploaded.
    already_queued: bool


class PendingIntakeList(BaseModel):
    #: Matching the status filter, ignoring pagination.
    total: int
    #: Still pending regardless of the filter, so a badge can be rendered from
    #: any listing rather than needing a second request for the count.
    pending_total: int
    entries: list[PendingIntakeRead]


class ResolveRequest(BaseModel):
    """Mark an entry dealt with, optionally naming what it became."""

    resolved_part_id: RowId | None = None
    note: str | None = Field(default=None, max_length=2000)


def _require(db: Session, entry_id: int) -> PendingIntake:
    entry = db.get(PendingIntake, entry_id)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "message": f"no pending intake with id {entry_id}"},
        )
    return entry


@router.post("", response_model=PendingIntakeCreated, status_code=status.HTTP_201_CREATED)
def park_scan(request: PendingIntakeIn, db: Session = Depends(get_db)) -> PendingIntakeCreated:
    """Park a scan for later. One tap, no further screens.

    Idempotent on `client_op_id` by an explicit lookup rather than by catching the
    UNIQUE violation, so the caller learns *which* case happened. Note this route
    does **not** use `app.api.idempotency`: that helper stores and replays a whole
    response body keyed on an operation id, which is exactly right for a ledger
    write that must not be repeated — but here the row itself is the record, and
    the natural key is already on it. Two mechanisms for one guarantee would just
    be two things to keep in step.
    """
    existing = db.execute(
        select(PendingIntake).where(PendingIntake.client_op_id == request.client_op_id)
    ).scalar_one_or_none()
    if existing is not None:
        return PendingIntakeCreated(
            entry=PendingIntakeRead.model_validate(existing), already_queued=True
        )

    if request.scan_event_id is not None and db.get(ScanEvent, request.scan_event_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "unknown_scan_event",
                "message": f"no scan event with id {request.scan_event_id}",
            },
        )
    if request.capture_id is not None and db.get(Capture, request.capture_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "unknown_capture",
                "message": f"no capture with id {request.capture_id}",
            },
        )
    if request.part_id is not None and db.get(Part, request.part_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "unknown_part", "message": f"no part with id {request.part_id}"},
        )

    entry = PendingIntake(
        client_op_id=request.client_op_id,
        raw_payload=request.raw_payload,
        symbology=request.symbology,
        decoded_kind=request.decoded_kind,
        scan_event_id=request.scan_event_id,
        capture_id=request.capture_id,
        mpn=request.mpn,
        manufacturer=request.manufacturer,
        supplier_part_number=request.supplier_part_number,
        date_code=request.date_code,
        lot_code=request.lot_code,
        quantity_milli=request.quantity_milli,
        part_id=request.part_id,
        note=request.note,
        device_id=request.device_id,
        queued_at=request.queued_at,
        status=PendingIntakeStatus.PENDING,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return PendingIntakeCreated(entry=PendingIntakeRead.model_validate(entry), already_queued=False)


@router.get("", response_model=PendingIntakeList)
def list_pending(
    db: Session = Depends(get_db),
    # Repeatable rather than a single value with an "any" sentinel. A querystring
    # cannot carry null, so `status | None` would have had no way to mean "all"
    # from a browser — and the two things a client actually wants are the
    # worklist (`?status=pending`, the default) and the history (all three
    # spelled out). A repeatable param gives both with no magic value, and stays
    # correct when a fourth status is added.
    status_filter: list[PendingIntakeStatus] = Query(
        default=[PendingIntakeStatus.PENDING],
        alias="status",
        description=(
            "Repeatable. Defaults to the worklist; pass every value to include "
            "resolved and dismissed entries as history."
        ),
    ),
    device_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: Annotated[ResultOffset, Query()] = 0,
) -> PendingIntakeList:
    """The worklist, **oldest first** — a box of reels is walked in scan order.

    Ordered by `id`, not `queued_at`: `id` is server-assigned and monotonic, and a
    client syncing a batch posts it in scan order, so this is both the order the
    user experienced and immune to a device clock that is wrong.
    """
    # Annotated, because `in_()` yields a `ColumnElement[bool]` while `==` yields
    # a `BinaryExpression[bool]`, and an unannotated list infers the narrower
    # type from whichever append comes first.
    where: list[ColumnElement[bool]] = []
    if status_filter:
        where.append(PendingIntake.status.in_(status_filter))
    if device_id is not None:
        where.append(PendingIntake.device_id == device_id)

    total = int(
        db.execute(select(func.count()).select_from(PendingIntake).where(*where)).scalar_one()
    )
    pending_total = int(
        db.execute(
            select(func.count())
            .select_from(PendingIntake)
            .where(PendingIntake.status == PendingIntakeStatus.PENDING)
        ).scalar_one()
    )
    entries = list(
        db.execute(
            select(PendingIntake)
            .where(*where)
            .order_by(PendingIntake.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return PendingIntakeList(
        total=total,
        pending_total=pending_total,
        entries=[PendingIntakeRead.model_validate(entry) for entry in entries],
    )


@router.post("/{entry_id}/resolve", response_model=PendingIntakeRead)
def resolve_entry(
    entry_id: RowId, request: ResolveRequest, db: Session = Depends(get_db)
) -> PendingIntakeRead:
    """Mark an entry dealt with, optionally naming the part it became.

    Records the outcome; it does not perform it. Creating the part and receiving
    the stock go through the ordinary routes, so there is one code path that
    writes the ledger and this is not it.
    """
    entry = _require(db, entry_id)
    if request.resolved_part_id is not None and db.get(Part, request.resolved_part_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "unknown_part",
                "message": f"no part with id {request.resolved_part_id}",
            },
        )

    entry.status = PendingIntakeStatus.RESOLVED
    entry.resolved_part_id = request.resolved_part_id
    entry.resolved_at = utcnow()
    if request.note is not None:
        entry.note = request.note
    db.commit()
    db.refresh(entry)
    return PendingIntakeRead.model_validate(entry)


@router.post("/{entry_id}/dismiss", response_model=PendingIntakeRead)
def dismiss_entry(
    entry_id: RowId, request: ResolveRequest, db: Session = Depends(get_db)
) -> PendingIntakeRead:
    """Not a real intake: a duplicate scan, a shipping label, someone else's box.

    Kept rather than deleted, and kept *distinguishable* from resolved, because
    the two say opposite things about whether the payload is worth mining — a
    pile of dismissed unknowns is noise, a pile of resolved ones is a parser
    worth writing.
    """
    entry = _require(db, entry_id)
    entry.status = PendingIntakeStatus.DISMISSED
    entry.resolved_at = utcnow()
    if request.note is not None:
        entry.note = request.note
    db.commit()
    db.refresh(entry)
    return PendingIntakeRead.model_validate(entry)


@router.post("/{entry_id}/reopen", response_model=PendingIntakeRead)
def reopen_entry(entry_id: RowId, db: Session = Depends(get_db)) -> PendingIntakeRead:
    """Put a resolved or dismissed entry back on the worklist.

    Exists because the desk pass is where mistakes happen — dismissing the wrong
    row is one tap — and nothing here is historical record, so undo is a status
    change rather than a compensating row. That is the difference between this
    table and `stock_ledger`, and it is why this one has no triggers.
    """
    entry = _require(db, entry_id)
    entry.status = PendingIntakeStatus.PENDING
    entry.resolved_at = None
    entry.resolved_part_id = None
    db.commit()
    db.refresh(entry)
    return PendingIntakeRead.model_validate(entry)
