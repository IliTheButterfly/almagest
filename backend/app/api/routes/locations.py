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
from app.api.limits import (
    PLAN_MAX_PLACEMENTS,
    PLAN_MAX_POINTS,
    PLAN_MAX_SHAPES,
    PLAN_MIN_POINTS,
    InstanceCount,
    MassMg,
    PlanCoordMm,
    PlanExtentMm,
    PlanRotationDeg,
    RowId,
)
from app.api.routes.documents import DocumentRead, primary_photo
from app.api.schemas import LotRead, ReplayableResponse, SlotSpecIn, lot_read
from app.db.session import get_db
from app.models.catalog import Packaging, Part
from app.models.enums import (
    CapacityModel,
    ChildView,
    ContainerGlyph,
    EntityType,
    PlanShapeKind,
    TagGranularity,
)
from app.models.identity import ObjectId
from app.models.stock import StockLot
from app.models.storage import ContainerType, Location, LocationOccupancy, LocationTag
from app.services import assignment, capacity, glyphs, removal, room_plan, shortid, views
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


# --- ADR 0009: drawn rooms and placed containers ---------------------------


class PlanPoint(BaseModel):
    """One vertex, in the room's own millimetres. Signed — see `PlanCoordMm`."""

    x_mm: PlanCoordMm
    y_mm: PlanCoordMm


class PlanShapeIn(BaseModel):
    """One drawn line as the editor sends it: a wall, a door, the bench.

    **No `id`.** The whole plan is replaced on every save, so the client never
    holds shape ids and redrawing a wall is not a diff. See
    `app.services.room_plan.replace_shapes`.
    """

    kind: PlanShapeKind
    points: list[PlanPoint] = Field(min_length=PLAN_MIN_POINTS, max_length=PLAN_MAX_POINTS)
    label: str | None = Field(default=None, max_length=255)
    is_closed: bool = False
    thickness_mm: PlanExtentMm | None = Field(
        default=None,
        description=(
            "Stroke width — a 100 mm stud wall is not a hairline. Null lets the "
            "renderer pick a nominal width for the kind, which is honest: nobody "
            "measures the thickness of a door swing."
        ),
    )


class PlanShapeRead(BaseModel):
    #: Assigned on save and **not stable across saves**, because a save replaces
    #: the plan. Nothing references it: it is not a `short_id`, it is never
    #: printed, and no tag carries one.
    id: int
    kind: str
    points: list[PlanPoint]
    label: str | None
    is_closed: bool
    thickness_mm: int | None
    sort_order: int


class PlacementIn(BaseModel):
    """Drop one child at a coordinate in this room."""

    location_id: RowId
    x_mm: PlanCoordMm
    y_mm: PlanCoordMm
    rotation_deg: PlanRotationDeg = 0
    width_mm: PlanExtentMm | None = Field(
        default=None,
        description=(
            "The footprint as drawn, overriding the container type's physical "
            "size. Null takes the type's, which is the common case."
        ),
    )
    depth_mm: PlanExtentMm | None = None


class PlacementRead(BaseModel):
    location_id: int
    #: The parent these coordinates belong to. Always equal to the location's
    #: current `parent_id` — a placement authored against a different parent is
    #: not reported at all, it is reported as unplaced (ADR 0009).
    parent_id: int
    x_mm: int
    y_mm: int
    rotation_deg: int
    #: The drawn footprint, else the container type's, else null. Null means
    #: "draw a nominal box" — **never zero**, which would draw nothing. This is
    #: the pair to *draw*; it is not the pair to send back.
    width_mm: int | None
    depth_mm: int | None
    #: What this placement itself says, with no fallback — null for "use the
    #: container type's size". Reported beside the resolved pair for the same
    #: reason ADR 0006 reports `child_view` beside `effective_child_view`: an
    #: editor handed only the resolved number cannot tell an authored size from
    #: an inherited one, so it sends the type's size back as an override and
    #: silently freezes it. **This is the pair an editor round-trips.**
    own_width_mm: int | None
    own_depth_mm: int | None


class PlanExtentRead(BaseModel):
    """Bounding box of everything drawn and placed. Derived, never stored."""

    min_x_mm: int
    min_y_mm: int
    max_x_mm: int
    max_y_mm: int


