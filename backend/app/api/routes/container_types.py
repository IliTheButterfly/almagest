"""`/api/container-types` — reusable templates for storage, and the canvas
editor that authors their slot layout.

Three routes carry the design from `docs/PLAN.md` ("Layout authoring"):

* `GET|PUT .../slot-template` is the canvas editor's one door. A pure grid
  (`materialize_slots=False`) costs zero rows; `PUT` with anything the
  generator would not already produce — a merge, a relabel, a size class —
  materialises the whole canvas. See `app.services.layout_authoring` for the
  half that actually implements this; this module is wire types and lookups.
* `PATCH` and `PUT .../slot-template` both **implicitly clone** a seed type
  rather than mutate it — seeds are the shared library every fresh install
  starts with, and editing one is defined to mean "start from a copy of it".
  That side effect is the reason both routes carry `client_op_id`, breaking
  from `PartUpdate`'s convention that a PATCH needs no idempotency guard: a
  retried edit of a seed must not silently spawn a second clone.
* `POST .../clone` is the *explicit* form of the same operation, for "this
  cabinet is identical to that one" with no edit attached.

Instantiating a type into the location tree, and reapplying an edited layout
onto an already-instantiated location, live in `app/api/routes/locations.py`
instead — they operate on `locations`, not on the type.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import GridSpan, RowId
from app.api.schemas import ReplayableResponse, SlotSpecIn, SlotSpecOut
from app.db.session import get_db
from app.models.enums import CapacityModel, ChildLayout, SlotLabelScheme
from app.models.storage import ContainerType
from app.services import layout_authoring as layout
from app.services.layout_authoring import LayoutError, SlotSpec

router = APIRouter(prefix="/api/container-types", tags=["container-types"])


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


def _dump_json(value: object | None) -> str | None:
    return None if value is None else json.dumps(value)


def _load_json(raw: str | None) -> dict[str, Any] | None:
    return json.loads(raw) if raw else None


def _load_json_list(raw: str | None) -> list[str] | None:
    return json.loads(raw) if raw else None


class ContainerTypeWrite(BaseModel):
    """Fields common to creating and updating a type.

    Deliberately excludes `is_seed` — the API can never mint one; seeding is a
    data migration, not a request a client can make — and excludes the slot
    template, which is `.../slot-template`'s job alone so there is exactly one
    path that can materialise a type.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    child_layout: ChildLayout | None = None
    grid_rows: GridSpan | None = None
    grid_cols: GridSpan | None = None
    grid_pitch_mm: float | None = Field(default=None, gt=0)
    grid_height_unit_mm: float | None = Field(default=None, gt=0)
    footprint_cols: GridSpan | None = None
    footprint_rows: GridSpan | None = None
    footprint_height_u: GridSpan | None = None
    slot_label_scheme: SlotLabelScheme | None = None
    slot_label_params: dict[str, Any] | None = None
    capacity_model: CapacityModel | None = None
    capacity_slots: GridSpan | None = None
    max_parts_per_slot: GridSpan | None = None
    inner_length_mm: float | None = Field(default=None, gt=0)
    inner_width_mm: float | None = Field(default=None, gt=0)
    inner_height_mm: float | None = Field(default=None, gt=0)
    default_fill_factor: float | None = Field(default=None, gt=0, le=1)
    full_threshold: float | None = Field(default=None, gt=0, le=1)
    esd_safe: bool | None = None
    is_placeable: bool | None = None
    max_item_dimension_mm: float | None = Field(default=None, gt=0)
    allowed_part_kinds: list[str] | None = None
    front_width_mm: float | None = Field(default=None, gt=0)
    front_height_mm: float | None = Field(default=None, gt=0)


class ContainerTypeCreate(ContainerTypeWrite):
    slug: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ContainerTypeUpdate(ContainerTypeWrite):
    #: See the module docstring: editing a seed clones it, so a retry of this
    #: exact edit must replay rather than mint a second clone.
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ContainerTypeRead(BaseModel):
    id: int
    slug: str
    display_name: str
    description: str | None
    child_layout: str
    grid_rows: int | None
    grid_cols: int | None
    grid_pitch_mm: float | None
    grid_height_unit_mm: float | None
    footprint_cols: int | None
    footprint_rows: int | None
    footprint_height_u: int | None
    slot_label_scheme: str
    slot_label_params: dict[str, Any] | None
    materialize_slots: bool
    capacity_model: str
    capacity_slots: int | None
    max_parts_per_slot: int | None
    inner_length_mm: float | None
    inner_width_mm: float | None
    inner_height_mm: float | None
    default_fill_factor: float
    full_threshold: float
    esd_safe: bool | None
    is_placeable: bool
    max_item_dimension_mm: float | None
    allowed_part_kinds: list[str] | None
    front_width_mm: float | None
    front_height_mm: float | None
    is_seed: bool


