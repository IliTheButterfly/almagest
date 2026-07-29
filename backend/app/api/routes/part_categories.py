"""`/api/part-categories` — authoring the taxonomy tree.

The read half of this prefix lives in `app/api/routes/facets.py`
(`GET`, the browse-by-type rail with its descendant-inclusive counts), for the
same reason `documents.parts_router` rides alongside `parts`: what it returns
belongs to the facet panel. This module is the write half.

**A category is the thing that owns filterable fields.** `part_kinds` says what
something fundamentally is; `parameter_template.applies_to_category` names a
*category*, so if the user wants somewhere to hang "ESR" they want a category.
`/api/part-kinds`' docstring has the other half of that distinction.

Every write goes through `TreeRepository`, never by hand. `depth`, `id_path` and
`label_path` are a cache reconstructible from `parent_id` alone, and the one thing
that must not happen is code that writes a path itself — `id_path` is what
subtree search, descendant counts and now field inheritance all read.

`slug` is required rather than derived from the name. It is an addressing key —
`?category=capacitor` in every search request and every shared URL — and a server
that invented it would be inventing a second slug policy beside the one the client
already shows the user in the form.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import RowId
from app.api.schemas import ReplayableResponse
from app.db.session import get_db
from app.models.catalog import Part, PartCategory
from app.models.enums import SizeClass
from app.services.tree import CycleError, category_tree

router = APIRouter(prefix="/api/part-categories", tags=["part-categories"])


class PartCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    parent_id: RowId | None = None
    description: str | None = None
    default_size_class: SizeClass | None = None
    default_fill_factor: float | None = Field(default=None, gt=0, le=1)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class PartCategoryUpdate(BaseModel):
    """Rename, re-describe, re-default. Reparenting is `POST .../move`.

    No `slug`: like a part kind's, it is an addressing key that already appears in
    saved search URLs. No `parent_id` either — a move rebuilds the path cache for
    the whole table, which is a different operation from an edit and deserves to
    look like one.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    default_size_class: SizeClass | None = None
    default_fill_factor: float | None = Field(default=None, gt=0, le=1)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class PartCategoryMove(BaseModel):
    #: Explicit null promotes the category to a root. Meaningful, not a no-op.
    parent_id: RowId | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class PartCategoryRead(BaseModel):
    id: int
    slug: str
    name: str
    parent_id: int | None
    description: str | None
    depth: int
    #: The derived cache, reported so a client can show "Passives > Capacitors"
    #: without walking the tree itself. Never sent *in*.
    label_path: str
    default_size_class: str | None
    default_fill_factor: float | None
    #: Parts filed directly here, not counting descendants — the rail's
    #: `CategoryNode.part_count` is the descendant-inclusive one, and conflating
    #: the two is how a tree ends up disagreeing with itself.
    own_part_count: int


class PartCategoryCreated(ReplayableResponse):
    part_category: PartCategoryRead


class PartCategoryEdited(ReplayableResponse):
    part_category: PartCategoryRead


def _read(db: Session, category: PartCategory) -> PartCategoryRead:
    own = int(
        db.execute(
            select(func.count()).select_from(Part).where(Part.category_id == category.id)
        ).scalar_one()
    )
    return PartCategoryRead(
        id=category.id,
        slug=category.slug,
        name=category.name,
        parent_id=category.parent_id,
        description=category.description,
        depth=category.depth,
        label_path=category.label_path,
        default_size_class=category.default_size_class,
        default_fill_factor=category.default_fill_factor,
        own_part_count=own,
    )


def _require_category(db: Session, category_id: RowId, *, label: str = "category") -> PartCategory:
    category = db.get(PartCategory, category_id)
    if category is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_part_category",
                "message": f"no part {label} with id {category_id}",
            },
        )
    return category


@router.post("", response_model=PartCategoryCreated, status_code=status.HTTP_201_CREATED)
def create_part_category(
    request: PartCategoryCreate, db: Session = Depends(get_db)
) -> PartCategoryCreated:
    """Add a category, optionally under a parent.

    The path cache is rebuilt immediately, so the row comes back with a correct
    `label_path` and a field authored on an ancestor is inherited by it on the very
    next request rather than after some later job.
    """
    if request.parent_id is not None:
        _require_category(db, request.parent_id, label="parent category")
    # Before the insert, not after: see `create_part_kind` for why a unique-slug
    # violation must never reach `idempotency.run`.
    if (
        db.execute(
            select(PartCategory.id).where(PartCategory.slug == request.slug)
        ).scalar_one_or_none()
        is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "duplicate_slug",
                "message": f"a part category with slug {request.slug!r} already exists",
            },
        )

    def work() -> PartCategoryCreated:
        category = PartCategory(
            name=request.name,
            slug=request.slug,
            parent_id=request.parent_id,
            description=request.description,
            default_size_class=request.default_size_class,
            default_fill_factor=request.default_fill_factor,
        )
        category_tree(db).insert_and_index(category)
        return PartCategoryCreated(part_category=_read(db, category))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/part-categories",
        payload=request,
        response_model=PartCategoryCreated,
        work=work,
    )


@router.patch("/{category_id}", response_model=PartCategoryEdited)
def update_part_category(
    category_id: RowId, request: PartCategoryUpdate, db: Session = Depends(get_db)
) -> PartCategoryEdited:
    """Rename a category. The rename propagates into every descendant's
    `label_path`, which is exactly why the rebuild runs here rather than nightly."""
    category = _require_category(db, category_id)
    assigned = set(request.model_fields_set)

    def work() -> PartCategoryEdited:
        renamed = False
        if "name" in assigned and request.name is not None:
            renamed = request.name != category.name
            category.name = request.name
        # Explicit null clears these three — a category that no longer wants a
        # default has to be able to say so.
        if "description" in assigned:
            category.description = request.description
        if "default_size_class" in assigned:
            category.default_size_class = request.default_size_class
        if "default_fill_factor" in assigned:
            category.default_fill_factor = request.default_fill_factor
        db.flush()
        if renamed:
            category_tree(db).rebuild_paths()
            db.refresh(category)
        return PartCategoryEdited(part_category=_read(db, category))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PATCH /api/part-categories/{id}",
        payload=request,
        response_model=PartCategoryEdited,
        work=work,
    )


@router.post("/{category_id}/move", response_model=PartCategoryEdited)
def move_part_category(
    category_id: RowId, request: PartCategoryMove, db: Session = Depends(get_db)
) -> PartCategoryEdited:
    """Reparent a category and its whole subtree.

    `TreeRepository.move` walks `parent_id` — the authoritative adjacency list, not
    the cache — to refuse a cycle, then rebuilds every path. A cycle admitted
    through a stale cache would make the rebuild CTE recurse forever.
    """
    category = _require_category(db, category_id)
    if request.parent_id is not None:
        _require_category(db, request.parent_id, label="parent category")

    def work() -> PartCategoryEdited:
        try:
            category_tree(db).move(category, request.parent_id)
        except CycleError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"reason": "would_create_cycle", "message": str(error)},
            ) from error
        db.refresh(category)
        return PartCategoryEdited(part_category=_read(db, category))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/part-categories/{id}/move",
        payload=request,
        response_model=PartCategoryEdited,
        work=work,
    )
