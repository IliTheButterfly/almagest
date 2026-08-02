"""`/api/captures` — the still the scanner kept, and what was read off it.

**This router stores an interpretation; it never performs one.** Every region
posted here was found in the browser, by the same `zxing-wasm` that already runs
the live decode loop and by an OCR pass sitting beside it. That split is not an
accident of who wrote what first:

- ADR 0005 puts every image and PDF pipeline outside the API process, and
  `app.services.blobstore` enforces the API half of it by checking five bytes of
  magic and stopping. A `readBarcodes()` call in a route handler would be the
  first thing to break that, and it would break it on the one replica that a
  phone at a shelf is waiting on.
- The client has the pixels already. It decoded them to draw the outlines the
  user is looking at, so posting the image *and asking the server to find the
  same regions again* would be strictly more work for an identical answer.

So the geometry arrives from the client and is taken at face value. That is safe
because a region is evidence, not authority: a `BARCODE` region's text is
re-resolved through `/api/scan/resolve` like any other payload, and a `TEXT`
region is never allowed to become a part number without a human tapping it. The
worst a lying client achieves is a wrong rectangle drawn over its own photo.

**Regions arrive in more than one instalment, by design.** Barcodes are decoded
in milliseconds; the OCR model is several megabytes and may fail to load at all.
So `POST /api/captures` takes whatever is known immediately, and
`POST /api/captures/{id}/regions` appends what turns up later, moving
`text_status` with it. A capture that stays at `NOT_ATTEMPTED` for ever is a
normal row — the same posture `ExtractionState.PENDING` has for datasheets.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.api.routes.documents import DocumentRead, document_read
from app.db.session import get_db
from app.models.captures import Capture, CaptureRegion
from app.models.documents import SHA256_LENGTH, Document
from app.models.enums import CaptureRegionKind, CaptureTextStatus
from app.models.scanning import ScanSource
from app.models.types import utcnow

router = APIRouter(prefix="/api/captures", tags=["captures"])

#: Matches `capture_regions.symbology`, which matches `scan_events.symbology`.
SYMBOLOGY_MAX_LENGTH = 32

#: Same ceiling as a scanned payload (`app.api.routes.scan.PAYLOAD_MAX_LENGTH`).
#: A barcode region's text *is* a scanned payload and is posted straight to the
#: resolver; an OCR line is far shorter. Rejecting past this happens before the
#: database is touched, so an abusive body cannot become an abusive row.
REGION_TEXT_MAX_LENGTH = 4096

#: One phone photo of one label. Chosen well above what a real capture produces
#: — a busy reel label is a handful of barcodes and a few dozen OCR lines — and
#: low enough that a runaway client cannot write a million rows in one request.
MAX_REGIONS_PER_REQUEST = 512

#: How many captures the list endpoint will return at once.
MAX_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class Point(BaseModel):
    """One corner, in the captured image's own pixel space.

    Not normalised to 0-1. The overlay has to scale onto whatever size the image
    is *rendered* at anyway, so it divides by `width_px`/`height_px` at draw
    time; storing pre-divided floats would lose precision to buy nothing and
    would make a region unreadable without also fetching the capture.
    """

    x: int
    y: int


class CaptureRegionIn(BaseModel):
    kind: CaptureRegionKind
    #: Verbatim, control characters and all — an ECIA payload *is* its GS/RS
    #: separators, and this string is posted unchanged to `/api/scan/resolve`.
    text: str = Field(max_length=REGION_TEXT_MAX_LENGTH)
    #: Four of them, in the order the decoder gave them, so a label read sideways
    #: draws a sideways outline. Exactly four: a quad is fixed-arity, and a
    #: three-corner region is a client bug worth refusing rather than storing.
    corners: list[Point] = Field(min_length=4, max_length=4)
    symbology: str | None = Field(default=None, max_length=SYMBOLOGY_MAX_LENGTH)
    #: 0-100, and only meaningful on a `TEXT` region — see `CaptureRegionKind`.
    #: A value sent for a barcode is dropped rather than refused; the client that
    #: sent it is wrong about the model, not about the picture.
    confidence: int | None = Field(default=None, ge=0, le=100)
    #: Set when this region was already put through the resolver, so the capture
    #: and the scan log point at each other.
    scan_event_id: int | None = None


class CaptureRegionRead(BaseModel):
    id: int
    kind: str
    text: str
    corners: list[Point]
    symbology: str | None
    confidence: int | None
    scan_event_id: int | None
    order_index: int


class CaptureCreate(BaseModel):
    #: An image already stored by `POST /api/documents`. By address rather than
    #: row id, like `DocumentAttachRequest` — the hash is what a client that only
    #: ever saw an upload response is holding.
    sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    device_id: str | None = Field(default=None, max_length=64)
    #: `scan_sources.slug`. An unknown slug is recorded as no source, never
    #: refused — same rule as `/api/scan/resolve`.
    source_slug: str | None = None
    text_status: CaptureTextStatus = CaptureTextStatus.NOT_ATTEMPTED
    note: str | None = None
    regions: list[CaptureRegionIn] = Field(default_factory=list, max_length=MAX_REGIONS_PER_REQUEST)


class CaptureRegionsAppend(BaseModel):
    """A later instalment — in practice, the OCR pass finishing.

    `text_status` is optional because appending barcodes (a re-decode at higher
    effort, say) must not silently claim anything about whether text was read.
    """

    regions: list[CaptureRegionIn] = Field(default_factory=list, max_length=MAX_REGIONS_PER_REQUEST)
    text_status: CaptureTextStatus | None = None


class CaptureRead(BaseModel):
    id: int
    created_at: datetime
    width_px: int
    height_px: int
    text_status: str
    device_id: str | None
    note: str | None
    #: The whole document row rather than a bare hash, because every consumer
    #: needs `url` to draw the thing and would otherwise make a second call for
    #: it.
    document: DocumentRead
    regions: list[CaptureRegionRead]


class CaptureList(BaseModel):
    items: list[CaptureRead]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_source(db: Session, slug: str | None) -> int | None:
    """Attribute the capture to a registered reader, and note that it is alive.

    Deliberately identical to `app.api.routes.scan._resolve_source`, down to
    touching `last_seen_at`: a capture is a scan that kept its frame, and a
    reader that only ever captures should not look dead.
    """
    if not slug:
        return None
    source = db.execute(select(ScanSource).where(ScanSource.slug == slug)).scalar_one_or_none()
    if source is None:
        return None
    source.last_seen_at = utcnow()
    return source.id


def _require_document(db: Session, sha256: str) -> Document:
    document = db.execute(select(Document).where(Document.sha256 == sha256)).scalar_one_or_none()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "document_not_found",
                "message": "Upload the image first; a capture points at a stored document.",
            },
        )
    return document


def _require_capture(db: Session, capture_id: int) -> Capture:
    capture = db.get(Capture, capture_id)
    if capture is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "capture_not_found", "message": f"No capture {capture_id}."},
        )
    return capture


def _add_regions(
    db: Session, capture: Capture, regions: list[CaptureRegionIn], *, start_index: int
) -> None:
    for offset, region in enumerate(regions):
        corners = region.corners
        db.add(
            CaptureRegion(
                capture_id=capture.id,
                kind=region.kind,
                text=region.text,
                symbology=region.symbology,
                # Dropped for a barcode rather than refused: see
                # `CaptureRegionKind`, where the rule that only OCR carries a
                # confidence is stated. Storing a fabricated 100 for a decoded
                # symbol would invite a UI that ranks a guessed word alongside a
                # checksummed payload.
                confidence=(region.confidence if region.kind == CaptureRegionKind.TEXT else None),
                scan_event_id=region.scan_event_id,
                order_index=start_index + offset,
                x0=corners[0].x,
                y0=corners[0].y,
                x1=corners[1].x,
                y1=corners[1].y,
                x2=corners[2].x,
                y2=corners[2].y,
                x3=corners[3].x,
                y3=corners[3].y,
            )
        )


def _region_read(region: CaptureRegion) -> CaptureRegionRead:
    return CaptureRegionRead(
        id=region.id,
        kind=region.kind,
        text=region.text,
        corners=[
            Point(x=region.x0, y=region.y0),
            Point(x=region.x1, y=region.y1),
            Point(x=region.x2, y=region.y2),
            Point(x=region.x3, y=region.y3),
        ],
        symbology=region.symbology,
        confidence=region.confidence,
        scan_event_id=region.scan_event_id,
        order_index=region.order_index,
    )


def _capture_read(db: Session, capture: Capture) -> CaptureRead:
    document = db.get(Document, capture.document_id)
    if document is None:  # pragma: no cover - FK is CASCADE, so this cannot happen
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"reason": "document_missing", "message": "Capture lost its document."},
        )
    regions = (
        db.execute(
            select(CaptureRegion)
            .where(CaptureRegion.capture_id == capture.id)
            .order_by(CaptureRegion.order_index, CaptureRegion.id)
        )
        .scalars()
        .all()
    )
    return CaptureRead(
        id=capture.id,
        created_at=capture.created_at,
        width_px=capture.width_px,
        height_px=capture.height_px,
        text_status=capture.text_status,
        device_id=capture.device_id,
        note=capture.note,
        document=document_read(document),
        regions=[_region_read(region) for region in regions],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=CaptureRead, status_code=status.HTTP_201_CREATED)
def create_capture(request: CaptureCreate, db: Session = Depends(get_db)) -> CaptureRead:
    """Record a still and whatever was read off it in the same breath."""
    document = _require_document(db, request.sha256)
    capture = Capture(
        document_id=document.id,
        width_px=request.width_px,
        height_px=request.height_px,
        source_id=_resolve_source(db, request.source_slug),
        device_id=request.device_id,
        text_status=request.text_status,
        note=request.note,
    )
    db.add(capture)
    # Needed before the regions, which carry the FK. Not a commit: the whole
    # request stays one transaction, so a bad region rolls the capture back with
    # it rather than leaving an image nobody can explain.
    db.flush()
    _add_regions(db, capture, request.regions, start_index=0)
    db.commit()
    db.refresh(capture)
    return _capture_read(db, capture)


@router.get("", response_model=CaptureList)
def list_captures(
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CaptureList:
    """Recent captures, newest first — the desk pass's way back to a photograph."""
    total = db.execute(select(func.count()).select_from(Capture)).scalar_one()
    captures = (
        db.execute(select(Capture).order_by(Capture.id.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return CaptureList(items=[_capture_read(db, capture) for capture in captures], total=total)


@router.get("/{capture_id}", response_model=CaptureRead)
def read_capture(capture_id: RowId, db: Session = Depends(get_db)) -> CaptureRead:
    return _capture_read(db, _require_capture(db, capture_id))


@router.post("/{capture_id}/regions", response_model=CaptureRead)
def append_capture_regions(
    capture_id: RowId, request: CaptureRegionsAppend, db: Session = Depends(get_db)
) -> CaptureRead:
    """Add what turned up after the fact — in practice, the OCR pass finishing.

    Appends rather than replaces, and continues `order_index` from wherever the
    existing rows stopped, so the barcode regions posted at capture time keep
    both their identity and their place at the top of the chip list.
    """
    capture = _require_capture(db, capture_id)
    next_index = (
        db.execute(
            select(func.coalesce(func.max(CaptureRegion.order_index), -1)).where(
                CaptureRegion.capture_id == capture.id
            )
        ).scalar_one()
        + 1
    )
    _add_regions(db, capture, request.regions, start_index=next_index)
    if request.text_status is not None:
        capture.text_status = request.text_status
    db.commit()
    db.refresh(capture)
    return _capture_read(db, capture)


@router.delete("/{capture_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_capture(capture_id: RowId, db: Session = Depends(get_db)) -> None:
    """Throw away a blurry one.

    A hard delete, unlike anything in the stock path. This is a notebook, not a
    ledger — no `RAISE(ABORT)` trigger guards it — and a photograph the user
    judged useless has no history worth compensating for. The blob itself stays:
    it is content-addressed and may be another capture's image, and reclaiming it
    is the scrub job's business (`app.services.documents`), not this route's.
    """
    capture = _require_capture(db, capture_id)
    db.execute(delete(CaptureRegion).where(CaptureRegion.capture_id == capture.id))
    db.delete(capture)
    db.commit()
