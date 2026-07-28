"""`/api/parts` — the definition of a thing, never a quantity and never a place.

**Only `name` is required.** `part_kind` defaults, everything else is optional,
and that is load-bearing rather than lax: the failure mode that killed every
abandoned system in this space is intake friction, so an unrecognised distributor
label has to become a legal row in one tap. Curation is deferred to the review
queue via `is_stub`, not demanded at the moment someone is holding a bag of parts
in one hand and a phone in the other.

Quantity is deliberately absent from every request model here. It lives on
`stock_lots`, and `/api/stock/receive` is the only way to put any there.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.schemas import LotRead, ReplayableResponse, lot_read
from app.db.session import get_db
from app.models.catalog import Manufacturer, PackageType, Part, PartCategory, PartKind
from app.models.enums import EntityType, VolumeSource
from app.models.stock import StockLot
from app.services import capacity, shortid
from app.services.scanning.codes import normalize_mpn
from app.services.scanning.describe import describe

router = APIRouter(prefix="/api/parts", tags=["parts"])

#: The `part_kinds` row seeded by the first migration. A default rather than a
#: required field so the fast path is one field long; anything else is a slug the
#: caller has to name.
DEFAULT_PART_KIND = "component"


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class PartWrite(BaseModel):
    """Fields common to creating and updating a part."""

    mpn: str | None = Field(default=None, max_length=255)
    manufacturer_id: int | None = None
    category_id: int | None = None
    package_type_id: int | None = None
    uom_id: int | None = None
    description: str | None = None
    keywords: str | None = None
    notes: str | None = None
    is_stub: bool | None = Field(
        default=None,
        description="Set when the row came from a scan nothing resolved. Drives the review queue.",
    )
    is_active: bool | None = None

    length_mm: float | None = Field(default=None, gt=0)
    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)
    shape_factor: float | None = Field(default=None, gt=0, le=1)
    unit_volume_mm3: float | None = Field(
        default=None,
        gt=0,
        description=(
            "A measured override. Setting it pins `volume_source` to 'override', "
            "so the dimension cascade stops recomputing it."
        ),
    )
    unit_mass_mg: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Learned from a hand-counted reference batch. Null means counting by "
            "weight is refused for this part rather than attempted badly."
        ),
    )


class PartCreate(PartWrite):
    name: str = Field(min_length=1, max_length=255)
    part_kind: str = Field(
        default=DEFAULT_PART_KIND, max_length=64, description="`part_kinds.slug`"
    )
    mint_short_id: bool = Field(
        default=False,
        description=(
            "Allocate a printed identifier. Off by default: a part is a definition, "
            "and it is containers that get labels. Turn it on for a bagged part that "
            "needs one of its own."
        ),
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class PartUpdate(PartWrite):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    part_kind: str | None = Field(default=None, max_length=64)


class PartRead(BaseModel):
    id: int
    name: str
    part_kind: str
    category_id: int | None
    mpn: str | None
    #: Casefolded, punctuation-stripped. Derived from `mpn` by
    #: `services.scanning.codes.normalize_mpn` and never accepted from a client:
    #: a value written by any other rule is invisible to the resolver's bare-MPN
    #: step while still looking perfectly correct in the row.
    mpn_norm: str | None
    manufacturer_id: int | None
    description: str | None
    keywords: str | None
    notes: str | None
    package_type_id: int | None
    uom_id: int | None
    is_stub: bool
    is_active: bool
    length_mm: float | None
    width_mm: float | None
    height_mm: float | None
    shape_factor: float | None
    unit_volume_mm3: float | None
    #: Which rung of the dimension cascade produced `unit_volume_mm3`, so the UI
    #: can say "estimated from package 0603" instead of showing a guess as a
    #: measurement.
    volume_source: str | None
    unit_mass_mg: float | None
    hot_score: float
    short_id: str | None
    display: str | None
    #: Summed from `stock_lots.qty_milli_cached` — a sum over the *cache*, one row
    #: per physical package, never over the ledger.
    total_qty_milli: int
    lots: list[LotRead]


class PartCreated(ReplayableResponse):
    part: PartRead


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _lots_of(db: Session, part_id: int) -> list[StockLot]:
    return list(
        db.execute(select(StockLot).where(StockLot.part_id == part_id).order_by(StockLot.id))
        .scalars()
        .all()
    )


def _read(db: Session, part: Part) -> PartRead:
    kind = db.get(PartKind, part.part_kind_id)
    entity = describe(db, EntityType.PART, part.id)
    lots = _lots_of(db, part.id)
    return PartRead(
        id=part.id,
        name=part.name,
        part_kind=kind.slug if kind is not None else "",
        category_id=part.category_id,
        mpn=part.mpn,
        mpn_norm=part.mpn_norm,
        manufacturer_id=part.manufacturer_id,
        description=part.description,
        keywords=part.keywords,
        notes=part.notes,
        package_type_id=part.package_type_id,
        uom_id=part.uom_id,
        is_stub=part.is_stub,
        is_active=part.is_active,
        length_mm=part.length_mm,
        width_mm=part.width_mm,
        height_mm=part.height_mm,
        shape_factor=part.shape_factor,
        unit_volume_mm3=part.unit_volume_mm3,
        volume_source=part.volume_source,
        unit_mass_mg=part.unit_mass_mg,
        hot_score=part.hot_score,
        short_id=entity.short_id,
        display=entity.display,
        total_qty_milli=sum(lot.qty_milli_cached for lot in lots),
        lots=[lot_read(db, lot) for lot in lots],
    )


def _resolve_part_kind(db: Session, slug: str) -> PartKind:
    kind = db.execute(select(PartKind).where(PartKind.slug == slug)).scalar_one_or_none()
    if kind is None:
        known = ", ".join(sorted(db.execute(select(PartKind.slug)).scalars().all()))
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "unknown_part_kind",
                "message": f"{slug!r} is not a part kind; known: {known}",
            },
        )
    return kind


def _check_references(db: Session, fields: PartWrite) -> None:
    """Reject a dangling FK with a reason instead of an `IntegrityError`.

    The database would catch these anyway; the point is that the caller gets
    "there is no category 47" rather than a constraint name.
    """
    for value, model, label in (
        (fields.manufacturer_id, Manufacturer, "manufacturer"),
        (fields.category_id, PartCategory, "category"),
    ):
        if value is not None and db.get(model, value) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"reason": f"unknown_{label}", "message": f"no {label} with id {value}"},
            )


def _apply(db: Session, part: Part, fields: PartWrite, assigned: set[str]) -> None:
    """Copy the fields the caller actually set onto the row.

    Driven by `model_fields_set` rather than by "is it None", so a PATCH can
    clear an optional column — `{"mpn": null}` means "this part has no part
    number", which is different from omitting the field.
    """
    for name in (
        "manufacturer_id",
        "category_id",
        "package_type_id",
        "uom_id",
        "description",
        "keywords",
        "notes",
        "is_stub",
        "is_active",
        "length_mm",
        "width_mm",
        "height_mm",
        "shape_factor",
        "unit_mass_mg",
    ):
        if name in assigned:
            setattr(part, name, getattr(fields, name))

    if "mpn" in assigned:
        part.mpn = fields.mpn
        part.mpn_norm = normalize_mpn(fields.mpn) if fields.mpn else None

    if "unit_volume_mm3" in assigned and fields.unit_volume_mm3 is not None:
        part.unit_volume_mm3 = fields.unit_volume_mm3
        part.volume_source = VolumeSource.OVERRIDE

    # Recomputed on every write, because the cascade's inputs are exactly what
    # just changed. Without this a fresh part has no `unit_volume_mm3` at all and
    # every volume-model container silently reads it as occupying nothing.
    # `apply_volume_cascade` returns an override untouched, so a measured volume
    # pinned above survives this.
    capacity.apply_volume_cascade(
        part,
        db.get(PackageType, part.package_type_id) if part.package_type_id else None,
        db.get(PartCategory, part.category_id) if part.category_id else None,
    )


@router.post("", response_model=PartCreated, status_code=status.HTTP_201_CREATED)
def create_part(request: PartCreate, db: Session = Depends(get_db)) -> PartCreated:
    """Create a part. One field is enough.

    A duplicate `(mpn_norm, manufacturer_id)` is a 409: that pair is uniquely
    indexed because two rows for the same part number from the same manufacturer
    is nearly always a double import, and the right fix is to add stock to the
    row that exists rather than to fork the catalogue.
    """
    kind = _resolve_part_kind(db, request.part_kind)
    _check_references(db, request)

    def work() -> PartCreated:
        part = Part(name=request.name, part_kind_id=kind.id)
        _apply(db, part, request, set(request.model_fields_set))
        db.add(part)
        try:
            db.flush()
        except IntegrityError as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "duplicate_mpn",
                    "message": (
                        f"a part with mpn {request.mpn!r} already exists for this "
                        "manufacturer; add stock to it instead"
                    ),
                },
            ) from error
        if request.mint_short_id:
            shortid.allocate(db, EntityType.PART, part.id)
        return PartCreated(part=_read(db, part))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/parts",
        payload=request,
        response_model=PartCreated,
        work=work,
    )


@router.get("/{part_id}", response_model=PartRead)
def read_part(part_id: int, db: Session = Depends(get_db)) -> PartRead:
    """The part, plus every lot of it and the total on hand."""
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_part", "message": f"no part with id {part_id}"},
        )
    return _read(db, part)


@router.patch("/{part_id}", response_model=PartRead)
def update_part(part_id: int, request: PartUpdate, db: Session = Depends(get_db)) -> PartRead:
    """Edit a part — the review-queue tail of intake.

    Deliberately **not** idempotency-guarded, unlike every other write in the
    API: a PATCH is idempotent by construction, since replaying it sets the same
    fields to the same values. The guard exists to stop a retry becoming a second
    *movement*, and there is no movement here.
    """
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_part", "message": f"no part with id {part_id}"},
        )
    assigned = set(request.model_fields_set)
    _check_references(db, request)

    if "part_kind" in assigned and request.part_kind is not None:
        part.part_kind_id = _resolve_part_kind(db, request.part_kind).id
    if "name" in assigned and request.name is not None:
        part.name = request.name

    _apply(db, part, request, assigned)
    try:
        db.flush()
    except IntegrityError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "duplicate_mpn",
                "message": "another part already has that part number for this manufacturer",
            },
        ) from error
    db.commit()
    return _read(db, part)