class ContainerTypeCreated(ReplayableResponse):
    container_type: ContainerTypeRead


class ContainerTypeEdited(ReplayableResponse):
    container_type: ContainerTypeRead
    #: True when the id in `container_type` differs from the id in the URL —
    #: the request targeted a seed, so it was cloned rather than mutated.
    cloned: bool


class CloneRequest(BaseModel):
    slug: str | None = Field(
        default=None, max_length=128, description="Defaults to '{source-slug}-copy[-N]'."
    )
    display_name: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class SlotTemplateRead(BaseModel):
    container_type_id: int
    materialize_slots: bool
    grid_rows: int | None
    grid_cols: int | None
    slot_label_scheme: str
    slot_label_params: dict[str, Any] | None
    slots: list[SlotSpecOut]


class SlotTemplateWrite(BaseModel):
    #: Omitted fields leave the type's current canvas size/scheme unchanged;
    #: `slots` is always the *complete* desired layout, never a delta.
    grid_rows: GridSpan | None = None
    grid_cols: GridSpan | None = None
    slot_label_scheme: SlotLabelScheme | None = None
    slot_label_params: dict[str, Any] | None = None
    slots: list[SlotSpecIn] = Field(default_factory=list)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class SlotTemplateWritten(ReplayableResponse):
    template: SlotTemplateRead
    cloned: bool
    #: The id actually written to — the URL's id unless `cloned`.
    container_type_id: RowId


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _require_type(db: Session, container_type_id: RowId) -> ContainerType:
    container_type = db.get(ContainerType, container_type_id)
    if container_type is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_container_type",
                "message": f"no container type with id {container_type_id}",
            },
        )
    return container_type


def _read(container_type: ContainerType) -> ContainerTypeRead:
    return ContainerTypeRead(
        id=container_type.id,
        slug=container_type.slug,
        display_name=container_type.display_name,
        description=container_type.description,
        child_layout=container_type.child_layout,
        grid_rows=container_type.grid_rows,
        grid_cols=container_type.grid_cols,
        grid_pitch_mm=container_type.grid_pitch_mm,
        grid_height_unit_mm=container_type.grid_height_unit_mm,
        footprint_cols=container_type.footprint_cols,
        footprint_rows=container_type.footprint_rows,
        footprint_height_u=container_type.footprint_height_u,
        slot_label_scheme=container_type.slot_label_scheme,
        slot_label_params=_load_json(container_type.slot_label_params_json),
        materialize_slots=container_type.materialize_slots,
        capacity_model=container_type.capacity_model,
        capacity_slots=container_type.capacity_slots,
        max_parts_per_slot=container_type.max_parts_per_slot,
        inner_length_mm=container_type.inner_length_mm,
        inner_width_mm=container_type.inner_width_mm,
        inner_height_mm=container_type.inner_height_mm,
        default_fill_factor=container_type.default_fill_factor,
        full_threshold=container_type.full_threshold,
        esd_safe=container_type.esd_safe,
        is_placeable=container_type.is_placeable,
        max_item_dimension_mm=container_type.max_item_dimension_mm,
        allowed_part_kinds=_load_json_list(container_type.allowed_part_kinds_json),
        front_width_mm=container_type.front_width_mm,
        front_height_mm=container_type.front_height_mm,
        is_seed=container_type.is_seed,
    )