class RoomPlanRead(BaseModel):
    """One room's drawing: its outline, its furniture, and where things stand.

    The floor-plan counterpart of `LayoutRead`, and deliberately a separate
    route from it: a slot canvas and a drawn room are different pictures with no
    shared field, and merging them would give every client a response where half
    the shape is always null.
    """

    location_id: RowId
    shapes: list[PlanShapeRead]
    placements: list[PlacementRead]
    #: Children with no valid placement — added to the room and never dragged
    #: anywhere, or dragged in a *different* room and moved here since. A real
    #: state that the client renders as an unplaced tray, not an error and not a
    #: container silently sitting at the origin.
    unplaced_location_ids: list[int]
    #: Null for a room with nothing drawn and nothing placed. Reportable because
    #: a default canvas would make the client draw a box that is not there.
    extent: PlanExtentRead | None


class RoomPlanShapesUpdate(BaseModel):
    """Replace this location's entire drawn plan.

    **One request for the whole drawing**, not a shape at a time: a drawing
    session ends with "this is the room now", and a stream of inserts and deletes
    whose order matters cannot half-apply safely.
    """

    shapes: list[PlanShapeIn] = Field(max_length=PLAN_MAX_SHAPES)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class RoomPlanShapesResponse(ReplayableResponse):
    location_id: RowId
    shapes: list[PlanShapeRead]
    extent: PlanExtentRead | None


class RoomPlacementsUpdate(BaseModel):
    """Save where several children now stand, in **one** request.

    Dragging five cabinets around and then saving is one write. Per-placement
    routes would make a five-box rearrangement five requests that can partially
    fail, leaving the room in a state nobody authored.
    """

    placements: list[PlacementIn] = Field(max_length=PLAN_MAX_PLACEMENTS)
    #: Children to return to the unplaced tray. Separate from `placements`
    #: because "not placed" is a real state and there is no coordinate that
    #: expresses it — sending (0, 0) would put the box in a corner instead.
    unplace_location_ids: list[RowId] = Field(default_factory=list, max_length=PLAN_MAX_PLACEMENTS)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class RoomPlacementsResponse(ReplayableResponse):
    location_id: RowId
    placements: list[PlacementRead]
    unplaced_location_ids: list[int]
    extent: PlanExtentRead | None


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
    #: Where this container stands in its parent's floor plan (ADR 0009), or null
    #: — which covers "never dragged anywhere", "moved to another room since it
    #: was placed", and "this is a root". All three are drawn the same way,
    #: because the fix for all three is the same gesture.
    placement: PlacementRead | None
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
    #: Set when this container was **removed but could not be deleted** — the
    #: ledger, a printed label or a stuck-on tag names it, so the row and its
    #: history stay while the container leaves the tree (`app.services.removal`).
    #: Reported on the detail screen rather than hidden, because this screen is
    #: where a tapped tag on a removed drawer lands, and "this is gone" has to be
    #: something it can actually say. Null for every live container.
    retired_at: datetime | None


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
    #: Set for a container that was removed but could not be deleted — see
    #: `LocationRead.retired_at`. **Always null unless `include_retired=true`**,
    #: because a retired container is not part of the tree; the field exists so
    #: the one view that asks for them can tell them apart from the living.
    retired_at: datetime | None


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


# --- removing a container (app.services.removal) ---------------------------


class RemovalBlockerRead(BaseModel):
    """One thing standing in the way, and **what is inside it**.

    `detail` carries the actual contents — "470 x C0603C104K (lot 12)" — because
    a refusal that does not name what is in the drawer tells the user nothing
    they can act on, and "constraint failed" is not an answer.
    """

    reason: str
    location_id: RowId
    label: str
    label_path: str
    detail: str


class RemovalNodeRead(BaseModel):
    """What removing this container would do to one node of its subtree."""

    location_id: RowId
    label: str
    label_path: str
    #: `delete` — the row goes — or `retire`: the row and its history stay, and
    #: the container leaves the tree. Never anything else, and never guessed.
    action: str
    #: Why it cannot simply be deleted: `has_lots`, `in_ledger`, `printed`,
    #: `bound_tag`, `pinned_by_child`. Empty exactly when `action == "delete"`.
    pins: list[str]


