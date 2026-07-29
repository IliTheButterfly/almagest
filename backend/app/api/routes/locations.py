"""`/api/locations` — the physical storage tree, and where to put things.

Three reads and two writes, plus `POST /suggest`, which is the auto-assignment
service's only door to the outside world.

The tree comes back **flat**, ordered by `id_path`, with `parent_id` and `depth`
on every row rather than nested children. One query, one shape, and a client that
wants a nested tree builds it in a loop — whereas a recursive response model
generates an awkward recursive type in every generated client, which is a cost
paid by all three consumers to save a loop in one of them.

`label_path` is always derived and always fresh. It is never written to a tag or
a printed label: the moment a box changes shelf, an encoded path is a lie.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import InstanceCount, MassMg, RowId
from app.api.routes.documents import DocumentRead, primary_photo
from app.api.schemas import LotRead, ReplayableResponse, SlotSpecIn, lot_read
from app.db.session import get_db
from app.models.catalog import Packaging, Part
from app.models.enums import CapacityModel, ChildView, ContainerGlyph, EntityType, TagGranularity
from app.models.identity import ObjectId
from app.models.stock import StockLot
from app.models.storage import ContainerType, Location, LocationOccupancy, LocationTag
from app.services import assignment, capacity, glyphs, shortid, views
from app.services import layout_authoring as layout
from app.services.assignment import AssignmentResult
from app.services.capacity import DefragPlan
from app.services.layout_authoring import GuardedLayoutChange, LayoutError, SlotSpec
from app.services.scanning.describe import describe
from app.services.tree import location_tree

router = APIRouter(prefix="/api/locations", tags=["locations"])


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class CapacityRead(BaseModel):
    """A location's fill state. **Advisory in every case.**

    An over-capacity put-away is accepted and `is_overfull` is raised; nothing
    here ever blocks a movement, because a scan that gets rejected teaches the
    user to stop scanning.
    """

    model: str
    #: Null means "no defined capacity" — the `none` model, or dimensions nobody
    #: has filled in — never a smuggled zero.
    capacity: float | None
    used: float
    fill_ratio: float | None
    #: The advisory threshold (default 90% full), not the same claim as
    #: `is_overfull`, which means capacity is literally exceeded.
    is_full: bool
    is_overfull: bool
    unit: str


class LocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: RowId | None = None
    container_type_id: RowId | None = None
    description: str | None = None
    slot_label: str | None = Field(
        default=None,
        max_length=64,
        description="Set for a cell of the parent, e.g. 'C-07'. Unique among siblings.",
    )
    row_idx: int | None = None
    col_idx: int | None = None
    sort_order: int | None = None
    esd_safe: bool | None = Field(
        default=None,
        description="Null inherits from the nearest ancestor that states one.",
    )
    is_placeable: bool | None = Field(
        default=None, description="Null takes the container type's answer."
    )
    fill_factor: float | None = Field(default=None, gt=0, le=1)
    child_view: ChildView | None = Field(
        default=None,
        description=(
            "How this one container draws its children, overriding the container "
            "type's answer (ADR 0006). Null takes the type's, which in turn falls "
            "back to deriving it from the type's declared geometry."
        ),
    )
    glyph: ContainerGlyph | None = Field(
        default=None,
        description=(
            "This one container's own pictogram, overriding the container type's "
            "(see `ContainerGlyph`). Null takes the type's; if the type has none "
            "either, that is 'no glyph', drawn as a neutral placeholder rather "
            "than guessed at."
        ),
    )
    access_score: float | None = Field(default=None, ge=0, le=1)
    tare_mg: MassMg | None = None
    mint_short_id: bool | None = Field(
        default=None,
        description=(
            "Null applies the rule from the design: a standalone container gets a "
            "printed id, a generated grid cell does not — nobody sticks 96 labels on "
            "an 8x12 box. Any cell can be promoted later by asking for one."
        ),
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class LocationRead(BaseModel):
    id: int
    name: str
    description: str | None
    parent_id: int | None
    depth: int
    #: Numeric ids, `/1/5/12/`. Numeric so a rename never invalidates a prefix
    #: query, and that is what every subtree filter in the system is.
    id_path: str
    label_path: str
    container_type_id: int | None
    slot_label: str | None
    esd_safe: bool | None
    #: `esd_safe` resolved up the ancestor chain, which is what a filter actually
    #: applies — marking a whole cabinet ESD-safe has to be one edit.
    effective_esd_safe: bool | None
    is_placeable: bool | None
    #: This location's own override, or null for "use the type" — reported beside
    #: the resolved value for the same reason `esd_safe` is: an editor cannot
    #: offer "stop overriding this" without knowing whether an override exists.
    child_view: str | None
    #: The instance override, else the type's, else derived from the type's
    #: geometry. Never null, and never inherited from an ancestor — see
    #: `app.models.storage.Location.child_view`.
    effective_child_view: str
    #: This container's own pictogram override, or null — reported beside
    #: `effective_glyph` for the same reason `child_view` is: an editor cannot
    #: offer "stop overriding this" without knowing whether an override exists.
    glyph: str | None
    #: The instance override, else the type's, else null — "no glyph chosen" is
    #: a real, terminal state here (unlike `effective_child_view`, there is no
    #: derived rung), and the renderer draws a neutral placeholder for it.
    effective_glyph: str | None
    #: This container's own photo, or null. A `role=photo` document attached
    #: directly to *this* location — not the type's.
    photo: DocumentRead | None
    #: This container's own photo if it has one, else its container type's, else
    #: null. What the detail screen actually shows; the dense tree view shows
    #: `effective_glyph` instead, never this — see `app.models.enums.
    #: ContainerGlyph` for why loading a photo per cell of a 96-cell grid would
    #: be the wrong trade.
    effective_photo: DocumentRead | None
    is_overfull: bool
    is_staging: bool
    access_score: float
    tare_mg: int | None
    short_id: str | None
    display: str | None
    child_count: int
    capacity: CapacityRead
    lots: list[LotRead]
    #: Drives the "never printed" badge (`docs/PLAN.md`, "Label sheets matched
    #: to the layout"). Set by `POST /api/labels/sheets`, never by anything in
    #: this module — a location cannot claim to be printed on its own say-so.
    last_printed_at: datetime | None


class LocationCreated(ReplayableResponse):
    location: LocationRead


class LocationNode(BaseModel):
    """One row of the flat tree. Cheaper than `LocationRead` by design: a tree
    render needs structure and fill state, not every lot in the warehouse."""

    id: int
    name: str
    parent_id: int | None
    depth: int
    id_path: str
    label_path: str
    container_type_id: int | None
    slot_label: str | None
    is_overfull: bool
    is_staging: bool
    #: Carried because **`is_staging` alone no longer identifies the INBOX**. ADR
    #: 0004 gives every project a staging box with the same flag, and the two want
    #: opposite words on screen: the INBOX is a catch-all meant to be emptied,
    #: while a project box holding parts for six months is doing its job. The pair
    #: `(is_staging, is_placeable is False)` is exactly how `capacity`'s own
    #: `get_inbox_location` tells them apart, so the tree asks the same question
    #: rather than sniffing the label path for `PROJECTS`.
    is_placeable: bool | None
    #: **How this node's own children are drawn** (ADR 0006), already resolved:
    #: instance override, else the container type's, else derived from the type's
    #: declared geometry. Resolved server-side rather than left to the client
    #: because the fallback reads `container_types`, and a tree render that had to
    #: join the type library itself would be the N+1 the rest of this response
    #: exists to avoid — as well as a second copy of the rule to disagree with.
    #:
    #: Only the effective value is carried, not the raw override: `LocationNode`
    #: is deliberately the cheap shape, and the map needs to know what to draw,
    #: not who decided. `LocationRead` carries both.
    effective_child_view: str
    #: The canvas this node presents to **its own** children — `grid_rows` and
    #: `grid_cols` off its container type, or null when it declares none.
    #:
    #: Carried because `effective_child_view` is *derived* from exactly these two
    #: columns (`app.services.views.derive_child_view`), so a client that could
    #: not see them could not honour the drawing they promise. A type whose slot
    #: labels are a plain sequence — both seeded Raacos — labels its drawers
    #: `01`...`30`, and a sequential label carries an order and no column: the
    #: server said `cabinet_face` and the client had no canvas to lay that face
    #: out on. Now the fact that decides the picture and the fact that makes it
    #: drawable travel together.
    #:
    #: Null is meaningful and must stay reportable: it is what makes the client
    #: refuse to draw a slotted view rather than guess a column count, which is
    #: the same rule `lib/locations/slots.ts` already applies to labels.
    child_grid_rows: int | None
    child_grid_cols: int | None
    #: The pictogram this node draws in the dense tree view, already resolved —
    #: instance override, else the container type's, else null. **Deliberately
    #: not a photo.** `ContainerLayout.tsx` can lay out dozens of these nodes in
    #: one screen (a baseplate's grid, a cabinet's drawer fronts), and a photo is
    #: a real image fetched and decoded from the document store — loading one
    #: per cell of a 12x8 grid to draw a picture that renders at a few dozen
    #: pixels is the exact waste this axis exists to avoid. `LocationRead`, drawn
    #: for exactly one container at a time, carries the actual `effective_photo`
    #: instead.
    effective_glyph: str | None
    #: Cached in `location_occupancy`, so the tree costs one extra join rather
    #: than a capacity computation per node.
    fill_ratio: float | None
    lot_count: int
    qty_milli: int


class LocationTree(BaseModel):
    #: Root-first, `id_path` order — so a client can render it by indenting on
    #: `depth` without sorting anything.
    nodes: list[LocationNode]


class SuggestRequest(BaseModel):
    part_id: RowId
    packaging_id: int | None = Field(
        default=None,
        description=(
            "The *new* lot's packaging, when it is already known. Its pitch is what "
            "the reel/tube-rack compatibility filter reads; omitting it simply skips "
            "that filter."
        ),
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class CandidateRead(BaseModel):
    location_id: RowId
    label_path: str
    score: float
    free_capacity: float


class MoveStepRead(BaseModel):
    lot_id: RowId
    from_location_id: int
    to_location_id: RowId
    #: Zero for a whole-lot move, mirroring the ledger's own `move` semantics.
    qty_milli: int


class MovePlanRead(BaseModel):
    #: Human-readable, for a one-tap confirmation. Never parsed back.
    rationale: str
    steps: list[MoveStepRead]


class NewSiblingRead(BaseModel):
    parent_id: RowId
    container_type_id: RowId
    based_on_location_id: int


class SuggestResponse(ReplayableResponse):
    """Where to put it, and how confident that answer is.

    **Never an error.** The escalation ladder always terminates in a concrete
    location — dropping soft preferences, materialising an empty grid cell,
    proposing a defrag move, proposing a new sibling container, and finally the
    permanent `INBOX` staging row. `escalation_level` says which rung answered so
    the UI can explain itself instead of presenting every answer as equally
    confident.
    """

    location_id: RowId
    label_path: str
    escalation_level: str
    reason: str
    candidates: list[CandidateRead]
    defrag_plan: MovePlanRead | None = None
    new_sibling_proposal: NewSiblingRead | None = None


class InstantiateRequest(BaseModel):
    """ "Give me N of these" — the bulk-provisioning entry point.

    Instances **own their own copy** of the type's layout from this moment on;
    nothing here links back to `container_type_id` afterwards (docs/PLAN.md,
    "Layout authoring").
    """

    container_type_id: RowId
    count: InstanceCount = 1
    naming_pattern: str = Field(
        min_length=1,
        max_length=255,
        description="'{n}' is replaced with the 1-based index; a count > 1 with no "
        "'{n}' gets ' {n}' appended so instances stay distinguishable.",
    )
    tag_granularity: TagGranularity = Field(
        default=TagGranularity.CONTAINER,
        description="Which new locations get a printed short_id: only the container "
        "roots, or every generated slot too.",
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class InstantiateResponse(ReplayableResponse):
    locations: list[LocationRead]


class AffectedSlotRead(BaseModel):
    location_id: RowId
    slot_label: str
    reasons: list[str]


class ReapplyLayoutRequest(BaseModel):
    """The complete desired layout for this location's own children — never a
    delta. See `app.services.layout_authoring.diff_instance_layout` for how a
    slot surviving, being deleted, or being refused outright is decided."""

    slots: list[SlotSpecIn]
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ReapplyLayoutResponse(ReplayableResponse):
    created: int
    updated: int
    deleted: int
    layout: LayoutRead


class SlotStateRead(BaseModel):
    """One slot's current physical state — the grid cell plus whatever is
    bound or stocked there. Shared by the editor, the provisioning walk and
    the verification walk, per `docs/PLAN.md`."""

    location_id: RowId
    slot_label: str
    row_idx: int
    col_idx: int
    row_span: int
    col_span: int
    size_class: str | None
    inner_volume_mm3: float | None
    sort_order: int
    short_id: str | None
    has_tag: bool
    lot_count: int
    qty_milli: int


class LayoutRead(BaseModel):
    location_id: RowId
    container_type_id: int | None
    #: The bounding box of the current children — **derived**, never stored:
    #: an instance owns concrete `locations` rows, not a canvas size of its
    #: own, so this is only ever a rendering convenience for the editor.
    grid_rows: int
    grid_cols: int
    slots: list[SlotStateRead]


class LocationChildViewUpdate(BaseModel):
    """Pin, or stop pinning, how this one container draws its children.

    The instance half of ADR 0006's override. There is no `PATCH /api/locations`
    to fold this into, and inventing one to carry a single field would put every
    other column of `locations` on the wire as writable — so this is its own
    narrow route, the same shape as `.../short-id` beside it.
    """

    child_view: ChildView | None = Field(
        default=None,
        description=(
            "Null clears the override, handing the drawing back to the container "
            "type (and through it to the derivation). Sending null is therefore a "
            "real edit, not an omission."
        ),
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class LocationChildViewResponse(ReplayableResponse):
    location_id: RowId
    #: What was stored: the caller's value, or null if the override was cleared.
    child_view: str | None
    #: What this container now actually draws with. Equal to `child_view` when one
    #: is set; the type's answer or the derived one when it was cleared — which is
    #: the whole reason this is reported rather than left for the client to guess.
    effective_child_view: str


class LocationGlyphUpdate(BaseModel):
    """Pin, or stop pinning, this one container's own pictogram. The instance
    half of the type/instance override — same shape and same reasoning as
    `LocationChildViewUpdate` beside it, one field over."""

    glyph: ContainerGlyph | None = Field(
        default=None,
        description=(
            "Null clears the override, handing the glyph back to the container "
            "type. Unlike `child_view` there is no derivation underneath it, so "
            "clearing both this and the type's own glyph is 'no glyph', drawn as "
            "a neutral placeholder rather than guessed at."
        ),
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class LocationGlyphResponse(ReplayableResponse):
    location_id: RowId
    #: What was stored: the caller's value, or null if the override was cleared.
    glyph: str | None
    #: This container's own glyph if set, else its container type's, else null.
    effective_glyph: str | None


class ShortIdRequest(BaseModel):
    """Mint (`short_id` absent) or adopt (`short_id` present)."""

    short_id: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "A code that is already printed on the label or written to the tag. "
            "Accepted in any human rendering — hyphenated, lower case, with a "
            "display prefix. Omit it to have one minted instead. The check symbol "
            "is verified, so a code mistyped off a label is refused rather than "
            "bound as itself."
        ),
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ShortIdResponse(ReplayableResponse):
    location_id: RowId
    short_id: str
    #: `BIN 4K7T-92M8` — for the confirmation toast, so what is read back matches
    #: what is printed on the card.
    display: str
    #: False when the code was minted, true when the caller's was bound. Lets the
    #: UI say "printed id assigned" versus "existing label adopted" rather than
    #: guessing from whether it echoed the request.
    adopted: bool
    #: Set when adoption superseded an earlier id. That id stays resolvable — a
    #: label already stuck to the drawer must keep working — so this is reported
    #: for the audit trail, not because anything was removed.
    previous_short_id: str | None


InstantiateResponse.model_rebuild()
ReapplyLayoutResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _require_location(db: Session, location_id: RowId, *, label: str = "location") -> Location:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": f"unknown_{label}", "message": f"no location with id {location_id}"},
        )
    return location


def _check_grid_compatibility(
    db: Session, parent: Location | None, child_type: ContainerType | None
) -> None:
    """Refuse a placement `capacity.grid_incompatibility()` flags.

    **Hard**, unlike every other capacity check in this API: a pitch mismatch
    or an oversized footprint is not a preference a defrag can tidy up later —
    a 42 mm bin does not physically seat on a 50 mm plate, so accepting the
    placement would record a world that cannot exist (ADR 0002).
    """
    if parent is None or child_type is None:
        return
    parent_type = (
        db.get(ContainerType, parent.container_type_id) if parent.container_type_id else None
    )
    incompatibility = capacity.grid_incompatibility(parent_type, child_type)
    if incompatibility is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": incompatibility,
                "message": (
                    f"{child_type.slug!r} cannot sit in {parent.name!r}'s grid: {incompatibility}"
                ),
            },
        )


def _capacity_read(db: Session, location: Location) -> CapacityRead:
    try:
        snapshot = capacity.compute_location_snapshot(db, location)
    except NotImplementedError:
        # The `mass` model is reserved for later and has no formula yet. Reporting
        # "unsupported" is better than a 500 on a bin somebody assigned it to.
        snapshot = capacity.CapacitySnapshot(
            model=CapacityModel.MASS,
            capacity=None,
            used=0.0,
            fill_ratio=None,
            is_full=False,
            unit="unsupported",
        )
    return CapacityRead(
        model=snapshot.model,
        capacity=snapshot.capacity,
        used=snapshot.used,
        fill_ratio=snapshot.fill_ratio,
        is_full=snapshot.is_full,
        is_overfull=location.is_overfull,
        unit=snapshot.unit,
    )


def _read(db: Session, location: Location) -> LocationRead:
    entity = describe(db, EntityType.LOCATION, location.id)
    container_type = (
        db.get(ContainerType, location.container_type_id)
        if location.container_type_id is not None
        else None
    )
    lots = list(
        db.execute(
            select(StockLot).where(StockLot.location_id == location.id).order_by(StockLot.id)
        )
        .scalars()
        .all()
    )
    child_count = db.execute(
        select(func.count()).select_from(Location).where(Location.parent_id == location.id)
    ).scalar_one()
    own_photo = primary_photo(db, entity_type=EntityType.LOCATION, entity_pk=location.id)
    type_photo = (
        primary_photo(db, entity_type=EntityType.CONTAINER_TYPE, entity_pk=container_type.id)
        if container_type is not None
        else None
    )
    return LocationRead(
        id=location.id,
        name=location.name,
        description=location.description,
        parent_id=location.parent_id,
        depth=location.depth,
        id_path=location.id_path,
        label_path=location.label_path,
        container_type_id=location.container_type_id,
        slot_label=location.slot_label,
        esd_safe=location.esd_safe,
        effective_esd_safe=location_tree(db).nearest_ancestor_value(location, "esd_safe"),
        is_placeable=location.is_placeable,
        child_view=location.child_view,
        effective_child_view=views.resolve_child_view(location, container_type),
        glyph=location.glyph,
        effective_glyph=glyphs.resolve_glyph(location, container_type),
        photo=own_photo,
        effective_photo=own_photo if own_photo is not None else type_photo,
        is_overfull=location.is_overfull,
        is_staging=location.is_staging,
        access_score=location.access_score,
        tare_mg=location.tare_mg,
        short_id=entity.short_id,
        display=entity.display,
        child_count=child_count,
        capacity=_capacity_read(db, location),
        lots=[lot_read(db, lot) for lot in lots],
        last_printed_at=location.last_printed_at,
    )


def _move_plan_read(plan: DefragPlan) -> MovePlanRead:
    return MovePlanRead(
        rationale=plan.rationale,
        steps=[
            MoveStepRead(
                lot_id=step.lot_id,
                from_location_id=step.from_location_id,
                to_location_id=step.to_location_id,
                qty_milli=step.qty_milli,
            )
            for step in plan.steps
        ],
    )


def _suggest_response(db: Session, result: AssignmentResult) -> SuggestResponse:
    chosen = _require_location(db, result.location_id)
    paths: dict[int, str] = {
        row[0]: row[1] for row in db.execute(select(Location.id, Location.label_path)).all()
    }
    return SuggestResponse(
        location_id=chosen.id,
        label_path=chosen.label_path,
        escalation_level=result.escalation_level,
        reason=result.reason,
        candidates=[
            CandidateRead(
                location_id=candidate.location_id,
                label_path=paths.get(candidate.location_id, ""),
                score=candidate.score,
                free_capacity=candidate.free_capacity,
            )
            for candidate in result.candidates
        ],
        defrag_plan=(None if result.defrag_plan is None else _move_plan_read(result.defrag_plan)),
        new_sibling_proposal=(
            None
            if result.new_sibling_proposal is None
            else NewSiblingRead(
                parent_id=result.new_sibling_proposal.parent_id,
                container_type_id=result.new_sibling_proposal.container_type_id,
                based_on_location_id=result.new_sibling_proposal.based_on_location_id,
            )
        ),
    )


def _slot_state_read(
    child: Location, *, short_id: str | None, has_tag: bool, lot_count: int, qty_milli: int
) -> SlotStateRead:
    row_idx = child.row_idx
    col_idx = child.col_idx
    if row_idx is None or col_idx is None:
        raise ValueError(f"location {child.id} is not a slot")  # pragma: no cover - caller filters
    return SlotStateRead(
        location_id=child.id,
        slot_label=child.slot_label or "",
        row_idx=row_idx,
        col_idx=col_idx,
        row_span=child.row_span,
        col_span=child.col_span,
        size_class=child.size_class,
        inner_volume_mm3=child.inner_volume_mm3,
        sort_order=child.sort_order,
        short_id=short_id,
        has_tag=has_tag,
        lot_count=lot_count,
        qty_milli=qty_milli,
    )


def _layout_read(db: Session, location: Location) -> LayoutRead:
    """Grid + tag + contents state for `location`'s own children — shared by
    the editor, the provisioning walk and the verification walk."""
    children = list(
        db.execute(
            select(Location)
            .where(Location.parent_id == location.id)
            .order_by(Location.sort_order, Location.id)
        ).scalars()
    )
    slots = [c for c in children if c.row_idx is not None and c.col_idx is not None]
    slot_ids = [c.id for c in slots]

    short_ids: dict[int, str] = {}
    tagged_ids: set[int] = set()
    totals: dict[int, tuple[int, int]] = {}
    if slot_ids:
        short_ids = {
            row[0]: row[1]
            for row in db.execute(
                select(ObjectId.entity_pk, ObjectId.short_id).where(
                    ObjectId.entity_type == EntityType.LOCATION,
                    ObjectId.entity_pk.in_(slot_ids),
                    ObjectId.is_primary.is_(True),
                )
            ).all()
        }
        tagged_ids = set(
            db.execute(select(LocationTag.location_id).where(LocationTag.location_id.in_(slot_ids)))
            .scalars()
            .all()
        )
        totals = {
            row.location_id: (row.lot_count, row.qty_milli)
            for row in db.execute(
                select(
                    StockLot.location_id.label("location_id"),
                    func.count().label("lot_count"),
                    func.coalesce(func.sum(StockLot.qty_milli_cached), 0).label("qty_milli"),
                )
                .where(StockLot.location_id.in_(slot_ids))
                .group_by(StockLot.location_id)
            ).all()
        }

    grid_rows = max((c.row_idx + c.row_span for c in slots if c.row_idx is not None), default=0)
    grid_cols = max((c.col_idx + c.col_span for c in slots if c.col_idx is not None), default=0)

    return LayoutRead(
        location_id=location.id,
        container_type_id=location.container_type_id,
        grid_rows=grid_rows,
        grid_cols=grid_cols,
        slots=[
            _slot_state_read(
                c,
                short_id=short_ids.get(c.id),
                has_tag=c.id in tagged_ids,
                lot_count=totals.get(c.id, (0, 0))[0],
                qty_milli=totals.get(c.id, (0, 0))[1],
            )
            for c in slots
        ],
    )


#: Unlike every other `LayoutError` reason, these three are geometry, not
#: authoring mistakes — ADR 0002 makes them a hard 409 rather than a 422,
#: matching `_check_grid_compatibility` below.
_GRID_CONFLICT_REASONS = {"pitch_mismatch", "footprint_too_wide", "footprint_too_deep"}


def _layout_error(error: LayoutError) -> HTTPException:
    code = (
        status.HTTP_409_CONFLICT
        if error.reason in _GRID_CONFLICT_REASONS
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(code, detail={"reason": error.reason, "message": str(error)})


def _guarded_change_error(guard: GuardedLayoutChange) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason": "slots_hold_content",
            "message": (
                "some slots in the new layout would be deleted but still hold stock or "
                "a bound tag; move contents to a holding location first"
            ),
            "affected_slots": [
                AffectedSlotRead(
                    location_id=affected.location_id,
                    slot_label=affected.slot_label,
                    reasons=list(affected.reasons),
                ).model_dump()
                for affected in guard.affected
            ],
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
# `/tree` and `/suggest` are declared before `/{location_id}`: a literal path and
# an `int` path parameter would otherwise race, and "tree" is not an int.


@router.get("/tree", response_model=LocationTree)
def read_location_tree(
    db: Session = Depends(get_db),
    root_id: int | None = None,
) -> LocationTree:
    """The whole tree, or one subtree.

    Subtree filtering is `id_path LIKE :prefix || '%'` — left-anchored, so the
    index on `id_path` serves it, and no recursion is involved at read time.
    """
    tree = location_tree(db)
    if root_id is None:
        nodes = sorted(tree.subtree_all(), key=lambda node: node.id_path)
    else:
        nodes = tree.subtree(_require_location(db, root_id))

    # One aggregate query for the whole tree rather than one per node: the totals
    # come from `stock_lots.qty_milli_cached`, so this is a sum over the cache.
    totals = {
        row.location_id: (row.lot_count, row.qty_milli)
        for row in db.execute(
            select(
                StockLot.location_id.label("location_id"),
                func.count().label("lot_count"),
                func.coalesce(func.sum(StockLot.qty_milli_cached), 0).label("qty_milli"),
            ).group_by(StockLot.location_id)
        ).all()
    }
    fill: dict[int, float | None] = {
        row[0]: row[1]
        for row in db.execute(
            select(LocationOccupancy.location_id, LocationOccupancy.fill_ratio)
        ).all()
    }
    # One more query for the whole tree, in the same spirit as the two above: the
    # drawing of a level falls back to its container type, and resolving that per
    # node would be one lookup per location. The picture and the canvas it is drawn
    # on come back together because they are read off the same type row.
    drawings = views.resolve_child_drawings(db, nodes)
    # And the glyph each node draws in this same map — batched for the identical
    # reason, and never the photo: see `LocationNode.effective_glyph`.
    node_glyphs = glyphs.resolve_glyphs(db, nodes)

    return LocationTree(
        nodes=[
            LocationNode(
                id=node.id,
                name=node.name,
                parent_id=node.parent_id,
                depth=node.depth,
                id_path=node.id_path,
                label_path=node.label_path,
                container_type_id=node.container_type_id,
                slot_label=node.slot_label,
                is_overfull=node.is_overfull,
                is_staging=node.is_staging,
                is_placeable=node.is_placeable,
                effective_child_view=drawings[node.id].view,
                child_grid_rows=drawings[node.id].grid_rows,
                child_grid_cols=drawings[node.id].grid_cols,
                effective_glyph=node_glyphs[node.id],
                fill_ratio=fill.get(node.id),
                lot_count=totals.get(node.id, (0, 0))[0],
                qty_milli=totals.get(node.id, (0, 0))[1],
            )
            for node in nodes
        ]
    )


@router.post("/suggest", response_model=SuggestResponse)
def suggest_location(request: SuggestRequest, db: Session = Depends(get_db)) -> SuggestResponse:
    """Propose where a new lot of a part should go. Workflow 1's ASSIGN step.

    Idempotency-guarded because this is not a pure read: one rung of the ladder
    *materialises* an empty grid cell, so a retried suggestion would otherwise
    leave a second empty cell behind every time the wifi dropped.

    Nothing here touches the ledger. Suggesting a destination and putting stock
    in it are separate steps, and only `/api/stock/receive` does the second.
    """
    part = db.get(Part, request.part_id)
    if part is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_part", "message": f"no part with id {request.part_id}"},
        )
    pitch: float | None = None
    if request.packaging_id is not None:
        packaging = db.get(Packaging, request.packaging_id)
        if packaging is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "reason": "unknown_packaging",
                    "message": f"no packaging with id {request.packaging_id}",
                },
            )
        pitch = packaging.pitch_mm

    def work() -> SuggestResponse:
        return _suggest_response(db, assignment.assign_location(db, part, packaging_pitch_mm=pitch))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/locations/suggest",
        payload=request,
        response_model=SuggestResponse,
        work=work,
    )


@router.post("", response_model=LocationCreated, status_code=status.HTTP_201_CREATED)
def create_location(request: LocationCreate, db: Session = Depends(get_db)) -> LocationCreated:
    """Add a container to the tree.

    The path cache is rebuilt immediately (`TreeRepository.insert_and_index`), so
    the new row comes back with a correct `label_path` rather than one that is
    right after the next nightly job.
    """
    parent = _require_location(db, request.parent_id, label="parent") if request.parent_id else None
    child_type = None
    if request.container_type_id is not None:
        child_type = db.get(ContainerType, request.container_type_id)
        if child_type is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "reason": "unknown_container_type",
                    "message": f"no container type with id {request.container_type_id}",
                },
            )
    _check_grid_compatibility(db, parent, child_type)

    def work() -> LocationCreated:
        location = Location(
            name=request.name,
            parent_id=request.parent_id,
            container_type_id=request.container_type_id,
            description=request.description,
            slot_label=request.slot_label,
            row_idx=request.row_idx,
            col_idx=request.col_idx,
            esd_safe=request.esd_safe,
            is_placeable=request.is_placeable,
            fill_factor=request.fill_factor,
            child_view=request.child_view,
            glyph=request.glyph,
            tare_mg=request.tare_mg,
        )
        if request.sort_order is not None:
            location.sort_order = request.sort_order
        if request.access_score is not None:
            location.access_score = request.access_score
        location_tree(db).insert_and_index(location)

        mint = request.mint_short_id
        if mint is None:
            mint = request.slot_label is None
        if mint:
            shortid.allocate(db, EntityType.LOCATION, location.id)
        return LocationCreated(location=_read(db, location))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/locations",
        payload=request,
        response_model=LocationCreated,
        work=work,
    )


@router.get("/{location_id}", response_model=LocationRead)
def read_location(location_id: RowId, db: Session = Depends(get_db)) -> LocationRead:
    """One container: its place in the tree, its fill state, and what is in it.

    This is the bin screen, which is also where "empty this bin into that one"
    starts from — hence the lots, and hence the capacity block alongside them.
    """
    return _read(db, _require_location(db, location_id))


@router.post(
    "/{location_id}/instantiate",
    response_model=InstantiateResponse,
    status_code=status.HTTP_201_CREATED,
)
def instantiate_containers(
    location_id: RowId, request: InstantiateRequest, db: Session = Depends(get_db)
) -> InstantiateResponse:
    """Bulk-create `count` instances of a container type under this location.

    Each instance materialises the type's *current* layout into its own child
    `locations` — never a live link back to the type, which is what keeps
    editing the type afterwards from touching anything created here.
    """
    parent = _require_location(db, location_id)
    container_type = db.get(ContainerType, request.container_type_id)
    if container_type is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_container_type",
                "message": f"no container type with id {request.container_type_id}",
            },
        )
    _check_grid_compatibility(db, parent, container_type)

    def work() -> InstantiateResponse:
        try:
            created = layout.instantiate(
                db,
                parent,
                container_type,
                count=request.count,
                naming_pattern=request.naming_pattern,
                tag_granularity=request.tag_granularity,
            )
        except LayoutError as error:
            raise _layout_error(error) from error
        return InstantiateResponse(locations=[_read(db, loc) for loc in created])

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/locations/{id}/instantiate",
        payload=request,
        response_model=InstantiateResponse,
        work=work,
    )


@router.post("/{location_id}/reapply-layout", response_model=ReapplyLayoutResponse)
def reapply_layout(
    location_id: RowId, request: ReapplyLayoutRequest, db: Session = Depends(get_db)
) -> ReapplyLayoutResponse:
    """Edit an already-instantiated location's own layout, through the change
    guard.

    Safe edits (relabel, size-class/volume, a scheme change that doesn't move
    a cell) apply outright. A slot that the new layout would delete but that
    still holds stock or a bound tag comes back as 409 with the full list of
    affected slots. Reusing an existing slot's label at a different grid
    position is refused outright (422) — see
    `app.services.layout_authoring.diff_instance_layout`.
    """
    location = _require_location(db, location_id)

    missing_labels = [item for item in request.slots if not item.slot_label]
    if missing_labels:
        # Unlike the type-level canvas, an instance has no stored generator to
        # fall back to — the client already has the current state from
        # `GET .../layout` and must say what every slot should be called.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "missing_slot_label",
                "message": (
                    "every slot needs an explicit slot_label when reapplying an instance's layout"
                ),
            },
        )

    def work() -> ReapplyLayoutResponse:
        desired = [
            SlotSpec(
                row_idx=item.row_idx,
                col_idx=item.col_idx,
                row_span=item.row_span,
                col_span=item.col_span,
                slot_label=item.slot_label or "",
                size_class=item.size_class,
                inner_volume_mm3=item.inner_volume_mm3,
            )
            for item in request.slots
        ]
        try:
            diff = layout.apply_layout_to_location(db, location, desired)
        except GuardedLayoutChange as guard:
            raise _guarded_change_error(guard) from guard
        except LayoutError as error:
            raise _layout_error(error) from error

        return ReapplyLayoutResponse(
            created=len(diff.creates),
            updated=len(diff.safe_updates),
            deleted=len(diff.deletes),
            layout=_layout_read(db, location),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/locations/{id}/reapply-layout",
        payload=request,
        response_model=ReapplyLayoutResponse,
        work=work,
    )


@router.get("/{location_id}/layout", response_model=LayoutRead)
def read_location_layout(location_id: RowId, db: Session = Depends(get_db)) -> LayoutRead:
    """Grid + tag + contents state for one location's own children — shared by
    the editor, the provisioning walk and the verification walk."""
    return _layout_read(db, _require_location(db, location_id))


@router.put("/{location_id}/child-view", response_model=LocationChildViewResponse)
def set_location_child_view(
    location_id: RowId, request: LocationChildViewUpdate, db: Session = Depends(get_db)
) -> LocationChildViewResponse:
    """Override how this container draws its children, or clear the override.

    Nothing about the tree's *shape* changes here and nothing is validated
    against the geometry on purpose: a drawing is a preference, and refusing to
    draw a cabinet as a floor plan because its type declares a grid would be the
    editor overruling the person holding the cabinet. The grid machinery still
    knows where each slot is either way — only the picture changes.
    """
    location = _require_location(db, location_id)

    def work() -> LocationChildViewResponse:
        location.child_view = request.child_view
        db.flush()
        container_type = (
            db.get(ContainerType, location.container_type_id)
            if location.container_type_id is not None
            else None
        )
        return LocationChildViewResponse(
            location_id=location.id,
            child_view=location.child_view,
            effective_child_view=views.resolve_child_view(location, container_type),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PUT /api/locations/{id}/child-view",
        payload=request,
        response_model=LocationChildViewResponse,
        work=work,
    )


@router.put("/{location_id}/glyph", response_model=LocationGlyphResponse)
def set_location_glyph(
    location_id: RowId, request: LocationGlyphUpdate, db: Session = Depends(get_db)
) -> LocationGlyphResponse:
    """Override this container's pictogram, or clear the override.

    Its own narrow route rather than a field on `.../child-view`, for the same
    reason that one is its own route rather than a general `PATCH /api/locations`
    — see `LocationChildViewUpdate`'s docstring.
    """
    location = _require_location(db, location_id)

    def work() -> LocationGlyphResponse:
        location.glyph = request.glyph
        db.flush()
        container_type = (
            db.get(ContainerType, location.container_type_id)
            if location.container_type_id is not None
            else None
        )
        return LocationGlyphResponse(
            location_id=location.id,
            glyph=location.glyph,
            effective_glyph=glyphs.resolve_glyph(location, container_type),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PUT /api/locations/{id}/glyph",
        payload=request,
        response_model=LocationGlyphResponse,
        work=work,
    )


@router.post("/{location_id}/short-id", response_model=ShortIdResponse)
def assign_location_short_id(
    location_id: RowId, request: ShortIdRequest, db: Session = Depends(get_db)
) -> ShortIdResponse:
    """Give this location a printed identity — minted, or one it already carries.

    Two orderings, one route, because they differ only in who chose the code:

    * **Promotion.** A generated grid cell starts with no printed id, since
      nobody sticks 96 labels on an 8x12 box. Send no `short_id` and one is
      minted, which is what "any cell can be promoted later" on
      `POST /api/locations` means. Already has one → that one comes back, so this
      is safe to call from a print button without checking first.
    * **Adoption.** Send a `short_id` and *that* code is bound, for pre-printed
      label stock, pre-encoded tags, or re-adopting a tag after restoring a
      backup older than the binding. The check symbol is verified and a collision
      is refused rather than substituted: the code is already on the object, so a
      substitute would put the label and the database permanently out of step.

    Adoption on a location that already has an id keeps the old one resolvable
    and makes the new one primary — the label still stuck to the drawer and the
    one in your hand both work, which is the point of relabelling being
    non-destructive.
    """
    location = _require_location(db, location_id)

    def work() -> ShortIdResponse:
        existing = shortid.primary_short_id(db, EntityType.LOCATION, location.id)
        if request.short_id is None:
            minted = existing or shortid.allocate(db, EntityType.LOCATION, location.id)
            return ShortIdResponse(
                location_id=location.id,
                short_id=minted,
                display=shortid.format_display(minted, EntityType.LOCATION),
                adopted=False,
                previous_short_id=None,
            )

        try:
            adopted = shortid.adopt(db, EntityType.LOCATION, location.id, request.short_id)
        except shortid.InvalidShortId as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "reason": error.reason,
                    "message": str(error),
                    "value": error.value,
                },
            ) from error
        except shortid.ShortIdTaken as error:
            held_by = describe_binding(db, error)
            # The path goes in `message` as well as `held_by`, so a client that
            # only renders the standard `{reason, message}` still tells the user
            # which drawer to walk to. Machine-readable extras are additive; the
            # human sentence has to stand alone.
            message = (
                str(error)
                if held_by is None
                else f"{shortid.format_display(error.short_id)} is already on {held_by}"
            )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "short_id_taken",
                    "message": message,
                    "short_id": error.short_id,
                    "held_by": held_by,
                },
            ) from error

        return ShortIdResponse(
            location_id=location.id,
            short_id=adopted,
            display=shortid.format_display(adopted, EntityType.LOCATION),
            adopted=True,
            previous_short_id=existing if existing != adopted else None,
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/locations/{id}/short-id",
        payload=request,
        response_model=ShortIdResponse,
        work=work,
    )


def describe_binding(db: Session, error: shortid.ShortIdTaken) -> str | None:
    """What already holds the refused code, in words a person can act on.

    "Already bound to Cabinet A / Drawer B2" tells you which drawer to go look
    at; "already bound to location 41" makes you go and query for it. Only
    locations are resolved to a path, since that is the type this route binds and
    the one whose identity is physical.
    """
    if error.entity_type != EntityType.LOCATION or error.entity_pk is None:
        return None
    holder = db.get(Location, error.entity_pk)
    return holder.label_path if holder is not None else None
