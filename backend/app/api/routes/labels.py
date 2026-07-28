"""`/api/labels` — multi-up label sheets, matched to the physical grid.

One write (`POST .../sheets`) and one read (`GET .../sheets/{id}`). The write
renders synchronously — a card is a handful of PIL draw calls and a QR, not a
network round trip, so there is no "pending" state worth a job-status poll —
and records exactly what it drew, both as a `label_sheet_jobs` row (the whole
sheet, including its grid geometry) and one `label_prints` row per card (per
`docs/PLAN.md`, "so a reprint matches the original").

**Nothing here accepts a name, a path, or a short id from the caller.**
`LabelSheetRequest` names only *which locations* to print (`root_location_id`,
optionally narrowed by `slot_ids`) — every word that ends up on a card is
re-derived inside `app.services.labels.resolve_label_fields` from whatever
those locations currently say. That is the whole mechanism behind "a stale
label is impossible": there is no field here a stale value could travel
through.

`GET .../sheets/{id}` is a pure read of a past job's frozen record, not a
re-render — a reprint from the *current* tree is a fresh `POST`, and the two
are different operations for the same reason `label_prints` exists at all:
"what got printed" has to survive whatever the location is called now.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import LabelDpi, RowId
from app.api.schemas import ReplayableResponse
from app.db.session import get_db
from app.models.enums import LabelBackendKind, LabelTemplate
from app.models.layout_authoring import LabelSheetJob
from app.models.storage import Location
from app.services import labels
from app.services.labels import CardPlacement, LabelError, RenderedSheet

router = APIRouter(prefix="/api/labels", tags=["labels"])


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class LabelSheetRequest(BaseModel):
    template: LabelTemplate
    #: The cabinet (`drawer_card`, every slotted child) or the single
    #: container (`cabinet_card`, just this one) the sheet is generated from.
    root_location_id: RowId
    #: `drawer_card` only. A subset of `root_location_id`'s slots, so one
    #: replacement card can be positioned on a partly-used sheet — it still
    #: lands at its own `row_idx`/`col_idx`, never repacked to the top-left.
    slot_ids: list[RowId] | None = Field(default=None, min_length=1, max_length=500)
    backend: LabelBackendKind = LabelBackendKind.PDF_SHEET
    dpi: LabelDpi = 300
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class LabelCardPlacementRead(BaseModel):
    location_id: RowId
    #: 0-based position in the sheet's row-major grid, in base cells.
    row: int
    col: int
    row_span: int
    col_span: int
    slot_label: str | None
    name: str
    short_id: str | None
    qr_included: bool
    width_mm: float
    height_mm: float


class LabelSheetJobRead(BaseModel):
    id: RowId
    template: str
    backend: str
    dpi: int
    root_location_id: RowId
    #: The base 1x1 cell size the whole sheet's pitch is measured against —
    #: not any individual card's rendered size, which scales with its span.
    card_width_mm: float
    card_height_mm: float
    item_count: int
    created_at: datetime
    cards: list[LabelCardPlacementRead]


class LabelSheetCreated(ReplayableResponse):
    job: LabelSheetJobRead


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


#: Every reason `LabelError` raises today is the request asking for something
#: that cannot exist (an unpriced drawer type, a slot outside this cabinet, a
#: template/filter combination with no meaning) — a 422, matching
#: `app.services.layout_authoring.LayoutError`'s default. None of them are
#: state conflicts, so unlike `app.services.provisioning`'s error map there is
#: currently no reason that belongs at 409.
def _label_error(error: LabelError) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"reason": error.reason, "message": str(error)},
    )


def _require_location(db: Session, location_id: RowId) -> Location:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_location", "message": f"no location with id {location_id}"},
        )
    return location


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _placement_read(placement: CardPlacement) -> LabelCardPlacementRead:
    return LabelCardPlacementRead(
        location_id=placement.location_id,
        row=placement.row,
        col=placement.col,
        row_span=placement.row_span,
        col_span=placement.col_span,
        slot_label=placement.slot_label,
        name=placement.name,
        short_id=placement.short_id,
        qr_included=placement.qr_included,
        width_mm=placement.width_mm,
        height_mm=placement.height_mm,
    )


def _job_read(job: LabelSheetJob, placements: list[CardPlacement]) -> LabelSheetJobRead:
    return LabelSheetJobRead(
        id=job.id,
        template=job.template,
        backend=job.backend,
        dpi=job.dpi,
        root_location_id=job.root_location_id,
        card_width_mm=job.card_width_mm,
        card_height_mm=job.card_height_mm,
        item_count=job.item_count,
        created_at=job.created_at,
        cards=[_placement_read(p) for p in placements],
    )


def _rendered_sheet_read(result: RenderedSheet) -> LabelSheetJobRead:
    return _job_read(result.job, result.placements)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/sheets", response_model=LabelSheetCreated, status_code=status.HTTP_201_CREATED)
def create_label_sheet(
    request: LabelSheetRequest, db: Session = Depends(get_db)
) -> LabelSheetCreated:
    """Render a sheet now, print it through the chosen backend, and record it.

    A replayed request (same `client_op_id`, same body) hands back the stored
    response without rendering again — the sheet was already written to disk
    and `label_prints`/`last_printed_at` already moved on the first attempt.
    """
    root = _require_location(db, request.root_location_id)

    def work() -> LabelSheetCreated:
        try:
            result = labels.render_sheet(
                db,
                root=root,
                template=request.template,
                slot_ids=request.slot_ids,
                backend_kind=request.backend,
                dpi=request.dpi,
            )
        except LabelError as error:
            raise _label_error(error) from error
        return LabelSheetCreated(job=_rendered_sheet_read(result))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/labels/sheets",
        payload=request,
        response_model=LabelSheetCreated,
        work=work,
    )


@router.get("/sheets/{job_id}", response_model=LabelSheetJobRead)
def read_label_sheet(job_id: RowId, db: Session = Depends(get_db)) -> LabelSheetJobRead:
    """Read back a past sheet job exactly as it was rendered.

    Not a re-render: `placements_json` is the frozen record of what was drawn
    at print time, per the same "a reprint matches the original" reasoning
    `label_prints` exists for. A card for a since-renamed location still
    reports the name it was printed with here — ask for a fresh `POST` to see
    the current one.
    """
    job = db.get(LabelSheetJob, job_id)
    if job is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_job", "message": f"no label sheet job with id {job_id}"},
        )
    return _job_read(job, labels.placements_from_json(job.placements_json))