class RemovalPreview(BaseModel):
    """A dry run of `DELETE /api/locations/{id}`, derived from the same plan.

    The confirm dialog reads this rather than deciding for itself, so it cannot
    promise an outcome the delete then refuses — and so the words "this cannot be
    undone" are only ever shown when they are true.
    """

    location_id: RowId
    removable: bool
    #: Non-empty exactly when `removable` is false.
    blockers: list[RemovalBlockerRead]
    reason: str | None
    message: str | None
    nodes: list[RemovalNodeRead]
    #: How many containers sit inside this one. Non-zero means `recursive` is
    #: required, and the preview says which ones would go with it.
    descendant_count: int


class LocationRemoved(BaseModel):
    """What actually happened, split by outcome rather than summarised.

    Two lists rather than one count, because the two are different promises: a
    deleted id is gone and a retired one is recoverable, and the UI has to be
    able to say which without asking again.
    """

    location_id: RowId
    deleted_location_ids: list[int]
    retired_location_ids: list[int]
    nodes: list[RemovalNodeRead]


class LocationRestored(BaseModel):
    """A retirement undone.

    `restored_location_ids` covers the whole retired subtree: retiring a cabinet
    retired its drawers, so restoring the cabinet alone would leave them
    stranded, invisible, inside a visible container.
    """

    location_id: RowId
    restored_location_ids: list[int]
    #: True when the container came back without a position, which is the normal
    #: outcome: retiring cleared its slot cell and its floor-plan coordinate, and
    #: silently reclaiming a cell somebody has laid out since is exactly the
    #: "redefine what a label already means" failure the layout guard prevents.
    unplaced: bool


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
    #: Whether a card has actually been printed for this slot, as opposed to a
    #: code merely having been minted for it. The two are very different: with
    #: `tag_granularity="slot"` every slot gets a `short_id` at instantiation,
    #: so `short_id is not None` says nothing about whether anything physical
    #: exists — and the editor used it to warn "the card printed for this slot
    #: will stop working" on deletions where no card had ever been printed. A
    #: location cannot claim to be printed on its own say-so; this is the same
    #: `last_printed_at` the "never printed" badge reads.
    last_printed_at: datetime | None
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


class LocationDetailsUpdate(BaseModel):
    """What a person can rename or re-describe about a container, in place.

    The write half of the storage screen's **edit mode**: name, description, and
    the two tri-state flags that read as sentences on that panel. Its own narrow
    route for the same reason `.../child-view` and `.../glyph` are — a general
    `PATCH /api/locations/{id}` would put `parent_id`, `slot_label`, `row_idx`
    and every other structural column on the wire as writable, and each of those
    has a guarded path of its own (`.../reapply-layout`, `TreeRepository.move`)
    that a free-for-all patch would let a client bypass.

    **Every field is sent every time**, which is why this is a PUT: a panel with
    a "Description" box in it that is now blank means "no description", and there
    is no way to tell that from a PATCH that simply omitted the key.
    """

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(
        default=None, description="Null and empty both mean 'no description'."
    )
    esd_safe: bool | None = Field(
        default=None,
        description=(
            "Null stops this container answering for itself and inherits from the "
            "nearest ancestor that does — so sending null is a real edit."
        ),
    )
    is_placeable: bool | None = Field(
        default=None,
        description="Null hands the answer back to the container type.",
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class LocationDetailsResponse(ReplayableResponse):
    #: The whole container, re-read. A rename changes `label_path` here *and* on
    #: every descendant, so returning the one row that was written would leave
    #: the caller holding a stale path for everything under it.
    location: LocationRead


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


def _require_live_parent(db: Session, location_id: RowId, *, label: str = "parent") -> Location:
    """A container something may be created *inside*.

    A retired parent is refused, because the tree read's own filter depends on it
    never happening: `read_location_tree` drops a retired node per row and says
    so on the grounds that "a retired node's descendants are retired too", which
    `removal.restore` enforces from the other end. A live child under a retired
    parent breaks that — the child comes back from `/tree` while its parent does
    not, so `indexTree` re-roots it and it renders as a top-level container whose
    `label_path` still names the cabinet somebody removed. Auto-assignment would
    then propose it as somewhere to put stock.
    """
    parent = _require_location(db, location_id, label=label)
    if parent.retired_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "parent_retired",
                "message": (
                    f"{parent.name} was removed from the storage tree, so nothing can be added"
                    " inside it. Bring it back first."
                ),
            },
        )
    return parent


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
        # From the snapshot, not the column. The column is the *scorer's* input
        # and is written only by the bulk pass, so serving it beside a live
        # `used`/`fill_ratio` produced one payload contradicting itself —
        # `fill_ratio 2.0, is_full true, is_overfull false` — while the field is
        # documented as "capacity is literally exceeded".
        is_overfull=snapshot.is_overfull,
        unit=snapshot.unit,
    )


