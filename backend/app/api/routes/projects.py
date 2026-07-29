"""`/api/projects` and `/api/builds` — projects, BOMs, builds, and allocations.

Three routers because the resources nest two different ways: a build is
addressed under its own id everywhere *except* creation, which has to be under
its project (`build_no` is assigned per project). Kept in one module anyway —
splitting projects from builds from allocations would scatter one workflow
(import a BOM, plan a build, allocate stock, release it) across three files
for no reader's benefit, the same call `stock.py` makes for movements.

**What this module does not do**: it never writes `stock_ledger` directly, and
it never touches `qty_reserved_milli_cached` by hand. `app.services.reservations`
is the sole writer for both, exactly as `app.services.ledger` is for the ledger
— a route here resolves ids into objects, calls the service, and renders the
result. `POST /allocate` and `POST /release` are movements in the sense that
matters (they change what a lot's stock is promised to), so both go through
`app.api.idempotency` the same way `stock.py`'s do. `PATCH` on a project or a
build is not — replaying it sets the same fields to the same values — which is
why it is unguarded, matching `parts.update_part`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import AssemblyCount, QtyMilli, RowId
from app.api.schemas import LotRead, ReplayableResponse, lot_read
from app.db.session import get_db
from app.models.catalog import Part
from app.models.enums import BuildStatus, ProjectStatus
from app.models.projects import BomLine, Project, ProjectBuild, StockAllocation
from app.models.stock import StockLot
from app.models.types import utcnow
from app.services.bom_import import import_bom as run_bom_import
from app.services.bom_import import normalized_mpn, parse_bom
from app.services.reservations import (
    LineShortage,
    ReservationError,
    release_build,
    reserve,
    shortage_for_build,
)
from app.services.reservations import release as release_allocation

router = APIRouter(prefix="/api/projects", tags=["projects"])
builds_router = APIRouter(prefix="/api/builds", tags=["projects"])

#: A BOM as CSV/TSV text arrives as a JSON string field rather than a multipart
#: upload — every other write in this API is JSON, and a file field would be
#: the one endpoint whose generated client method looks nothing like its
#: neighbours. `parse_bom` accepts `str` directly, so no decoding step is lost.
#: The bound is generous by two orders of magnitude over any real export (a
#: thousand-line BOM with wide columns is a few hundred KB) and exists only so
#: this cannot be used as a free multi-megabyte upload endpoint.
_BOM_CONTENT_MAX = 5_000_000


# ---------------------------------------------------------------------------
# Wire types — projects
# ---------------------------------------------------------------------------


class ProjectWrite(BaseModel):
    """Fields common to creating and updating a project."""

    revision: str | None = Field(default=None, max_length=32)
    status: ProjectStatus | None = None
    description: str | None = None
    source_ref: str | None = Field(default=None, max_length=512)
    notes: str | None = None


class ProjectCreate(ProjectWrite):
    name: str = Field(min_length=1, max_length=255)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ProjectUpdate(ProjectWrite):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class BuildRead(BaseModel):
    id: int
    project_id: int
    build_no: int
    label: str | None
    assembly_count: int
    bom_revision: str | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectRead(BaseModel):
    id: int
    name: str
    revision: str | None
    status: str
    description: str | None
    source_ref: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    #: Every build of this project, newest first — the same "definition plus
    #: its dependents" shape `PartRead.lots` uses, and for the same reason: a
    #: build list without its own endpoint is one round trip, not N.
    builds: list[BuildRead]


class ProjectCreated(ReplayableResponse):
    project: ProjectRead


class ProjectList(BaseModel):
    total: int
    projects: list[ProjectRead]


# ---------------------------------------------------------------------------
# Wire types — BOM lines
# ---------------------------------------------------------------------------


class BomLineRead(BaseModel):
    id: int
    project_id: int
    line_no: int
    designators: str | None
    qty_per_assembly_milli: int
    part_id: int | None
    is_match_confirmed: bool
    is_dnp: bool
    ref_value: str | None
    footprint: str | None
    mpn_raw: str | None
    mpn_norm: str | None
    manufacturer_raw: str | None
    description: str | None
    note: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BomLineList(BaseModel):
    total: int
    lines: list[BomLineRead]


class BomImportRequest(BaseModel):
    #: The file, verbatim — `parse_bom` finds its own header row and delimiter,
    #: so nothing about the export's exact shape has to be known up front.
    content: str = Field(min_length=1, max_length=_BOM_CONTENT_MAX)
    #: Off for a dry preview: land the lines but skip the exact-MPN pass, so a
    #: user can see what an import *would* match before it writes `part_id`.
    match: bool = True
    #: Recorded onto `projects.source_ref` when given — "where the BOM came
    #: from" is a property of the project, not of one import into it, so a
    #: later re-import with the same value is a no-op rather than a drift.
    source_ref: str | None = Field(default=None, max_length=512)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class BomImportResponse(ReplayableResponse):
    project_id: int
    lines: list[BomLineRead]
    matched_count: int
    unmatched_count: int
    dnp_count: int
    #: `mpn_norm` keys that hit more than one active part and so were left
    #: unmatched — the curation worklist an import leaves behind.
    ambiguous_keys: list[str]
    warnings: list[str]


class BomLineEdit(BaseModel):
    """One line's edit. Only the fields actually present in the request are
    applied — `model_fields_set`, exactly as `parts.PartUpdate` does — so a
    client can clear `note` with an explicit `null` without every other
    field on the line needing to be restated.
    """

    id: RowId
    part_id: RowId | None = None
    #: Confirming a match by hand *is* the human agreement
    #: `bom_lines.is_match_confirmed` exists to distinguish from an automatic
    #: exact-MPN hit — unlike `bom_import`, which never sets this, a curator
    #: setting `part_id` through this route is the confirmation. See
    #: `_apply_bom_line_edit` for the exact rule and why it cannot be a
    #: `CHECK`: "confirmed" is meaningless without a part, and that is a
    #: cross-field invariant.
    is_match_confirmed: bool | None = None
    is_dnp: bool | None = None
    qty_per_assembly_milli: QtyMilli | None = None
    designators: str | None = Field(default=None, max_length=1024)
    ref_value: str | None = Field(default=None, max_length=128)
    footprint: str | None = Field(default=None, max_length=128)
    mpn_raw: str | None = Field(default=None, max_length=128)
    manufacturer_raw: str | None = Field(default=None, max_length=128)
    description: str | None = None
    note: str | None = None


class BomLinesUpdateRequest(BaseModel):
    edits: list[BomLineEdit] = Field(min_length=1, max_length=500)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class BomLinesUpdateResponse(ReplayableResponse):
    lines: list[BomLineRead]


# ---------------------------------------------------------------------------
# Wire types — builds and allocations
# ---------------------------------------------------------------------------


class BuildCreate(BaseModel):
    label: str | None = Field(default=None, max_length=128)
    assembly_count: AssemblyCount = 1
    notes: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class BuildCreated(ReplayableResponse):
    build: BuildRead


class BuildUpdate(BaseModel):
    """Label, notes, and the status transition that closes a build.

    Not in the route list this module was scoped from — added because nothing
    else can ever set `project_builds.status`, and closing a build is what
    releases its reservations (see `_apply_build_status`). Idempotent by
    construction like `parts.update_part`: replaying "mark it completed" a
    second time finds nothing left to release and leaves `completed_at` alone,
    so no idempotency key is needed.
    """

    label: str | None = Field(default=None, max_length=128)
    status: BuildStatus | None = None
    notes: str | None = None


class AllocationRead(BaseModel):
    id: int
    build_id: int
    bom_line_id: int | None
    part_id: int
    lot_id: int | None
    qty_milli: int
    state: str
    consumed_ledger_seq: int | None
    reserved_at: datetime | None
    consumed_at: datetime | None
    note: str | None

    model_config = {"from_attributes": True}


class AllocateRequest(BaseModel):
    lot_id: RowId
    qty_milli: QtyMilli
    bom_line_id: RowId | None = None
    #: Asserted rather than trusted from the lot — `reserve` refuses a
    #: mismatch, the same defence `stock.py` never needed because a movement
    #: route only ever takes a `lot_id`. Here a client may reasonably believe
    #: it is reserving "the 10k" and be wrong about which lot that is.
    part_id: RowId | None = None
    note: str | None = None
    #: "Reserve it anyway, more is on order" — a deliberate user decision,
    #: never a silent fallback after a refusal. See `reservations.reserve`.
    allow_overcommit: bool = False
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class AllocateResponse(ReplayableResponse):
    allocation: AllocationRead
    lot: LotRead


class ReleaseRequest(BaseModel):
    """Release one hold, or — when `allocation_id` is omitted — every open
    hold the build has. The bulk form is what a `BuildUpdate` close calls
    internally; exposed here too because abandoning a build outside of that
    transition (a plan dropped before it is ever started) is a legitimate
    reason to free stock without touching `status`.
    """

    allocation_id: RowId | None = None
    note: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ReleaseResponse(ReplayableResponse):
    released_count: int
    #: Populated only for a single-allocation release — a bulk release can
    #: free dozens of rows across as many lots, and returning all of them
    #: would make this response as large as a second `shortages` call.
    allocation: AllocationRead | None = None
    lot: LotRead | None = None


class LineShortageRead(BaseModel):
    bom_line_id: int
    line_no: int
    part_id: int | None
    kind: str
    required_milli: int
    allocated_milli: int
    #: The part of `allocated_milli` its lot can no longer fill — an emptied bin,
    #: an over-committed lot, a lot pulled out of `ACTIVE`. Non-zero means this
    #: line's hold needs a human, and it is why `shortfall_milli` can be positive
    #: while `allocated_milli` looks sufficient.
    undeliverable_milli: int
    available_milli: int | None
    shortfall_milli: int | None
    substitute_part_ids: list[int]
    is_blocking: bool


class ShortageResponse(BaseModel):
    build_id: int
    assembly_count: int
    is_buildable: bool
    lines: list[LineShortageRead]


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _require_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_project", "message": f"no project with id {project_id}"},
        )
    return project


def _require_build(db: Session, build_id: int) -> ProjectBuild:
    build = db.get(ProjectBuild, build_id)
    if build is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_build", "message": f"no build with id {build_id}"},
        )
    return build


def _require_bom_line(db: Session, line_id: int) -> BomLine:
    line = db.get(BomLine, line_id)
    if line is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_bom_line", "message": f"no bom line with id {line_id}"},
        )
    return line


def _require_lot(db: Session, lot_id: int) -> StockLot:
    lot = db.get(StockLot, lot_id)
    if lot is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_lot", "message": f"no stock lot with id {lot_id}"},
        )
    return lot


def _reservation_error(error: ReservationError) -> HTTPException:
    """Map a refusal onto 409, exactly as `stock._ledger_error` does.

    409, not 400: every one of `reserve`'s refusals is a well-formed request
    that would have succeeded against different state (more stock, an open
    build, a matching part) — the client needs the reason, not "bad request".
    """
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"reason": error.reason, "message": str(error)},
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _build_read(build: ProjectBuild) -> BuildRead:
    return BuildRead.model_validate(build)


def _project_read(db: Session, project: Project) -> ProjectRead:
    builds = list(
        db.execute(
            select(ProjectBuild)
            .where(ProjectBuild.project_id == project.id)
            .order_by(ProjectBuild.build_no.desc())
        ).scalars()
    )
    return ProjectRead(
        id=project.id,
        name=project.name,
        revision=project.revision,
        status=project.status,
        description=project.description,
        source_ref=project.source_ref,
        notes=project.notes,
        created_at=project.created_at,
        updated_at=project.updated_at,
        builds=[_build_read(build) for build in builds],
    )


def _bom_line_read(line: BomLine) -> BomLineRead:
    return BomLineRead.model_validate(line)


def _allocation_read(allocation: StockAllocation) -> AllocationRead:
    return AllocationRead.model_validate(allocation)


def _line_shortage_read(line: LineShortage) -> LineShortageRead:
    return LineShortageRead(
        bom_line_id=line.bom_line_id,
        line_no=line.line_no,
        part_id=line.part_id,
        kind=line.kind.value,
        required_milli=line.required_milli,
        allocated_milli=line.allocated_milli,
        undeliverable_milli=line.undeliverable_milli,
        available_milli=line.available_milli,
        shortfall_milli=line.shortfall_milli,
        substitute_part_ids=list(line.substitute_part_ids),
        is_blocking=line.is_blocking,
    )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@router.post("", response_model=ProjectCreated, status_code=status.HTTP_201_CREATED)
def create_project(request: ProjectCreate, db: Session = Depends(get_db)) -> ProjectCreated:
    """Create a project. `name` is the only required field, and is not unique
    — two revisions of a board legitimately share one, per `Project`'s
    docstring."""

    def work() -> ProjectCreated:
        project = Project(
            name=request.name,
            revision=request.revision,
            status=request.status or ProjectStatus.PLANNING,
            description=request.description,
            source_ref=request.source_ref,
            notes=request.notes,
        )
        db.add(project)
        db.flush()
        return ProjectCreated(project=_project_read(db, project))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="projects.create",
        payload=request,
        response_model=ProjectCreated,
        work=work,
    )


@router.get("", response_model=ProjectList)
def list_projects(
    status_filter: list[ProjectStatus] | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ProjectList:
    """Projects, newest first. `status` is repeatable and unset means "all" —
    unlike `intake.list_pending`'s worklist, there is no single default status
    a project view should hide, since `ARCHIVED` is still a real board someone
    may re-plan a build against."""
    where: list[ColumnElement[bool]] = []
    if status_filter:
        where.append(Project.status.in_(status_filter))

    total = int(db.execute(select(func.count()).select_from(Project).where(*where)).scalar_one())
    rows = list(
        db.execute(
            select(Project).where(*where).order_by(Project.id.desc()).limit(limit).offset(offset)
        ).scalars()
    )
    return ProjectList(total=total, projects=[_project_read(db, project) for project in rows])


@router.get("/{project_id}", response_model=ProjectRead)
def read_project(project_id: RowId, db: Session = Depends(get_db)) -> ProjectRead:
    return _project_read(db, _require_project(db, project_id))


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: RowId, request: ProjectUpdate, db: Session = Depends(get_db)
) -> ProjectRead:
    """Edit a project. Unguarded like `parts.update_part`: a PATCH replayed is
    the same fields set to the same values, so there is nothing an idempotency
    key would protect that the request body does not already guarantee."""
    project = _require_project(db, project_id)
    assigned = set(request.model_fields_set)
    for name in ("name", "revision", "description", "source_ref", "notes"):
        if name in assigned:
            setattr(project, name, getattr(request, name))
    if "status" in assigned and request.status is not None:
        project.status = request.status
    db.commit()
    return _project_read(db, project)


# ---------------------------------------------------------------------------
# BOM import and lines
# ---------------------------------------------------------------------------


@router.post("/{project_id}/bom/import", response_model=BomImportResponse)
def import_bom(
    project_id: RowId, request: BomImportRequest, db: Session = Depends(get_db)
) -> BomImportResponse:
    """Land a KiCad-style CSV/TSV export as `bom_lines`. **Appends; never
    replaces** — see `app.services.bom_import`'s module docstring for why a
    "this is a new revision" merge is not attempted. A retried upload without
    `client_op_id` therefore double-imports, the same at-least-once contract
    every unguarded write in this API already has.
    """
    project = _require_project(db, project_id)

    def work() -> BomImportResponse:
        parsed = parse_bom(request.content)
        if request.source_ref is not None:
            project.source_ref = request.source_ref
        result = run_bom_import(db, project, parsed, match=request.match)
        return BomImportResponse(
            project_id=result.project_id,
            lines=[_bom_line_read(line) for line in result.lines],
            matched_count=result.matched_count,
            unmatched_count=result.unmatched_count,
            dnp_count=result.dnp_count,
            ambiguous_keys=list(result.ambiguous_keys),
            warnings=list(result.warnings),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="projects.bom_import",
        payload=request,
        response_model=BomImportResponse,
        work=work,
    )


@router.get("/{project_id}/bom", response_model=BomLineList)
def list_bom_lines(
    project_id: RowId,
    unmatched_only: bool = Query(
        default=False,
        description="Only lines with no part_id — the worklist `ix_bom_lines_unmatched` serves.",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> BomLineList:
    _require_project(db, project_id)
    where: list[ColumnElement[bool]] = [BomLine.project_id == project_id]
    if unmatched_only:
        where.append(BomLine.part_id.is_(None))

    total = int(db.execute(select(func.count()).select_from(BomLine).where(*where)).scalar_one())
    rows = list(
        db.execute(
            select(BomLine)
            .where(*where)
            .order_by(BomLine.line_no, BomLine.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return BomLineList(total=total, lines=[_bom_line_read(line) for line in rows])


def _apply_bom_line_edit(db: Session, line: BomLine, edit: BomLineEdit) -> None:
    """Copy the fields actually present in `edit` onto `line`.

    The one cross-field rule that cannot be a `CHECK`: `is_match_confirmed`
    means nothing without a `part_id`. Concretely —

    * clearing `part_id` (setting it to `null`) always forces
      `is_match_confirmed` back to `False`, regardless of what the request
      also said about it — a confirmed match against no part is not a weaker
      claim, it is not a claim;
    * setting a *new* `part_id` and saying nothing about confirmation defaults
      it to `True`. A human choosing a part through this route is exactly the
      "a human agreed" event the column exists to distinguish from
      `bom_import`'s automatic exact-MPN hits, which is why that importer
      never sets it: nobody has told this line's story until now.
    """
    assigned = set(edit.model_fields_set) - {"id"}

    if "part_id" in assigned:
        if edit.part_id is not None and db.get(Part, edit.part_id) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"reason": "unknown_part", "message": f"no part with id {edit.part_id}"},
            )
        line.part_id = edit.part_id
        if edit.part_id is None:
            line.is_match_confirmed = False
        elif "is_match_confirmed" not in assigned:
            line.is_match_confirmed = True

    if "is_match_confirmed" in assigned and edit.is_match_confirmed is not None:
        if edit.is_match_confirmed and line.part_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "reason": "no_part_to_confirm",
                    "message": f"bom line {line.id} has no part_id; set one in the same edit",
                },
            )
        line.is_match_confirmed = edit.is_match_confirmed

    for name in (
        "is_dnp",
        "qty_per_assembly_milli",
        "designators",
        "ref_value",
        "footprint",
        "mpn_raw",
        "manufacturer_raw",
        "description",
        "note",
    ):
        if name in assigned:
            setattr(line, name, getattr(edit, name))

    if "mpn_raw" in assigned:
        # `mpn_norm` is derived, so a plain copy of `mpn_raw` leaves it stating
        # the *previous* text's key: the matcher then re-matches the corrected
        # line to the part the user just deleted, and a corrected typo can never
        # find the part that does exist. Derived through the importer's own
        # helper so there is one definition of the column.
        line.mpn_norm = normalized_mpn(edit.mpn_raw)


@router.put("/{project_id}/bom", response_model=BomLinesUpdateResponse)
def update_bom_lines(
    project_id: RowId, request: BomLinesUpdateRequest, db: Session = Depends(get_db)
) -> BomLinesUpdateResponse:
    """Apply a batch of per-line edits — corrections, DNP toggles, and manual
    part matching. Idempotency-guarded because, unlike `parts.update_part`,
    the edit that matters most here (confirming a match) has a
    request-shape-dependent default (see `_apply_bom_line_edit`): a retried
    POST that landed and a retried POST that never reached the server are only
    safely indistinguishable with a key.
    """
    _require_project(db, project_id)

    def work() -> BomLinesUpdateResponse:
        touched: list[BomLine] = []
        for edit in request.edits:
            line = _require_bom_line(db, edit.id)
            if line.project_id != project_id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "reason": "line_not_in_project",
                        "message": f"bom line {line.id} belongs to project {line.project_id}",
                    },
                )
            _apply_bom_line_edit(db, line, edit)
            touched.append(line)
        db.flush()
        return BomLinesUpdateResponse(lines=[_bom_line_read(line) for line in touched])

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="projects.bom_update",
        payload=request,
        response_model=BomLinesUpdateResponse,
        work=work,
    )


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


def _next_build_no(db: Session, project_id: int) -> int:
    """The project's next `build_no`. A scalar aggregate, not a loaded list —
    `bom_import._highest_line_no` is the same shape for the same reason."""
    highest = db.execute(
        select(func.coalesce(func.max(ProjectBuild.build_no), 0)).where(
            ProjectBuild.project_id == project_id
        )
    ).scalar_one()
    return int(highest) + 1


@router.post(
    "/{project_id}/builds", response_model=BuildCreated, status_code=status.HTTP_201_CREATED
)
def create_build(
    project_id: RowId, request: BuildCreate, db: Session = Depends(get_db)
) -> BuildCreated:
    """Plan a build. `build_no` is assigned here, never accepted from the
    client, so it stays a stable, gapless-per-project ordinal even as builds
    are created concurrently by different desks. `bom_revision` is copied from
    `projects.revision` at this instant — see `ProjectBuild`'s docstring for
    why that copy, and not a live reference, is the record of what the build
    was actually planned against.
    """
    project = _require_project(db, project_id)

    def work() -> BuildCreated:
        build = ProjectBuild(
            project_id=project.id,
            build_no=_next_build_no(db, project.id),
            label=request.label,
            assembly_count=request.assembly_count,
            bom_revision=project.revision,
            status=BuildStatus.PLANNED,
            notes=request.notes,
        )
        db.add(build)
        db.flush()
        return BuildCreated(build=_build_read(build))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="projects.build_create",
        payload=request,
        response_model=BuildCreated,
        work=work,
    )


@builds_router.get("/{build_id}", response_model=BuildRead)
def read_build(build_id: RowId, db: Session = Depends(get_db)) -> BuildRead:
    return _build_read(_require_build(db, build_id))


@builds_router.patch("/{build_id}", response_model=BuildRead)
def update_build(build_id: RowId, request: BuildUpdate, db: Session = Depends(get_db)) -> BuildRead:
    """Edit a build, including the status transition that closes it.

    Closing (`COMPLETED` or `ABANDONED`) releases every open reservation via
    `reservations.release_build` — the same rule `ProjectBuild`'s docstring
    states: a closed build holding `RESERVED` rows reads as missing inventory
    forever, because nothing else would ever come back to free them.
    Idempotent by construction: replaying "mark it completed" a second time
    finds no open allocations left to release and leaves `completed_at`
    untouched, so no `client_op_id` is needed here either.
    """
    build = _require_build(db, build_id)
    assigned = set(request.model_fields_set)
    if "label" in assigned:
        build.label = request.label
    if "notes" in assigned:
        build.notes = request.notes

    if "status" in assigned and request.status is not None:
        new_status = BuildStatus(request.status)
        old_status = BuildStatus(build.status)
        build.status = new_status
        if new_status is BuildStatus.IN_PROGRESS and build.started_at is None:
            build.started_at = utcnow()
        if new_status in (BuildStatus.COMPLETED, BuildStatus.ABANDONED):
            if build.completed_at is None:
                build.completed_at = utcnow()
            if old_status not in (BuildStatus.COMPLETED, BuildStatus.ABANDONED):
                release_build(db, build, note=f"build closed ({new_status.value})")

    db.commit()
    return _build_read(build)


@builds_router.get("/{build_id}/shortages", response_model=ShortageResponse)
def read_shortages(build_id: RowId, db: Session = Depends(get_db)) -> ShortageResponse:
    """What stands between this build and being built, line by line. A pure
    read — see `reservations.shortage_for_build` for the netting rules."""
    build = _require_build(db, build_id)
    report = shortage_for_build(db, build)
    return ShortageResponse(
        build_id=report.build_id,
        assembly_count=report.assembly_count,
        is_buildable=report.is_buildable,
        lines=[_line_shortage_read(line) for line in report.lines],
    )


@builds_router.post("/{build_id}/allocate", response_model=AllocateResponse)
def allocate_stock(
    build_id: RowId, request: AllocateRequest, db: Session = Depends(get_db)
) -> AllocateResponse:
    """Hold stock for a build. Delegates every refusal to
    `reservations.reserve` — see its docstring for why a reservation, unlike a
    ledger movement, is allowed to say no."""
    build = _require_build(db, build_id)
    lot = _require_lot(db, request.lot_id)
    bom_line = None
    if request.bom_line_id is not None:
        bom_line = _require_bom_line(db, request.bom_line_id)

    def work() -> AllocateResponse:
        try:
            allocation = reserve(
                db,
                build,
                lot,
                request.qty_milli,
                bom_line=bom_line,
                part_id=request.part_id,
                note=request.note,
                allow_overcommit=request.allow_overcommit,
            )
        except ReservationError as error:
            raise _reservation_error(error) from error
        return AllocateResponse(allocation=_allocation_read(allocation), lot=lot_read(db, lot))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="builds.allocate",
        payload=request,
        response_model=AllocateResponse,
        work=work,
    )


@builds_router.post("/{build_id}/release", response_model=ReleaseResponse)
def release_stock(
    build_id: RowId, request: ReleaseRequest, db: Session = Depends(get_db)
) -> ReleaseResponse:
    """Give back one hold, or (no `allocation_id`) every open hold this build
    has — the same choice `stock.empty_bin` makes between one lot and a whole
    location, for the same reason: a build being abandoned outright and a
    build correcting one bad pick are different-sized actions.
    """
    build = _require_build(db, build_id)

    def work() -> ReleaseResponse:
        if request.allocation_id is not None:
            allocation = db.get(StockAllocation, request.allocation_id)
            if allocation is None or allocation.build_id != build.id:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    detail={
                        "reason": "unknown_allocation",
                        "message": f"no allocation {request.allocation_id} on build {build.id}",
                    },
                )
            lot = db.get(StockLot, allocation.lot_id) if allocation.lot_id is not None else None
            try:
                release_allocation(db, allocation, note=request.note)
            except ReservationError as error:
                raise _reservation_error(error) from error
            return ReleaseResponse(
                released_count=1,
                allocation=_allocation_read(allocation),
                lot=None if lot is None else lot_read(db, lot),
            )

        released = release_build(db, build, note=request.note)
        return ReleaseResponse(released_count=released)

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="builds.release",
        payload=request,
        response_model=ReleaseResponse,
        work=work,
    )