#: Fields copied straight across on both create and update — anything that
#: needs translation (json blobs, enums as plain strings) is handled beside
#: this rather than folded in, so the exceptions stay visible.
_DIRECT_FIELDS = (
    "display_name",
    "description",
    "grid_rows",
    "grid_cols",
    "grid_pitch_mm",
    "grid_height_unit_mm",
    "footprint_cols",
    "footprint_rows",
    "footprint_height_u",
    "capacity_slots",
    "max_parts_per_slot",
    "inner_length_mm",
    "inner_width_mm",
    "inner_height_mm",
    "default_fill_factor",
    "full_threshold",
    "esd_safe",
    "is_placeable",
    "max_item_dimension_mm",
    "front_width_mm",
    "front_height_mm",
)


def _apply(container_type: ContainerType, fields: ContainerTypeWrite, assigned: set[str]) -> None:
    """Copy only the fields the caller actually set — driven by
    `model_fields_set`, exactly as `app.api.routes.parts._apply` is, so a PATCH
    can distinguish "leave this alone" from "clear it"."""
    for name in _DIRECT_FIELDS:
        if name in assigned:
            setattr(container_type, name, getattr(fields, name))
    if "child_layout" in assigned and fields.child_layout is not None:
        container_type.child_layout = fields.child_layout
    if "slot_label_scheme" in assigned and fields.slot_label_scheme is not None:
        container_type.slot_label_scheme = fields.slot_label_scheme
    if "slot_label_params" in assigned:
        container_type.slot_label_params_json = _dump_json(fields.slot_label_params)
    if "capacity_model" in assigned and fields.capacity_model is not None:
        container_type.capacity_model = fields.capacity_model
    if "allowed_part_kinds" in assigned:
        container_type.allowed_part_kinds_json = _dump_json(fields.allowed_part_kinds)


def _layout_error(error: LayoutError) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"reason": error.reason, "message": str(error)},
    )


def _slot_template_read(container_type: ContainerType, slots: list[SlotSpec]) -> SlotTemplateRead:
    return SlotTemplateRead(
        container_type_id=container_type.id,
        materialize_slots=container_type.materialize_slots,
        grid_rows=container_type.grid_rows,
        grid_cols=container_type.grid_cols,
        slot_label_scheme=container_type.slot_label_scheme,
        slot_label_params=_load_json(container_type.slot_label_params_json),
        slots=[
            SlotSpecOut(
                row_idx=spec.row_idx,
                col_idx=spec.col_idx,
                row_span=spec.row_span,
                col_span=spec.col_span,
                slot_label=spec.slot_label,
                size_class=spec.size_class,
                inner_volume_mm3=spec.inner_volume_mm3,
                sort_order=order,
            )
            for spec, order in layout.compute_sort_order(slots)
        ],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=ContainerTypeCreated, status_code=status.HTTP_201_CREATED)
def create_container_type(
    request: ContainerTypeCreate, db: Session = Depends(get_db)
) -> ContainerTypeCreated:
    # Checked before the insert, not caught after: `idempotency.run` rolls back
    # on IntegrityError to absorb a duplicate *client_op_id*, so letting a slug
    # collision reach the same handler conflates two unrelated conditions and
    # returned a bare 500. The sibling clone route already checks first.
    if (
        db.execute(
            select(ContainerType.id).where(ContainerType.slug == request.slug)
        ).scalar_one_or_none()
        is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "duplicate_slug",
                "message": f"a container type with slug {request.slug!r} already exists",
            },
        )

    def work() -> ContainerTypeCreated:
        container_type = ContainerType(slug=request.slug, display_name=request.display_name)
        _apply(container_type, request, set(request.model_fields_set))
        db.add(container_type)
        db.flush()
        return ContainerTypeCreated(container_type=_read(container_type))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/container-types",
        payload=request,
        response_model=ContainerTypeCreated,
        work=work,
    )


@router.get("", response_model=list[ContainerTypeRead])
def list_container_types(
    db: Session = Depends(get_db), is_seed: bool | None = None
) -> list[ContainerTypeRead]:
    stmt = select(ContainerType).order_by(ContainerType.slug)
    if is_seed is not None:
        stmt = stmt.where(ContainerType.is_seed.is_(is_seed))
    return [_read(row) for row in db.execute(stmt).scalars()]


@router.get("/{container_type_id}", response_model=ContainerTypeRead)
def read_container_type(
    container_type_id: RowId, db: Session = Depends(get_db)
) -> ContainerTypeRead:
    return _read(_require_type(db, container_type_id))