def _placement_read(placement: room_plan.Placement) -> PlacementRead:
    return PlacementRead(
        location_id=placement.location_id,
        parent_id=placement.parent_id,
        x_mm=placement.x_mm,
        y_mm=placement.y_mm,
        rotation_deg=placement.rotation_deg,
        width_mm=placement.width_mm,
        depth_mm=placement.depth_mm,
        own_width_mm=placement.own_width_mm,
        own_depth_mm=placement.own_depth_mm,
    )


def _extent_read(extent: room_plan.Extent | None) -> PlanExtentRead | None:
    if extent is None:
        return None
    return PlanExtentRead(
        min_x_mm=extent.min_x_mm,
        min_y_mm=extent.min_y_mm,
        max_x_mm=extent.max_x_mm,
        max_y_mm=extent.max_y_mm,
    )


def _shape_read(shape: room_plan.Shape) -> PlanShapeRead:
    return PlanShapeRead(
        id=shape.id,
        kind=shape.kind,
        points=[PlanPoint(x_mm=point.x_mm, y_mm=point.y_mm) for point in shape.points],
        label=shape.label,
        is_closed=shape.is_closed,
        thickness_mm=shape.thickness_mm,
        sort_order=shape.sort_order,
    )


def _plan_read(db: Session, parent: Location) -> RoomPlanRead:
    """Everything drawn on, and standing in, one location.

    Placed and unplaced children are split here rather than left to the client,
    because the split is exactly the `plan_parent_id == parent_id` rule and there
    must be one implementation of it (`room_plan.placement_of`).
    """
    shapes = room_plan.shapes_of(db, parent)
    placements: list[room_plan.Placement] = []
    unplaced: list[int] = []
    for child in room_plan.children_of(db, parent):
        placement = room_plan.placement_of(db, child)
        if placement is None:
            unplaced.append(child.id)
        else:
            placements.append(placement)
    return RoomPlanRead(
        location_id=parent.id,
        shapes=[_shape_read(shape) for shape in shapes],
        placements=[_placement_read(placement) for placement in placements],
        unplaced_location_ids=unplaced,
        extent=_extent_read(room_plan.extent(shapes, placements)),
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
    # Every part these lots name, in one query, handed to `lot_read` directly.
    #
    # Not left to the identity map: a request session starts empty, so the first
    # read of each distinct part is a query — a drawer holding a 4k7 and a 10k
    # costs two, which is exactly the case this screen exists to disambiguate.
    # `StockLot` has no `part` relationship to eager-load through,
    # and adding one for a rendering concern is a model change; an `IN` and a
    # dict is local and obvious.
    parts_by_id = {
        part.id: part
        for part in db.execute(select(Part).where(Part.id.in_({lot.part_id for lot in lots})))
        .scalars()
        .all()
    }
    # Retired children are excluded: `child_count` drives "N slot(s) laid out
    # here" and whether the client fetches a subtree at all, and a container that
    # has left the tree must not keep its parent claiming to hold something.
    child_count = db.execute(
        select(func.count())
        .select_from(Location)
        .where(Location.parent_id == location.id, Location.retired_at.is_(None))
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
        placement=(
            _placement_read(placement)
            if (placement := room_plan.placement_of(db, location)) is not None
            else None
        ),
        access_score=location.access_score,
        tare_mg=location.tare_mg,
        short_id=entity.short_id,
        display=entity.display,
        child_count=child_count,
        capacity=_capacity_read(db, location),
        lots=[lot_read(db, lot, parts_by_id.get(lot.part_id)) for lot in lots],
        last_printed_at=location.last_printed_at,
        retired_at=location.retired_at,
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
        last_printed_at=child.last_printed_at,
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
    include_retired: bool = False,
) -> LocationTree:
    """The whole tree, or one subtree.

    Subtree filtering is `id_path LIKE :prefix || '%'` — left-anchored, so the
    index on `id_path` serves it, and no recursion is involved at read time.

    **Retired containers are excluded by default** (`app.services.removal`): a
    container whose row the ledger pins but which the user has removed is not part
    of the storage tree any more, and leaving it in would make "remove" mean
    nothing. `include_retired=true` is for the one screen that offers to restore
    them; a retired node's descendants are retired too, so the filter is per node
    and needs no subtree arithmetic.
    """
    tree = location_tree(db)
    if root_id is None:
        nodes = sorted(tree.subtree_all(), key=lambda node: node.id_path)
    else:
        nodes = tree.subtree(_require_location(db, root_id))
    if not include_retired:
        nodes = [node for node in nodes if node.retired_at is None]

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
                retired_at=node.retired_at,
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
    parent = _require_live_parent(db, request.parent_id) if request.parent_id else None
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


# ---------------------------------------------------------------------------
# Removing a container
# ---------------------------------------------------------------------------


def _blocker_read(blocker: removal.Blocker) -> RemovalBlockerRead:
    return RemovalBlockerRead(
        reason=blocker.reason,
        location_id=blocker.location_id,
        label=blocker.label,
        label_path=blocker.label_path,
        detail=blocker.detail,
    )


def _node_read(node: removal.NodePlan) -> RemovalNodeRead:
    return RemovalNodeRead(
        location_id=node.location_id,
        label=node.label,
        label_path=node.label_path,
        action=node.action,
        pins=list(node.pins),
    )


def _removal_conflict(error: removal.RemovalRefused) -> HTTPException:
    """A refusal, carrying the list of what is in the way.

    409 rather than 422 for both reasons this can fire: neither is a malformed
    request. The drawer really does hold stock, or really does have containers in
    it, and the fix is out here in the workshop.
    """
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason": error.reason,
            "message": error.message,
            "blockers": [_blocker_read(blocker).model_dump() for blocker in error.blockers],
        },
    )


