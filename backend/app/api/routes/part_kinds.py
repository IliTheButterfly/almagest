"""`/api/part-kinds` — what a thing fundamentally *is*.

Iliana: "we currently have no way to create new part types." "Part type" names two
different things in this schema, and the UI must not make the user guess which one
they wanted:

* a **kind** (this module) is what something fundamentally is — a tool is not a
  component, and non-component inventory was in scope from the first migration,
  which is why `part_kinds` is a table rather than a boolean. It is the top-level
  split `/api/search/parts?part_kind=` filters on. It carries **no fields**.
* a **category** (`app/api/routes/part_categories.py`) is where a thing sits in the
  taxonomy, and it is what filterable fields hang off via
  `parameter_template.applies_to_category`.

So: author a kind when the thing is a different *sort* of inventory; author a
category when you want somewhere to put fields.

`slug` is deliberately **immutable**. It is not a display string — it is the value
`part_kind` takes in a search request and therefore in every shared search URL, so
renaming it silently breaks links that already exist. The display name is the
editable one, which is the whole reason both columns exist.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import RowId, SortOrder
from app.api.schemas import ReplayableResponse
from app.db.session import get_db
from app.models.catalog import Part, PartKind

router = APIRouter(prefix="/api/part-kinds", tags=["part-kinds"])


class PartKindCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    sort_order: SortOrder = 0
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class PartKindUpdate(BaseModel):
    """No `slug` — see the module docstring: it is a search parameter, not a label."""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    sort_order: SortOrder | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class PartKindRead(BaseModel):
    id: int
    slug: str
    display_name: str
    sort_order: int
    #: How many parts are of this kind. Shown so "delete this kind" can be
    #: greyed out honestly rather than attempted and refused by
    #: `parts.part_kind_id`'s `ON DELETE RESTRICT`.
    part_count: int


class PartKindCreated(ReplayableResponse):
    part_kind: PartKindRead


class PartKindEdited(ReplayableResponse):
    part_kind: PartKindRead


def _read(db: Session, part_kind: PartKind) -> PartKindRead:
    count = int(
        db.execute(
            select(func.count()).select_from(Part).where(Part.part_kind_id == part_kind.id)
        ).scalar_one()
    )
    return PartKindRead(
        id=part_kind.id,
        slug=part_kind.slug,
        display_name=part_kind.display_name,
        sort_order=part_kind.sort_order,
        part_count=count,
    )


def _require_kind(db: Session, part_kind_id: RowId) -> PartKind:
    part_kind = db.get(PartKind, part_kind_id)
    if part_kind is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_part_kind",
                "message": f"no part kind with id {part_kind_id}",
            },
        )
    return part_kind


@router.get("", response_model=list[PartKindRead])
def list_part_kinds(db: Session = Depends(get_db)) -> list[PartKindRead]:
    """Every kind, in the order the picker should show them."""
    stmt = select(PartKind).order_by(PartKind.sort_order, PartKind.display_name)
    return [_read(db, row) for row in db.execute(stmt).scalars()]


@router.post("", response_model=PartKindCreated, status_code=status.HTTP_201_CREATED)
def create_part_kind(request: PartKindCreate, db: Session = Depends(get_db)) -> PartKindCreated:
    # Checked before the insert rather than caught after: `idempotency.run` rolls
    # back on IntegrityError to absorb a duplicate *client_op_id*, so a unique-slug
    # violation reaching it comes back as a bare 500. Same trap, same fix, as
    # `create_container_type`.
    if (
        db.execute(select(PartKind.id).where(PartKind.slug == request.slug)).scalar_one_or_none()
        is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "duplicate_slug",
                "message": f"a part kind with slug {request.slug!r} already exists",
            },
        )

    def work() -> PartKindCreated:
        part_kind = PartKind(
            slug=request.slug,
            display_name=request.display_name,
            sort_order=request.sort_order,
        )
        db.add(part_kind)
        db.flush()
        return PartKindCreated(part_kind=_read(db, part_kind))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/part-kinds",
        payload=request,
        response_model=PartKindCreated,
        work=work,
    )


@router.patch("/{part_kind_id}", response_model=PartKindEdited)
def update_part_kind(
    part_kind_id: RowId, request: PartKindUpdate, db: Session = Depends(get_db)
) -> PartKindEdited:
    """Rename a kind, or move it in the picker. Its slug stays put."""
    part_kind = _require_kind(db, part_kind_id)
    assigned = set(request.model_fields_set)

    def work() -> PartKindEdited:
        if "display_name" in assigned and request.display_name is not None:
            part_kind.display_name = request.display_name
        if "sort_order" in assigned and request.sort_order is not None:
            part_kind.sort_order = request.sort_order
        db.flush()
        return PartKindEdited(part_kind=_read(db, part_kind))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PATCH /api/part-kinds/{id}",
        payload=request,
        response_model=PartKindEdited,
        work=work,
    )