@router.patch("/{container_type_id}", response_model=ContainerTypeEdited)
def update_container_type(
    container_type_id: RowId, request: ContainerTypeUpdate, db: Session = Depends(get_db)
) -> ContainerTypeEdited:
    """Edit a type. A seed is read-only, so editing one clones it first —
    see the module docstring for why this, unlike `PartUpdate`, needs the
    idempotency guard."""
    original = _require_type(db, container_type_id)

    def work() -> ContainerTypeEdited:
        target, cloned = layout.ensure_editable(db, original)
        _apply(target, request, set(request.model_fields_set))
        db.flush()
        return ContainerTypeEdited(container_type=_read(target), cloned=cloned)

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PATCH /api/container-types/{id}",
        payload=request,
        response_model=ContainerTypeEdited,
        work=work,
    )


@router.post(
    "/{container_type_id}/clone",
    response_model=ContainerTypeCreated,
    status_code=status.HTTP_201_CREATED,
)
def clone_container_type(
    container_type_id: RowId, request: CloneRequest, db: Session = Depends(get_db)
) -> ContainerTypeCreated:
    """ "This cabinet is identical to that one" — the explicit form of clone,
    with no edit attached. Works on any type, seed or not."""
    source = _require_type(db, container_type_id)

    def work() -> ContainerTypeCreated:
        slug = request.slug or layout.default_clone_slug(db, source.slug)
        if db.execute(
            select(ContainerType.id).where(ContainerType.slug == slug)
        ).scalar_one_or_none():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "duplicate_slug",
                    "message": f"a container type with slug {slug!r} already exists",
                },
            )
        clone = layout.clone_type(db, source, slug=slug, display_name=request.display_name)
        return ContainerTypeCreated(container_type=_read(clone))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/container-types/{id}/clone",
        payload=request,
        response_model=ContainerTypeCreated,
        work=work,
    )


@router.get("/{container_type_id}/slot-template", response_model=SlotTemplateRead)
def read_slot_template(container_type_id: RowId, db: Session = Depends(get_db)) -> SlotTemplateRead:
    """The type's effective layout — generated or materialised, indistinguishably."""
    container_type = _require_type(db, container_type_id)
    slots = layout.effective_slots_for_type(db, container_type)
    return _slot_template_read(container_type, slots)


@router.put("/{container_type_id}/slot-template", response_model=SlotTemplateWritten)
def write_slot_template(
    container_type_id: RowId, request: SlotTemplateWrite, db: Session = Depends(get_db)
) -> SlotTemplateWritten:
    """Save the canvas. A seed clones first (see the module docstring); the
    grid then materialises **unless** `slots` is exactly what the generator
    would already produce for the (possibly just-updated) canvas size/scheme,
    in which case nothing is written at all.
    """
    original = _require_type(db, container_type_id)

    def work() -> SlotTemplateWritten:
        target, cloned = layout.ensure_editable(db, original)

        if request.grid_rows is not None:
            target.grid_rows = request.grid_rows
        if request.grid_cols is not None:
            target.grid_cols = request.grid_cols
        if request.slot_label_scheme is not None:
            target.slot_label_scheme = request.slot_label_scheme
        if "slot_label_params" in request.model_fields_set:
            target.slot_label_params_json = _dump_json(request.slot_label_params)

        scheme = SlotLabelScheme(target.slot_label_scheme)
        params = layout.parse_params(target)
        desired = [
            SlotSpec(
                row_idx=item.row_idx,
                col_idx=item.col_idx,
                row_span=item.row_span,
                col_span=item.col_span,
                slot_label=item.slot_label
                or layout.generate_label(
                    scheme,
                    params,
                    item.row_idx,
                    item.col_idx,
                    grid_rows=target.grid_rows,
                    grid_cols=target.grid_cols,
                ),
                size_class=item.size_class,
                inner_volume_mm3=item.inner_volume_mm3,
            )
            for item in request.slots
        ]

        try:
            layout.replace_type_slots(db, target, desired)
        except LayoutError as error:
            raise _layout_error(error) from error

        db.flush()
        slots = layout.effective_slots_for_type(db, target)
        return SlotTemplateWritten(
            template=_slot_template_read(target, slots), cloned=cloned, container_type_id=target.id
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PUT /api/container-types/{id}/slot-template",
        payload=request,
        response_model=SlotTemplateWritten,
        work=work,
    )