@router.get("/{location_id}/removal", response_model=RemovalPreview)
def preview_location_removal(
    location_id: RowId,
    recursive: bool = False,
    db: Session = Depends(get_db),
) -> RemovalPreview:
    """What removing this container would do — a dry run, writing nothing.

    The confirm dialog reads this so that it and the delete derive their answer
    from one function (`app.services.removal.plan_removal`). Without it the dialog
    would have to guess between "this is permanent" and "this can be undone", and
    it would guess wrong for exactly the drawers where being wrong matters: the
    ones with history behind them.

    A refusal comes back as a 200 with `removable: false`, not a 409 — nothing was
    attempted, so there is nothing to refuse; the caller asked a question and this
    is the answer.
    """
    location = _require_location(db, location_id)
    descendants = len(location_tree(db).subtree(location, include_self=False))
    try:
        plan = removal.plan_removal(db, location, recursive=recursive)
    except removal.RemovalRefused as error:
        return RemovalPreview(
            location_id=location.id,
            removable=False,
            blockers=[_blocker_read(blocker) for blocker in error.blockers],
            reason=error.reason,
            message=error.message,
            nodes=[],
            descendant_count=descendants,
        )
    return RemovalPreview(
        location_id=location.id,
        removable=True,
        blockers=[],
        reason=None,
        message=None,
        nodes=[_node_read(node) for node in plan.nodes],
        descendant_count=descendants,
    )


