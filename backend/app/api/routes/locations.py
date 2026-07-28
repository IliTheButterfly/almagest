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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import MassMg, RowId
from app.api.schemas import LotRead, ReplayableResponse, lot_read
from app.db.session import get_db
from app.models.catalog import Packaging, Part
from app.models.enums import CapacityModel, EntityType
from app.models.stock import StockLot
from app.models.storage import ContainerType, Location, LocationOccupancy
from app.services import assignment, capacity, shortid
from app.services.assignment import AssignmentResult
from app.services.capacity import DefragPlan
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
    is_overfull: bool
    is_staging: bool
    access_score: float
    tare_mg: int | None
    short_id: str | None
    display: str | None
    child_count: int
    capacity: CapacityRead
    lots: list[LotRead]


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
        is_overfull=location.is_overfull,
        is_staging=location.is_staging,
        access_score=location.access_score,
        tare_mg=location.tare_mg,
        short_id=entity.short_id,
        display=entity.display,
        child_count=child_count,
        capacity=_capacity_read(db, location),
        lots=[lot_read(db, lot) for lot in lots],
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
    if request.parent_id is not None:
        _require_location(db, request.parent_id, label="parent")
    if (
        request.container_type_id is not None
        and db.get(ContainerType, request.container_type_id) is None
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_container_type",
                "message": f"no container type with id {request.container_type_id}",
            },
        )

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