@router.delete("/{location_id}", response_model=LocationRemoved)
def remove_location(
    location_id: RowId,
    recursive: bool = False,
    db: Session = Depends(get_db),
) -> LocationRemoved:
    """Remove a container. **Deletes it if nothing names it, retires it if
    something does, and refuses if stock is inside.**

    Which of the three happens per node is decided by
    `app.services.removal.plan_removal` and reported back rather than summarised,
    because the three are different promises and the UI has to be able to say
    which one it got. The full reasoning lives in that module; the short version:

    * `stock_lots.location_id` and `stock_ledger.{from,to}_location_id` are
      `RESTRICT` against tables nothing deletes from, so a drawer that ever held
      anything cannot be deleted, ever. It is retired instead: the row and every
      ledger entry naming it stay untouched, and the container leaves the tree,
      the room plan, its parent's slot canvas and auto-assignment.
    * A container holding actual stock is refused, and the refusal names the
      lots. Relocating them is a ledger movement and the user's decision, so
      nothing here does it silently.
    * `recursive=false` on a container with children is refused and names them.
      Deleting a cabinet is never an accident of deleting a cabinet.
    """
    location = _require_location(db, location_id)
    try:
        plan = removal.apply_removal(db, removal.plan_removal(db, location, recursive=recursive))
    except removal.RemovalRefused as error:
        raise _removal_conflict(error) from error
    db.commit()
    return LocationRemoved(
        location_id=location_id,
        deleted_location_ids=list(plan.deleted_ids),
        retired_location_ids=list(plan.retired_ids),
        nodes=[_node_read(node) for node in plan.nodes],
    )


@router.post("/{location_id}/restore", response_model=LocationRestored)
def restore_location(location_id: RowId, db: Session = Depends(get_db)) -> LocationRestored:
    """Undo a retirement — for this container and everything retired under it.

    Only a retirement is undoable. A deleted container is gone, and the UI says
    so plainly before it happens rather than offering an undo that cannot exist.
    """
    location = _require_location(db, location_id)
    try:
        restored = removal.restore(db, location)
    except removal.RestoreRefused as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": error.reason, "message": error.message},
        ) from error
    db.commit()
    return LocationRestored(
        location_id=location_id,
        restored_location_ids=[loc.id for loc in restored],
        unplaced=room_plan.placement_of(db, location) is None and location.slot_label is None,
    )


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
    parent = _require_live_parent(db, location_id, label="location")
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


@router.get("/{location_id}/plan", response_model=RoomPlanRead)
def read_location_plan(location_id: RowId, db: Session = Depends(get_db)) -> RoomPlanRead:
    """This location's drawn room — outline, furniture, and where things stand.

    The floor-plan sibling of `GET /{id}/layout`, which answers the *slot canvas*
    question. Two routes rather than one because a room and a grid are different
    pictures sharing no field; a merged response would be half null for everyone.

    **Never a 404 for an undrawn room.** A location with nothing drawn and nothing
    placed answers with empty lists and a null `extent`, which is what the editor
    needs in order to be the thing you draw the first wall in.
    """
    return _plan_read(db, _require_location(db, location_id))


@router.put("/{location_id}/plan/shapes", response_model=RoomPlanShapesResponse)
def set_location_plan_shapes(
    location_id: RowId, request: RoomPlanShapesUpdate, db: Session = Depends(get_db)
) -> RoomPlanShapesResponse:
    """Replace this location's drawn plan — walls, doors, benches — in one write.

    **A drawn wall is not a location** (ADR 0009): it gets no `short_id`, holds no
    stock and never appears in the tree, so nothing here touches `locations`.
    Sending an empty list erases the drawing, which is a real edit rather than an
    omission — same convention as clearing a `child_view` override.

    Nothing is validated against the location's `child_view`. Drawing a room on a
    container that renders as a cabinet face is allowed and simply unused, for the
    reason ADR 0006 gives: refusing would be the editor overruling the person
    holding the furniture.
    """
    location = _require_location(db, location_id)

    def work() -> RoomPlanShapesResponse:
        shapes = room_plan.replace_shapes(
            db,
            location,
            [
                room_plan.ShapeDraft(
                    kind=shape.kind,
                    points=[
                        room_plan.Point(x_mm=point.x_mm, y_mm=point.y_mm) for point in shape.points
                    ],
                    label=shape.label,
                    is_closed=shape.is_closed,
                    thickness_mm=shape.thickness_mm,
                )
                for shape in request.shapes
            ],
        )
        placements = [
            placement
            for child in room_plan.children_of(db, location)
            if (placement := room_plan.placement_of(db, child)) is not None
        ]
        return RoomPlanShapesResponse(
            location_id=location.id,
            shapes=[_shape_read(shape) for shape in shapes],
            extent=_extent_read(room_plan.extent(shapes, placements)),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PUT /api/locations/{id}/plan/shapes",
        payload=request,
        response_model=RoomPlanShapesResponse,
        work=work,
    )


@router.put("/{location_id}/plan/placements", response_model=RoomPlacementsResponse)
def set_location_plan_placements(
    location_id: RowId, request: RoomPlacementsUpdate, db: Session = Depends(get_db)
) -> RoomPlacementsResponse:
    """Save where several children now stand, in **one** request.

    Dragging five cabinets around the room and then saving is one write. Per-child
    routes would make that five requests that can partially fail, leaving a room
    in a state nobody authored.

    Every id must be a current child of this location — a coordinate authored
    against one room is meaningless in another, so placing something that is not
    in the room is a 422 rather than a coordinate that would be ignored on read
    anyway. Ids sent in both `placements` and `unplace_location_ids` are the same
    refusal: the request contradicts itself, and guessing which half was meant is
    how a drag gets silently discarded.
    """
    location = _require_location(db, location_id)
    placed_ids = [item.location_id for item in request.placements]
    both = sorted(set(placed_ids) & set(request.unplace_location_ids))
    if both:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "placed_and_unplaced",
                "message": (f"these locations are both placed and unplaced in one request: {both}"),
                "location_ids": both,
            },
        )
    duplicates = sorted({row for row in placed_ids if placed_ids.count(row) > 1})
    if duplicates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "duplicate_placement",
                "message": f"these locations are placed more than once: {duplicates}",
                "location_ids": duplicates,
            },
        )

    children = {child.id: child for child in room_plan.children_of(db, location)}
    strangers = sorted(
        {row for row in [*placed_ids, *request.unplace_location_ids] if row not in children}
    )
    if strangers:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "not_a_child",
                "message": (
                    f"these locations are not children of location {location.id}: {strangers}"
                ),
                "location_ids": strangers,
            },
        )

    def work() -> RoomPlacementsResponse:
        for item in request.placements:
            room_plan.place(
                children[item.location_id],
                parent_id=location.id,
                x_mm=item.x_mm,
                y_mm=item.y_mm,
                rotation_deg=item.rotation_deg,
                width_mm=item.width_mm,
                depth_mm=item.depth_mm,
            )
        for stale_id in request.unplace_location_ids:
            room_plan.forget_placement(children[stale_id])
        db.flush()
        plan = _plan_read(db, location)
        return RoomPlacementsResponse(
            location_id=location.id,
            placements=plan.placements,
            unplaced_location_ids=plan.unplaced_location_ids,
            extent=plan.extent,
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PUT /api/locations/{id}/plan/placements",
        payload=request,
        response_model=RoomPlacementsResponse,
        work=work,
    )


@router.put("/{location_id}/details", response_model=LocationDetailsResponse)
def set_location_details(
    location_id: RowId, request: LocationDetailsUpdate, db: Session = Depends(get_db)
) -> LocationDetailsResponse:
    """Rename and re-describe a container where it stands.

    A rename is the one edit here with a consequence beyond the row: `label_path`
    is a cache of the names down the chain, so renaming a cabinet restates the
    path of every drawer in it. That goes through `TreeRepository.rebuild_paths`
    rather than any hand-written string surgery — the cache is reconstructible
    from `parent_id` and `name` by exactly one recursive CTE, and a second way to
    compute it is a second way to be wrong.

    Nothing physical changes: no `short_id` is re-minted, no tag is touched and
    nothing is re-printed. A printed label carries the opaque code and never the
    name, which is precisely what makes renaming free.
    """
    location = _require_location(db, location_id)

    def work() -> LocationDetailsResponse:
        renamed = location.name != request.name
        location.name = request.name
        description = request.description
        # A box the user cleared is "no description", not the empty string —
        # otherwise the read side has two falsy values meaning one thing.
        location.description = (
            None if description is None or not description.strip() else description
        )
        location.esd_safe = request.esd_safe
        location.is_placeable = request.is_placeable
        db.flush()
        if renamed:
            location_tree(db).rebuild_paths()
        return LocationDetailsResponse(location=_read(db, location))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PUT /api/locations/{id}/details",
        payload=request,
        response_model=LocationDetailsResponse,
        work=work,
    )


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
