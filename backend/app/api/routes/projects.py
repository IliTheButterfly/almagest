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

`POST /stage`, `/unstage`, `/consume-staged` and `/record-used` (ADR 0004) are
movements in the *literal* sense — parts leave a drawer — so they are guarded
for the same reason `stock.py`'s are: an append-only ledger has no way to take
back a second withdrawal except by writing a third row. `DELETE` on a project
is unguarded because a delete is idempotent by nature; the second one is a 404.

`GET /roster` and `GET /pick-list` are the two views ADR 0004's roster section
asks for, and they are deliberately separate endpoints rather than fields on
`/shortages`: one answers "what went into this" (the past, corrections
included) and the other "where do I go and what do I take" (a route through
the room). A single response carrying all three would be re-fetched wholesale
every time any one of them changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.batch import LineRefused
from app.api.limits import AssemblyCount, CandidateLimit, QtyMilli, ResultOffset, RowId
from app.api.schemas import (
    LotRead,
    ReplayableResponse,
    SuggestionLineRead,
    lot_read,
    suggestion_read,
)
from app.db.session import get_db
from app.models.catalog import Part
from app.models.enums import BuildStatus, ProjectStatus
from app.models.projects import BomLine, Project, ProjectBuild, StockAllocation
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location
from app.models.types import utcnow
from app.services import staging
from app.services.bom_import import import_bom as run_bom_import
from app.services.bom_import import normalized_mpn, parse_bom
from app.services.ledger import Attribution, LedgerError
from app.services.picking import PickGap, PickStop, PickTake, pick_list_for_build
from app.services.requirements import matching
from app.services.requirements.matching import RequirementInput
from app.services.reservations import (
    LineShortage,
    ReservationError,
    available,
    consume_staged,
    record_used,
    release_build,
    reserve,
    shortage_for_build,
    stage,
    unstage,
)
from app.services.reservations import release as release_allocation
from app.services.roster import RosterEntry, RosterLine, roster_for_build
from app.services.staging import StagingError

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
    #: The project box this build's withdrawals went to, null until it stages
    #: anything (ADR 0004 creates them lazily). Always the *project* node even
    #: when the parts went to an assembly under it: that is the box a user carries.
    staging_location_id: int | None
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


class BomSuggestionsResponse(BaseModel):
    """Suggestions for one page of a project's BOM lines.

    Named for the project rather than sharing a name with
    `requirements.SuggestionBatchResponse`: FastAPI disambiguates a repeated model
    name by fully qualifying **both**, which silently renames the other module's
    schema and breaks any client aliasing the short name (see
    `tests/integration/test_health.py`). The line shape itself *is* shared —
    `SuggestionLineRead` — which is the part that must not drift.
    """

    project_id: int
    #: Echoed because `required_milli` on every line was multiplied by it, and a
    #: response whose numbers depend on a defaulted query parameter has to say so.
    assembly_count: int
    #: Lines matching the filter, ignoring pagination.
    total: int
    lines: list[SuggestionLineRead]


class BomLineEdit(BaseModel):
    """One line's edit — or, with `id` omitted, a **new** hand-added line — or,
    with `delete: true`, a line to **remove**. Only the fields actually present
    in the request are applied to an edit — `model_fields_set`, exactly as
    `parts.PartUpdate` does — so a client can clear `note` with an explicit
    `null` without every other field on the line needing to be restated.

    Add and remove ride on this same batch rather than getting their own
    routes: `PUT .../bom` already owns the one code path that writes
    `bom_lines` — `_apply_bom_line_edit`, the project-membership check, the
    idempotency guard — and a `POST .../bom/lines` plus a
    `DELETE .../bom/lines/{id}` would each have to reconstruct that guard
    beside it rather than reuse it, which is exactly the duplication CLAUDE.md
    calls out. A hand-added line is a legal, ordinary row from here on — the
    same "unmatched is normal, not an error" state an import leaves behind,
    just entered a different way.
    """

    id: RowId | None = None
    #: Echoed on this edit's entry in `results`, so a cart marks the row that
    #: failed by its own key rather than by counting positions.
    client_line_id: str | None = Field(default=None, max_length=64)
    #: This edit's own idempotency key, honoured whether or not `partial` is set.
    #: The batch key alone protects a whole resubmission; only a per-line key
    #: protects the *partial* resubmission a per-line failure invites, which for
    #: an add is the difference between one new line and two. See
    #: `app.api.idempotency.replay_line`.
    client_op_id: str | None = Field(default=None, max_length=36)
    #: Required when adding, optional when editing, which the validator below
    #: enforces rather than the type: a new line with no quantity is not a BOM
    #: line at all, while an edit that says nothing about quantity must leave
    #: the existing one alone. Pydantic cannot express "required only when
    #: another field is absent", so the check has to be a cross-field one and
    #: the annotation has to stay optional for both cases.
    qty_per_assembly_milli: QtyMilli | None = None
    #: Remove the line outright. A flag rather than a `DELETE` route so one
    #: batch can add, edit and remove in a single idempotency-guarded write —
    #: a curation pass over an imported BOM does all three, and splitting it
    #: across three requests makes half of it landing a normal outcome.
    #: Deleting is safe here because `bom_lines` is a *plan*, not a record:
    #: `stock_allocations.bom_line_id` is `ON DELETE SET NULL`, so removing a
    #: line drops the requirement and keeps every row saying what was really
    #: taken — which is then reported as off-BOM by the roster.
    delete: bool = False
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
    designators: str | None = Field(default=None, max_length=1024)
    ref_value: str | None = Field(default=None, max_length=128)
    footprint: str | None = Field(default=None, max_length=128)
    mpn_raw: str | None = Field(default=None, max_length=128)
    manufacturer_raw: str | None = Field(default=None, max_length=128)
    description: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _create_needs_a_quantity_delete_needs_an_id(self) -> BomLineEdit:
        if self.id is None and self.delete:
            raise ValueError("cannot delete a line that was never created (id is required)")
        if self.id is None and "qty_per_assembly_milli" not in self.model_fields_set:
            raise ValueError(
                "qty_per_assembly_milli is required when adding a new line (id omitted)"
            )
        return self


class BomLinesUpdateRequest(BaseModel):
    edits: list[BomLineEdit] = Field(min_length=1, max_length=500)
    #: Apply what can be applied and report the rest per line, instead of
    #: refusing the whole batch on the first bad edit (ADR 0007). Off by default:
    #: a curation pass over an imported BOM is one person's considered set of
    #: changes, and half of it landing is a worse outcome than being told the
    #: request was wrong. A **cart** checkout is the opposite — its lines were
    #: gathered minutes apart and one of them naming a part somebody has since
    #: deleted must not discard the other nineteen — so it sets this.
    partial: bool = False
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class BomLineOutcome(ReplayableResponse):
    """What became of one edit.

    The same shape `stock.MovementLineResult` and `AllocateLineResult` use, on
    purpose: a cart drains to one of three destinations (ADR 0007) and a client
    that had to read three different failure shapes would have three chances to
    mishandle the one that matters.
    """

    index: int
    client_line_id: str | None
    applied: bool
    reason: str | None = None
    message: str | None = None
    bom_line_id: int | None = None
    deleted: bool = False


class BomLinesUpdateResponse(ReplayableResponse):
    #: What survived the batch. A deleted line has no row left to render, so it
    #: is reported by id in `deleted_ids` instead — dropping it silently would
    #: leave a client unable to tell "removed" from "not in this batch".
    lines: list[BomLineRead]
    deleted_ids: list[int] = Field(default_factory=list)
    #: One entry per edit, in request order, applied or not. Always populated,
    #: including when `partial` is off — a caller then never sees a refusal here
    #: because the first one is a 4xx, but it still gets the per-edit mapping from
    #: its own `client_line_id` to the row that was written.
    results: list[BomLineOutcome] = Field(default_factory=list)


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
    """Label, notes, `assembly_count`, and the status transition that closes a
    build.

    Not in the route list this module was scoped from — added because nothing
    else can ever set `project_builds.status`, and closing a build is what
    releases its reservations (see `_apply_build_status`). Idempotent by
    construction like `parts.update_part`: replaying "mark it completed" a
    second time finds nothing left to release and leaves `completed_at` alone,
    so no idempotency key is needed.

    **`assembly_count` needs no idempotency key either, and no companion write
    anywhere.** `reservations.shortage_for_build` computes
    `required_milli = qty_per_assembly_milli * build.assembly_count` fresh on
    every read (ADR 0004) — it is never stored per line — so raising this
    column from 1 to 3 makes `needed_milli` triple the next time anyone asks,
    with nothing written to `stock_allocations`. Lowering it is equally inert:
    it only shrinks what is reported *needed*, and can never claw back a unit
    already `RESERVED`, `STAGED`, or `CONSUMED`, so it can never strand real
    stock or need a backfill pass.
    """

    label: str | None = Field(default=None, max_length=128)
    status: BuildStatus | None = None
    assembly_count: AssemblyCount | None = None
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
    #: The movement that put these parts in a project box, so `/unstage` can
    #: compensate exactly it. Null on a `STAGED` remainder left by a partial
    #: build — no single movement describes what the row still holds.
    staged_ledger_seq: int | None
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


class AllocateLine(BaseModel):
    """One line of a batch allocation — one hold, on one lot."""

    #: Echoed on this line's result, so a cart marks the row that failed by its
    #: own key rather than by counting positions.
    client_line_id: str | None = Field(default=None, max_length=64)
    lot_id: RowId
    qty_milli: QtyMilli
    bom_line_id: RowId | None = None
    part_id: RowId | None = None
    note: str | None = None
    allow_overcommit: bool = False
    #: This line's own idempotency key — see `AllocateBatchRequest`.
    client_op_id: str | None = Field(default=None, max_length=36)


class AllocateLineResult(ReplayableResponse):
    index: int
    client_line_id: str | None
    applied: bool
    #: Machine-readable, null exactly when `applied`. `reserve`'s own refusal
    #: codes (`insufficient_available`, `lot_not_active`, `build_closed`, …) plus
    #: the resolution ones only a batch can have.
    reason: str | None = None
    message: str | None = None
    allocation: AllocationRead | None = None
    lot_id: int | None = None
    #: What is left free on that lot after this hold — the number the next line
    #: of the same cart is competing for, and the one a refusal is explained by.
    available_milli: int | None = None


class AllocateBatchRequest(BaseModel):
    """A cart's worth of holds against one build, in one request (ADR 0007).

    Two levels of idempotency key, as `stock.batch_movements` has: this one makes
    a whole resubmission a no-op, and each line's own makes a *partial*
    resubmission one. The second is not redundant here even though a reservation
    writes no ledger row — `stock_allocations` has no UNIQUE to fall back on, so
    without a line key a cart resent after one refusal would double every hold
    that had already been placed.
    """

    lines: list[AllocateLine] = Field(min_length=1, max_length=500)
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class AllocateBatchResponse(ReplayableResponse):
    applied_count: int
    failed_count: int
    results: list[AllocateLineResult]


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


class StageRequest(BaseModel):
    """Send parts out of a bin to a project, or to one of its assemblies.

    `assembly_no` omitted means the project's **floating** parts: set aside for
    the project, not yet committed to a unit. That is the state the requirement
    asks for, and it is a location rather than a flag — see ADR 0004.
    """

    lot_id: RowId
    qty_milli: QtyMilli
    #: Stage *from* an existing hold, consuming it. Omitted means "take these
    #: out and put them in the project box" with no prior reservation, which is
    #: one gesture at a bench rather than two.
    allocation_id: RowId | None = None
    bom_line_id: RowId | None = None
    #: 1-based, and bounded by the build's `assembly_count` — `staging`
    #: refuses "assembly 7 of 3", because that names a unit that does not
    #: exist and no later correction could attach the parts to it.
    assembly_no: AssemblyCount | None = None
    note: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class StageResponse(ReplayableResponse):
    allocation: AllocationRead
    #: The drawer, whose count has already dropped — the number every screen
    #: reads, correct the instant the parts left it.
    source_lot: LotRead
    #: The project box's lot — for a whole-lot move, the *same* lot relocated.
    staging_lot: LotRead
    #: Where they landed: the project node, or an assembly node under it. On
    #: the wire as an id rather than a path because the path is derived and
    #: `lot_read` already carries `location_label_path` for display.
    staging_location_id: int
    #: One row for a whole-lot move, two for a partial one — the ledger rows
    #: this withdrawal wrote, so a client can hand them to `/api/stock/undo`.
    seqs: list[int]
    #: Set only for a partial withdrawal, whose two rows are one undoable
    #: unit. A whole-lot move is a single row and needs no group.
    group_uuid: str | None = None


class UnstageRequest(BaseModel):
    """Put a staged withdrawal back on the shelf.

    No ledger handle needed, unlike `/api/stock/undo`: the allocation records
    the movement it came from (`staged_ledger_seq`), so "put it back" names the
    parts, not the paperwork.
    """

    allocation_id: RowId
    note: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class UnstageResponse(ReplayableResponse):
    allocation: AllocationRead
    #: The lot the parts went *back* into — the original bin's lot for a split,
    #: and the relocated lot itself for a whole-lot move. Derived from the
    #: compensating rows rather than from the allocation, which names the
    #: project box.
    lot: LotRead
    #: The project box's lot as it now stands, usually at zero.
    staging_lot: LotRead
    #: The compensating rows `ledger.reverse` appended. History reads "this
    #: happened, then it was undone", which is not "this never happened".
    reversed_seqs: list[int]


class ConsumeStagedRequest(BaseModel):
    """Build staged parts into the assembly: `staged -> consumed`."""

    allocation_id: RowId
    #: Below the staged quantity leaves the remainder `STAGED` on the same lot,
    #: because a half-populated board is the normal case. Omitted means all of
    #: it.
    qty_milli: QtyMilli | None = None
    note: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ConsumeStagedResponse(ReplayableResponse):
    allocation: AllocationRead
    #: The *staging* lot, drawn down — the drawer's count already dropped when
    #: these parts were staged, so nothing at the source changes here. Returned
    #: rather than the source lot so a client refreshes the box it just emptied.
    lot: LotRead
    seq: int


class ProjectDeleted(BaseModel):
    """What a delete removed. Not a `ReplayableResponse`: a delete is idempotent
    by nature — the second one is a 404 — so there is no stored response a
    retried key would need to replay."""

    project_id: int
    #: Staging boxes that went with it — "usually none", because any box that
    #: ever held stock is named by an undeletable `stock_ledger` row. Reported
    #: rather than assumed so a client's tree cache knows what to drop.
    removed_location_ids: list[int]


class LineShortageRead(BaseModel):
    bom_line_id: int
    line_no: int
    part_id: int | None
    kind: str
    required_milli: int
    allocated_milli: int
    #: The three states `allocated_milli` is the sum of, kept apart on the wire
    #: because ADR 0004 is explicit that merging them lets a BOM look buildable
    #: off parts already soldered into last week's board. Three units in a
    #: drawer, three in a project box and three in a finished assembly are
    #: three different situations with three different next actions.
    reserved_milli: int
    staged_milli: int
    consumed_milli: int
    #: `max(0, required - allocated)` — what still has to be *obtained*, as
    #: distinct from `shortfall_milli`, which is what cannot be obtained from
    #: current free stock. Raising `assembly_count` moves this number and
    #: writes nothing, which is what "demand is derived" means.
    needed_milli: int
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
# Wire types — the roster and the pick list
# ---------------------------------------------------------------------------


class RosterEntryRead(BaseModel):
    """One allocation row, with the ledger row behind it resolved."""

    allocation_id: int
    part_id: int
    part_name: str
    part_mpn: str | None
    lot_id: int | None
    qty_milli: int
    state: str
    ledger_seq: int | None
    ledger_source: str | None
    #: Written down after the fact rather than scanned at the time —
    #: `stock_ledger.source == reconciled`. A corrected row that looked
    #: identical to a scanned one would make the roster unreadable as evidence.
    is_after_the_fact: bool
    #: Where the parts came from, resolved for display. Null for a row that
    #: never named a lot — the roster still renders it, because "we used this
    #: and lost track of where from" is what this report exists to admit.
    location_id: int | None
    location_label_path: str | None
    reserved_at: datetime | None
    consumed_at: datetime | None
    note: str | None


class RosterLineRead(BaseModel):
    """One BOM line's account, or one off-BOM part this build used."""

    #: Null on an off-BOM line: a part nobody planned for has no line to hang
    #: off, and refusing to report it would guarantee the roster is wrong.
    bom_line_id: int | None
    line_no: int | None
    designators: str | None
    part_id: int | None
    part_name: str | None
    part_mpn: str | None
    is_dnp: bool
    is_off_bom: bool
    #: Zero on an off-BOM line — nothing required it. Keeping the field rather
    #: than omitting it means one renderer handles both kinds.
    required_milli: int
    reserved_milli: int
    staged_milli: int
    consumed_milli: int
    #: ADR 0004's `accounted` — `reserved + staged + consumed` — and ADR 0007's
    #: `needed`, `max(0, required - accounted)`. Computed server-side and sent
    #: even though the three states are all here for a client to add up: ADR 0007
    #: asks for *per build* and *in use* to be legible, and a screen doing its own
    #: arithmetic is a second definition of demand that can drift from
    #: `reservations.shortage_for_build`'s. Neither is stored, so raising
    #: `assembly_count` moves both on the next read with nothing backfilled.
    accounted_milli: int
    needed_milli: int
    #: How much of this line's total was recorded after the fact, so a reader
    #: can weigh the line's own credibility without walking `entries` — the
    #: same reason the report carries a build-wide total of it.
    after_the_fact_milli: int
    entries: list[RosterEntryRead]


class RosterResponse(BaseModel):
    build_id: int
    assembly_count: int
    after_the_fact_milli: int
    off_bom_count: int
    lines: list[RosterLineRead]


class RecordUsedRequest(BaseModel):
    """Record a part that was really used but never tracked.

    No `source` field, unlike every other movement request: it is forced to
    `reconciled` by the service. See `reservations.record_used` — a correction
    that could label itself `scan` would destroy the only property that makes
    the roster worth reading.
    """

    lot_id: RowId
    qty_milli: QtyMilli
    bom_line_id: RowId | None = None
    #: Asserted against the lot's own part, like `AllocateRequest.part_id`, so
    #: a client that believes it is recording "the 10k" is told when it is not.
    part_id: RowId | None = None
    note: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class RecordUsedResponse(ReplayableResponse):
    #: A roster entry, not an `AllocationRead`: the correction's whole point is
    #: that it is visible as one, and `is_after_the_fact` lives on that shape.
    allocation: RosterEntryRead
    lot: LotRead
    seq: int


class PickTakeRead(BaseModel):
    bom_line_id: int | None
    line_no: int | None
    designators: str | None
    part_id: int
    part_name: str
    part_mpn: str | None
    lot_id: int
    qty_milli: int
    #: Set when this take fills an existing hold rather than drawing on free
    #: stock, so the picker knows the parts are already spoken for.
    allocation_id: int | None
    is_substitute: bool
    #: Take the whole lot rather than counting out of it — the reel goes in the
    #: tray. Cheaper and less error-prone than counting, so worth surfacing.
    whole_lot: bool


class PickStopRead(BaseModel):
    location_id: int
    label_path: str
    #: Carried so a client can sort or group by the same key the walk order is
    #: built from, without re-deriving hierarchy from `label_path` text — which
    #: would break the moment a location is renamed. `id_path` uses numeric ids
    #: precisely so a rename never invalidates it.
    id_path: str
    short_id: str | None
    takes: list[PickTakeRead]
    qty_milli: int


class PickGapRead(BaseModel):
    """A line the walk cannot finish. **Never omitted** — a pick list missing
    its own gaps reads as complete, and the user finds out at the bench."""

    bom_line_id: int
    line_no: int
    part_id: int | None
    kind: str
    needed_milli: int
    pickable_milli: int
    shortfall_milli: int


class PickListResponse(BaseModel):
    build_id: int
    is_complete: bool
    qty_milli: int
    stops: list[PickStopRead]
    gaps: list[PickGapRead]


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


def _require_allocation(db: Session, build: ProjectBuild, allocation_id: int) -> StockAllocation:
    """An allocation, addressable **only** under the build that owns it.

    A plain `db.get` would find an allocation belonging to someone else's build
    and hand a route the chance to act on it; checking `build_id` here is what
    turns that into a 404 before any service call, the same shape
    `_require_bom_line`'s sibling checks in `update_bom_lines` enforce for a
    line. `stage`'s own build-mismatch guard exists too, but it only fires
    *inside* the service and answers with 409 (`allocation_not_in_build`) —
    wrong for an id that was never addressable here at all, which is a 404.
    """
    allocation = db.get(StockAllocation, allocation_id)
    if allocation is None or allocation.build_id != build.id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_allocation",
                "message": f"no allocation {allocation_id} on build {build.id}",
            },
        )
    return allocation


def _reservation_error(error: ReservationError | StagingError | LedgerError) -> HTTPException:
    """Map a refusal onto 409, exactly as `stock._ledger_error` does.

    409, not 400: every one of these refusals is a well-formed request that
    would have succeeded against different state (more stock, an open build, a
    matching part, an un-reversed movement) — the client needs the reason, not
    "bad request". One function for all three error types because a staging
    movement can fail for a reservation reason (`build_closed`), a destination
    reason (`unknown_assembly`), or a ledger reason (`already_reversed` from a
    double-tapped `/unstage`), and the response shape does not care which.
    """
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"reason": error.reason, "message": str(error)},
    )


def _staging_attribution(
    request: StageRequest | UnstageRequest | ConsumeStagedRequest | RecordUsedRequest,
) -> Attribution:
    """`Attribution` for a staging-family movement.

    None of these four requests carry a `source` field, unlike `stock.py`'s
    `MovementRequest`: stage, unstage, consume-staged and record-used are
    always typed at a build screen, never a raw barcode scan, so there is
    nothing for a client to choose between. `Attribution.source` therefore
    stays at its default, `LedgerSource.MANUAL` — `record_used` overrides it to
    `RECONCILED` itself (never trusting the caller for exactly that field, see
    `LedgerSource.RECONCILED`), so this helper stays the same for every caller.
    """
    return Attribution(note=request.note, client_op_id=request.client_op_id)


def _location_is_referenced(db: Session, location_id: int) -> bool:
    """Whether anything still names this location: a lot sitting in it (even at
    zero balance) or a ledger row that ever moved stock through it.

    Checked **before** attempting a delete rather than caught as an
    `IntegrityError`. `stock_lots.location_id` and `stock_ledger.{from,to}_
    location_id` are all `RESTRICT`, and once any lot has ever been created in
    a project's staging box there is always at least one `stock_lots` row
    pointing at it — even after that lot is drawn down to zero, because nothing
    ever deletes a lot row. So this is true for almost every box that ever held
    anything, which is the honest answer `delete_project` promises: "usually
    none" removed, not "usually fails".
    """
    lot_row = db.execute(
        select(StockLot.id).where(StockLot.location_id == location_id).limit(1)
    ).first()
    if lot_row is not None:
        return True
    ledger_row = db.execute(
        select(StockLedger.seq)
        .where(
            or_(
                StockLedger.from_location_id == location_id,
                StockLedger.to_location_id == location_id,
            )
        )
        .limit(1)
    ).first()
    return ledger_row is not None


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
        reserved_milli=line.reserved_milli,
        staged_milli=line.staged_milli,
        consumed_milli=line.consumed_milli,
        needed_milli=line.needed_milli,
        undeliverable_milli=line.undeliverable_milli,
        available_milli=line.available_milli,
        shortfall_milli=line.shortfall_milli,
        substitute_part_ids=list(line.substitute_part_ids),
        is_blocking=line.is_blocking,
    )


def _roster_entry_read(entry: RosterEntry) -> RosterEntryRead:
    return RosterEntryRead(
        allocation_id=entry.allocation_id,
        part_id=entry.part_id,
        part_name=entry.part_name,
        part_mpn=entry.part_mpn,
        lot_id=entry.lot_id,
        qty_milli=entry.qty_milli,
        state=entry.state.value,
        ledger_seq=entry.ledger_seq,
        ledger_source=None if entry.ledger_source is None else entry.ledger_source.value,
        is_after_the_fact=entry.is_after_the_fact,
        location_id=entry.location_id,
        location_label_path=entry.location_label_path,
        reserved_at=entry.reserved_at,
        consumed_at=entry.consumed_at,
        note=entry.note,
    )


def _roster_line_read(line: RosterLine) -> RosterLineRead:
    return RosterLineRead(
        bom_line_id=line.bom_line_id,
        line_no=line.line_no,
        designators=line.designators,
        part_id=line.part_id,
        part_name=line.part_name,
        part_mpn=line.part_mpn,
        is_dnp=line.is_dnp,
        is_off_bom=line.is_off_bom,
        required_milli=line.required_milli,
        reserved_milli=line.reserved_milli,
        staged_milli=line.staged_milli,
        consumed_milli=line.consumed_milli,
        accounted_milli=line.accounted_milli,
        needed_milli=line.needed_milli,
        after_the_fact_milli=line.after_the_fact_milli,
        entries=[_roster_entry_read(entry) for entry in line.entries],
    )


def _pick_take_read(take: PickTake) -> PickTakeRead:
    return PickTakeRead(
        bom_line_id=take.bom_line_id,
        line_no=take.line_no,
        designators=take.designators,
        part_id=take.part_id,
        part_name=take.part_name,
        part_mpn=take.part_mpn,
        lot_id=take.lot_id,
        qty_milli=take.qty_milli,
        allocation_id=take.allocation_id,
        is_substitute=take.is_substitute,
        whole_lot=take.whole_lot,
    )


def _pick_stop_read(stop: PickStop) -> PickStopRead:
    return PickStopRead(
        location_id=stop.location_id,
        label_path=stop.label_path,
        id_path=stop.id_path,
        short_id=stop.short_id,
        takes=[_pick_take_read(take) for take in stop.takes],
        qty_milli=stop.qty_milli,
    )


def _pick_gap_read(gap: PickGap) -> PickGapRead:
    return PickGapRead(
        bom_line_id=gap.bom_line_id,
        line_no=gap.line_no,
        part_id=gap.part_id,
        kind=gap.kind.value,
        needed_milli=gap.needed_milli,
        pickable_milli=gap.pickable_milli,
        shortfall_milli=gap.shortfall_milli,
    )


def _roster_entry_of(db: Session, build: ProjectBuild, allocation_id: int) -> RosterEntryRead:
    """One allocation's roster row, the same shape `GET /roster` would render
    for it — so `record_used`'s response looks exactly like the entry the
    report would add, without a second copy of `is_after_the_fact`'s
    derivation living in this module. Cheap enough to afford: bounded by one
    build's BOM and allocations, not by history.
    """
    for line in roster_for_build(db, build).lines:
        for entry in line.entries:
            if entry.allocation_id == allocation_id:
                return _roster_entry_read(entry)

    # Unreachable unless the roster's own predicate and `record_used` disagree.
    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"allocation {allocation_id} was written but is not in its own build's roster",
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
    offset: Annotated[ResultOffset, Query()] = 0,
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
    """Land a CSV/TSV BOM export as `bom_lines` — KiCad, Altium, CircuitMaker or
    hand-rolled. The header row, the delimiter and the column meanings are all
    worked out from the file, so no format has to be declared.

    **Text only.** A binary workbook (`.xlsx`, `.xls`, or an Altium `.BomDoc`) is
    refused with a warning naming the format, and imports nothing; re-export as
    CSV or tab-delimited text. Altium's numbered supplier sets beyond the first
    are not imported either, for the reason `app.services.bom_import._map_headers`
    gives — they are alternates, and choosing one is a substitution decision.
    Both cases are reported in `warnings` rather than as an error status, because
    the file still lands whatever else is wrong with it.

    **Appends; never replaces** — see `app.services.bom_import`'s module docstring
    for why a "this is a new revision" merge is not attempted. A retried upload
    without `client_op_id` therefore double-imports, the same at-least-once
    contract every unguarded write in this API already has.
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
    offset: Annotated[ResultOffset, Query()] = 0,
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


def _requirement_text(line: BomLine) -> str:
    """The description a BOM line is read as prose from: `ref_value`, then
    `description`.

    **Deliberately not `footprint`.** A KiCad or Altium footprint is a library
    path, not a description — `Capacitor_SMD:C_0603_1608Metric`,
    `R_0805_2012Metric` — and it tokenises as one long letters-and-digits word,
    which `requirements.parser.looks_like_a_part_number` correctly reads as a part
    number. Feeding it in would give most lines an invented MPN and lose the real
    one, so the package facet has to come from `ref_value` or `description` or not
    at all.

    **Deliberately not `mpn_raw` either.** An unmatched line with an `mpn_raw` is
    one the importer's exact-MPN pass already failed to resolve, so re-reading it
    here adds no matching power — and a second part-number-shaped token in the text
    would collide with a real one in `ref_value` and produce an `ambiguous_mpn`
    refusal for both. It is carried on the response as the line's own field
    instead, where a reviewer can see it.

    `ref_value` first because it is the money column (`10k`, `100nF`) and the
    parser reads curated spellings and values left to right; a `description` that
    repeats the same value contributes nothing new, and one that contradicts it
    surfaces as a contradiction rather than as a silent choice.
    """
    return " ".join(part for part in (line.ref_value, line.description) if part)


@router.get("/{project_id}/bom/suggestions", response_model=BomSuggestionsResponse)
def read_bom_suggestions(
    project_id: RowId,
    unmatched_only: bool = Query(
        default=True,
        description=(
            "Only lines with no part_id. Default **true**, unlike `GET .../bom`: a "
            "line already matched to a part has nothing to suggest, and including "
            "it would spend two executor queries confirming a decision somebody "
            "already made."
        ),
    ),
    # `Annotated[...]` rather than `alias = Query(default=...)`, and that is not a
    # style choice: a bare type-alias annotation with a `Query()` *default* does
    # not carry the alias's `Field` constraints into FastAPI's validation, so
    # `assembly_count=200000` and `candidates=51` both sailed past their own
    # ceilings — and `assembly_count=10**30` multiplied straight into
    # `required_milli`. Inside `Annotated` the constraints apply, which is why
    # `offset` below was correct all along. Every bounded query parameter in this
    # file uses this form.
    assembly_count: Annotated[
        AssemblyCount,
        Query(
            description=(
                "How many boards the demand is for. Demand is derived, never stored "
                "(ADR 0004), and this route has no build in scope — so `required_milli` "
                "is `qty_per_assembly_milli * assembly_count` computed here, exactly as "
                "`reservations.shortage_for_build` computes it from a build's own count."
            )
        ),
    ] = 1,
    limit: int = Query(default=50, ge=1, le=matching.MAX_BATCH),
    offset: Annotated[ResultOffset, Query()] = 0,
    candidates: Annotated[
        CandidateLimit,
        Query(description="Candidates per availability list, per line."),
    ] = matching.DEFAULT_LIMIT,
    db: Session = Depends(get_db),
) -> BomSuggestionsResponse:
    """What this project's unmatched lines could be filled with. **A pure read.**

    The project-scoped half of `POST /api/requirements/suggest`, and the same
    answer: every candidate came out of `app.services.search.query_builder`, in
    `mode="search"` for an exact match and `mode="substitute"` for a part that
    satisfies the line per each template's `substitution_direction`. Ranking
    reorders that result and can never extend it.

    Lives here rather than on `/api/requirements` because it is about *this
    project's* rows — it reads `bom_lines`, it returns `bom_line_id` on every
    answer, and it needs the project-membership check — while the prose batch door
    is about the catalogue and needs no project at all. Both render through
    `app.api.schemas.suggestion_read`, so a suggestion looks the same either way.

    **Writes nothing, and accepts nothing.** Accepting a candidate is an ordinary
    edit through `PUT /api/projects/{project_id}/bom`: `{"edits": [{"id":
    <bom_line_id>, "part_id": <the chosen part_id>}]}`, which sets
    `is_match_confirmed` because a human choosing a part through that route *is*
    the confirmation (see `_apply_bom_line_edit`) — unlike `bom_import`'s automatic
    exact-MPN hits, which never set it. Rejecting every candidate means calling
    nothing at all: the line stays unmatched with its `description` intact, which
    is a normal state an import leaves behind by the dozen and not an error.
    """
    _require_project(db, project_id)
    where: list[ColumnElement[bool]] = [BomLine.project_id == project_id]
    if unmatched_only:
        where.append(BomLine.part_id.is_(None))

    total = int(db.execute(select(func.count()).select_from(BomLine).where(*where)).scalar_one())
    lines = list(
        db.execute(
            select(BomLine)
            .where(*where)
            .order_by(BomLine.line_no, BomLine.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    suggestions = matching.suggest_batch(
        db,
        [
            RequirementInput(
                text=_requirement_text(line),
                required_milli=line.qty_per_assembly_milli * assembly_count,
                bom_line_id=line.id,
            )
            for line in lines
        ],
        limit=candidates,
    )
    return BomSuggestionsResponse(
        project_id=project_id,
        assembly_count=assembly_count,
        total=total,
        lines=[suggestion_read(index, suggestion) for index, suggestion in enumerate(suggestions)],
    )


def _validate_bom_line_edit(db: Session, line: BomLine | None, edit: BomLineEdit) -> None:
    """Every refusal one edit can produce, decided **before anything is mutated**.

    Split out from `_apply_bom_line_edit` so that applying an edit cannot fail
    halfway through. That matters twice over: the default mode of
    `update_bom_lines` is all-or-nothing, and `partial` mode reports a refusal and
    carries on — and a half-applied edit left behind on the session would be
    committed by the *next* line's success either way. Validate-then-apply is why
    neither mode needs a SAVEPOINT to undo a mutation.

    `line` is None for an add, where nothing is on the row yet.
    """
    assigned = set(edit.model_fields_set) - {"id"}

    if "part_id" in assigned and edit.part_id is not None and db.get(Part, edit.part_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "unknown_part", "message": f"no part with id {edit.part_id}"},
        )

    if "is_match_confirmed" in assigned and edit.is_match_confirmed:
        # What the line's `part_id` will be once this edit lands, which is the
        # only thing "confirmed" can be a claim about.
        part_after = (
            edit.part_id if "part_id" in assigned else (None if line is None else line.part_id)
        )
        if part_after is None:
            subject = "this new bom line" if line is None else f"bom line {line.id}"
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "reason": "no_part_to_confirm",
                    "message": f"{subject} has no part_id; set one in the same edit",
                },
            )


def _apply_bom_line_edit(db: Session, line: BomLine, edit: BomLineEdit) -> None:
    """Copy the fields actually present in `edit` onto `line`.

    Refusals are `_validate_bom_line_edit`'s job and it is called first here, so
    this function either mutates nothing or mutates everything the edit asked for.

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
    _validate_bom_line_edit(db, line, edit)
    assigned = set(edit.model_fields_set) - {"id"}

    if "part_id" in assigned:
        line.part_id = edit.part_id
        if edit.part_id is None:
            line.is_match_confirmed = False
        elif "is_match_confirmed" not in assigned:
            line.is_match_confirmed = True

    if "is_match_confirmed" in assigned and edit.is_match_confirmed is not None:
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


def _next_line_no(db: Session, project_id: int) -> int:
    """One past this project's highest `line_no` — for a hand-added line, so it
    lands after every imported line rather than colliding with the number a
    later re-import would assign next. Same shape as `_next_build_no` below and
    as `bom_import._highest_line_no`, and deliberately not imported from that
    module: it is that module's own private helper for its own re-import pass,
    not a shared utility, and the two aggregates are one line each to keep
    separate rather than one round trip apart to keep the same.
    """
    highest = db.execute(
        select(func.coalesce(func.max(BomLine.line_no), 0)).where(BomLine.project_id == project_id)
    ).scalar_one()
    return int(highest) + 1


#: Where a single BOM edit's idempotency record is filed, distinct from the
#: batch's `projects.bom_update`.
_BOM_LINE_ENDPOINT = "projects.bom_line"


def _http_failure(error: HTTPException) -> tuple[str, str]:
    """`(reason, message)` out of a per-line `HTTPException`.

    Every refusal this module raises carries `detail={"reason", "message"}`, so a
    batch can turn one back into a line failure without the validation helpers
    needing a second, batch-only error type. The fallback covers a detail that is
    a bare string, which nothing here produces today and a future edit might.
    """
    detail = error.detail
    if isinstance(detail, dict):
        return str(detail.get("reason", "refused")), str(detail.get("message", detail))
    return "refused", str(detail)


def _bom_line_refusal(
    index: int, edit: BomLineEdit, reason: str, message: str, *, partial: bool
) -> BomLineOutcome:
    """This edit's failure entry — or a 409, when the caller did not ask for
    partial application. One helper so the two modes cannot drift apart."""
    if not partial:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason": reason, "message": message})
    return BomLineOutcome(
        index=index,
        client_line_id=edit.client_line_id,
        applied=False,
        reason=reason,
        message=message,
        bom_line_id=edit.id,
    )


@router.put("/{project_id}/bom", response_model=BomLinesUpdateResponse)
def update_bom_lines(
    project_id: RowId, request: BomLinesUpdateRequest, db: Session = Depends(get_db)
) -> BomLinesUpdateResponse:
    """Apply a batch of per-line edits — corrections, DNP toggles, manual part
    matching, hand-adding a line (`id` omitted), and removing one
    (`delete: true`). Idempotency-guarded because, unlike `parts.update_part`,
    the edit that matters most here (confirming a match) has a
    request-shape-dependent default (see `_apply_bom_line_edit`): a retried
    POST that landed and a retried POST that never reached the server are only
    safely indistinguishable with a key — which also means a retried *add*
    adds one line, not two.

    **This is also the cart's checkout door for a project BOM** (ADR 0007), and
    no parallel batch route was added for it: adding a line is already what a
    cart line becomes, and a second route would have to rebuild the
    project-membership check, `_apply_bom_line_edit` and the idempotency guard
    beside this one. What the cart needs and a curation pass does not is
    `partial: true` — apply what can be applied, report the rest per line — plus
    a `client_op_id` per edit, so resubmitting a cart after one line was refused
    does not re-add the lines that landed.
    """
    _require_project(db, project_id)

    def work() -> BomLinesUpdateResponse:
        touched: list[BomLine] = []
        deleted_ids: list[int] = []
        results: list[BomLineOutcome] = []
        seen_keys: set[str] = set()
        # Queried once and then counted up locally: `_next_line_no` reads an
        # aggregate over rows this loop is still adding, and re-querying per
        # add would return the same number until a flush, which is how two new
        # lines end up sharing one `line_no`. Only advanced once an add has
        # actually landed, so a refused add does not burn a number.
        next_line_no: int | None = None

        for index, edit in enumerate(request.edits):
            key = edit.client_op_id
            if key is not None and key in seen_keys:
                results.append(
                    _bom_line_refusal(
                        index,
                        edit,
                        "duplicate_client_op_id",
                        f"client_op_id {key} appears on more than one edit",
                        partial=request.partial,
                    )
                )
                continue
            if key is not None:
                seen_keys.add(key)

            try:
                stored = idempotency.replay_line(
                    db,
                    client_op_id=key,
                    endpoint=_BOM_LINE_ENDPOINT,
                    payload=edit,
                    response_model=BomLineOutcome,
                )
            except idempotency.LineIdempotencyError as error:
                results.append(
                    _bom_line_refusal(
                        index, edit, error.reason, str(error), partial=request.partial
                    )
                )
                continue
            if stored is not None:
                results.append(stored.model_copy(update={"index": index}))
                if stored.bom_line_id is not None:
                    if stored.deleted:
                        deleted_ids.append(stored.bom_line_id)
                    else:
                        replayed = db.get(BomLine, stored.bom_line_id)
                        if replayed is not None:
                            touched.append(replayed)
                continue

            # Validated first, mutated second, so a refusal leaves nothing
            # half-applied on the session — which is what lets both modes work
            # without a SAVEPOINT to undo. (A released SAVEPOINT commits under
            # pysqlite, so one here would break the default mode's
            # all-or-nothing contract rather than protect it.)
            try:
                if edit.id is None:
                    _validate_bom_line_edit(db, None, edit)
                    line_no = (
                        _next_line_no(db, project_id) if next_line_no is None else next_line_no + 1
                    )
                    # Guaranteed by `BomLineEdit`'s validator, restated for mypy
                    # because the field is `| None` for the edit case. An
                    # `assert` rather than a raise: a request that reaches here
                    # without it has already been rejected with a 422 by
                    # validation, so this is a type narrowing, not a runtime
                    # check whose failure a client could ever observe.
                    qty = edit.qty_per_assembly_milli
                    assert qty is not None
                    line = BomLine(
                        project_id=project_id, line_no=line_no, qty_per_assembly_milli=qty
                    )
                    db.add(line)
                    _apply_bom_line_edit(db, line, edit)
                    db.flush()
                    next_line_no = line_no
                    touched.append(line)
                    outcome = BomLineOutcome(
                        index=index,
                        client_line_id=edit.client_line_id,
                        applied=True,
                        bom_line_id=line.id,
                    )
                else:
                    line = _require_bom_line(db, edit.id)
                    if line.project_id != project_id:
                        raise HTTPException(
                            status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail={
                                "reason": "line_not_in_project",
                                "message": (
                                    f"bom line {line.id} belongs to project {line.project_id}"
                                ),
                            },
                        )
                    if edit.delete:
                        deleted_ids.append(line.id)
                        outcome = BomLineOutcome(
                            index=index,
                            client_line_id=edit.client_line_id,
                            applied=True,
                            bom_line_id=line.id,
                            deleted=True,
                        )
                        db.delete(line)
                    else:
                        _validate_bom_line_edit(db, line, edit)
                        _apply_bom_line_edit(db, line, edit)
                        touched.append(line)
                        outcome = BomLineOutcome(
                            index=index,
                            client_line_id=edit.client_line_id,
                            applied=True,
                            bom_line_id=line.id,
                        )
            except HTTPException as error:
                # Every per-edit refusal in this module is an `HTTPException`
                # carrying `{reason, message}`. Re-raised unchanged in the default
                # mode, so the existing contract (a bad edit is a 404/422 and
                # nothing lands) is exactly preserved; turned into this line's
                # failure when the caller asked for partial application.
                if not request.partial:
                    raise
                reason, message = _http_failure(error)
                results.append(_bom_line_refusal(index, edit, reason, message, partial=True))
                continue

            idempotency.record_line(
                db,
                client_op_id=key,
                device_id=request.device_id,
                endpoint=_BOM_LINE_ENDPOINT,
                payload=edit,
                result=outcome,
            )
            results.append(outcome)

        db.flush()
        return BomLinesUpdateResponse(
            lines=[_bom_line_read(line) for line in touched],
            deleted_ids=deleted_ids,
            results=results,
        )

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
    if "assembly_count" in assigned and request.assembly_count is not None:
        # **The whole write.** ADR 0004's "changing the assembly count marks the
        # extra parts as needed" is satisfied by this one column, because
        # `required_milli` is `qty_per_assembly_milli * assembly_count` computed
        # on every read. Backfilling `stock_allocations` here would be a second
        # place demand lives, and an event handler that could be missed.
        build.assembly_count = request.assembly_count

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


#: Where a *line's* idempotency record is filed, distinct from the batch's
#: `builds.allocate_batch` so a key reused across the two levels is caught as a
#: mismatch instead of replaying the wrong shape.
_ALLOCATE_LINE_ENDPOINT = "builds.allocate_line"


@builds_router.post("/{build_id}/allocate-batch", response_model=AllocateBatchResponse)
def allocate_stock_batch(
    build_id: RowId, request: AllocateBatchRequest, db: Session = Depends(get_db)
) -> AllocateBatchResponse:
    """Hold stock for a build, several lots at once — a cart checkout to a build
    (ADR 0007).

    **One line failing places the rest and reports just that failure.** That is
    not a convenience: the cart was gathered at the shelf over minutes, so by
    checkout time one of its lots may have been emptied, quarantined or promised
    to another build, and `reserve` is the one service in this system allowed to
    say no. Refusing the whole cart over that would throw away the user's other
    nineteen decisions, and a 4xx would leave them unable to see which row to fix.

    Lines are applied **in order**, so two lines of one cart drawing on the same
    lot compete for it exactly as two separate requests would — the second sees
    what the first left. `reserve` stays the sole writer of `stock_allocations`
    and `qty_reserved_milli_cached`, and it decides every refusal before it
    mutates anything, which is what lets a refused line leave nothing behind
    without a SAVEPOINT to roll back.
    """
    build = _require_build(db, build_id)

    def work() -> AllocateBatchResponse:
        seen_keys: set[str] = set()
        results = [
            _apply_allocate_line(db, build, request, line, index, seen_keys)
            for index, line in enumerate(request.lines)
        ]
        applied = sum(1 for result in results if result.applied)
        return AllocateBatchResponse(
            applied_count=applied, failed_count=len(results) - applied, results=results
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="builds.allocate_batch",
        payload=request,
        response_model=AllocateBatchResponse,
        work=work,
    )


def _apply_allocate_line(
    db: Session,
    build: ProjectBuild,
    request: AllocateBatchRequest,
    line: AllocateLine,
    index: int,
    seen_keys: set[str],
) -> AllocateLineResult:
    """One hold, never raising.

    No SAVEPOINT: `reserve` performs every check before it touches the allocation
    or the reserved cache, so a refusal has nothing to undo — and under pysqlite a
    released SAVEPOINT commits, which would break the enclosing `run`'s single
    transaction rather than protect it.
    """

    def refused(reason: str, message: str) -> AllocateLineResult:
        return AllocateLineResult(
            index=index,
            client_line_id=line.client_line_id,
            applied=False,
            reason=reason,
            message=message,
        )

    if line.client_op_id is not None:
        if line.client_op_id in seen_keys:
            return refused(
                "duplicate_client_op_id",
                f"client_op_id {line.client_op_id} appears on more than one line",
            )
        seen_keys.add(line.client_op_id)

    try:
        stored = idempotency.replay_line(
            db,
            client_op_id=line.client_op_id,
            endpoint=_ALLOCATE_LINE_ENDPOINT,
            payload=line,
            response_model=AllocateLineResult,
        )
    except idempotency.LineIdempotencyError as error:
        return refused(error.reason, str(error))
    if stored is not None:
        return stored.model_copy(update={"index": index})

    try:
        lot = db.get(StockLot, line.lot_id)
        if lot is None:
            raise LineRefused(f"no stock lot with id {line.lot_id}", reason="unknown_lot")
        bom_line = None
        if line.bom_line_id is not None:
            bom_line = db.get(BomLine, line.bom_line_id)
            if bom_line is None:
                raise LineRefused(
                    f"no bom line with id {line.bom_line_id}", reason="unknown_bom_line"
                )
        allocation = reserve(
            db,
            build,
            lot,
            line.qty_milli,
            bom_line=bom_line,
            part_id=line.part_id,
            note=line.note,
            allow_overcommit=line.allow_overcommit,
        )
    except LineRefused as error:
        return refused(error.reason, str(error))
    except ReservationError as error:
        return refused(error.reason, str(error))

    result = AllocateLineResult(
        index=index,
        client_line_id=line.client_line_id,
        applied=True,
        allocation=_allocation_read(allocation),
        lot_id=lot.id,
        available_milli=available(lot),
    )
    idempotency.record_line(
        db,
        client_op_id=line.client_op_id,
        device_id=request.device_id,
        endpoint=_ALLOCATE_LINE_ENDPOINT,
        payload=line,
        result=result,
    )
    return result


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
            allocation = _require_allocation(db, build, request.allocation_id)
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


# ---------------------------------------------------------------------------
# Staging: withdrawing parts to a project (ADR 0004)
# ---------------------------------------------------------------------------


@builds_router.post("/{build_id}/stage", response_model=StageResponse)
def stage_stock(
    build_id: RowId, request: StageRequest, db: Session = Depends(get_db)
) -> StageResponse:
    """Withdraw parts to the project, or to one of its assemblies.

    **This is an ordinary stock movement**, guarded by `app.api.idempotency`
    like every other one: the parts leave the bin, so the bin's count drops in
    the same transaction. A retried request that carries the same
    `client_op_id` replays the stored response instead of emptying the drawer
    twice — the ledger's `client_op_id` UNIQUE is the second backstop under
    that.
    """
    build = _require_build(db, build_id)
    lot = _require_lot(db, request.lot_id)
    # Resolved out here rather than inside `work`, so a bad id is a 404 before
    # the idempotency guard stores anything — a replayed key must never be able
    # to return a stored 404.
    allocation = (
        None
        if request.allocation_id is None
        else _require_allocation(db, build, request.allocation_id)
    )
    bom_line = None if request.bom_line_id is None else _require_bom_line(db, request.bom_line_id)

    def work() -> StageResponse:
        try:
            move = stage(
                db,
                build,
                lot,
                request.qty_milli,
                attribution=_staging_attribution(request),
                allocation=allocation,
                bom_line=bom_line,
                assembly_no=request.assembly_no,
                note=request.note,
            )
        except (ReservationError, StagingError) as error:
            raise _reservation_error(error) from error
        return StageResponse(
            allocation=_allocation_read(move.allocation),
            source_lot=lot_read(db, move.source_lot),
            staging_lot=lot_read(db, move.staging_lot),
            staging_location_id=move.location.id,
            seqs=[row.seq for row in move.rows],
            group_uuid=move.group_uuid,
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="builds.stage",
        payload=request,
        response_model=StageResponse,
        work=work,
    )


@builds_router.post("/{build_id}/unstage", response_model=UnstageResponse)
def unstage_stock(
    build_id: RowId, request: UnstageRequest, db: Session = Depends(get_db)
) -> UnstageResponse:
    """Put a staged withdrawal back on the shelf — the ledger's existing undo.

    Not a fresh move in the opposite direction: `reservations.unstage` appends
    compensating rows against the movement the allocation names, so the
    history reads "this happened, then it was undone" and the double-tap
    refusals (`already_reversed`) come from `ledger.reverse` for free.
    """
    build = _require_build(db, build_id)
    allocation = _require_allocation(db, build, request.allocation_id)

    def work() -> UnstageResponse:
        try:
            move = unstage(
                db, allocation, attribution=_staging_attribution(request), note=request.note
            )
        except (ReservationError, LedgerError) as error:
            raise _reservation_error(error) from error
        return UnstageResponse(
            allocation=_allocation_read(move.allocation),
            lot=lot_read(db, move.restored_lot),
            staging_lot=lot_read(db, move.staging_lot),
            reversed_seqs=[row.seq for row in move.compensations],
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="builds.unstage",
        payload=request,
        response_model=UnstageResponse,
        work=work,
    )


@builds_router.post("/{build_id}/consume-staged", response_model=ConsumeStagedResponse)
def consume_staged_stock(
    build_id: RowId, request: ConsumeStagedRequest, db: Session = Depends(get_db)
) -> ConsumeStagedResponse:
    """Build staged parts into the assembly: `staged -> consumed`.

    Consumes the *staging* lot, because that is where the parts physically
    are. The bin's count dropped when they were staged; taking it down again
    here would remove the same units twice.
    """
    build = _require_build(db, build_id)
    allocation = _require_allocation(db, build, request.allocation_id)

    def work() -> ConsumeStagedResponse:
        try:
            allocation_after, row = consume_staged(
                db,
                allocation,
                attribution=_staging_attribution(request),
                qty_milli=request.qty_milli,
            )
        except ReservationError as error:
            raise _reservation_error(error) from error
        lot_id = allocation_after.lot_id
        assert lot_id is not None
        return ConsumeStagedResponse(
            allocation=_allocation_read(allocation_after),
            lot=lot_read(db, _require_lot(db, lot_id)),
            seq=row.seq,
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="builds.consume_staged",
        payload=request,
        response_model=ConsumeStagedResponse,
        work=work,
    )


# ---------------------------------------------------------------------------
# The as-built roster and the pick list (ADR 0004)
# ---------------------------------------------------------------------------


@builds_router.get("/{build_id}/roster", response_model=RosterResponse)
def read_roster(build_id: RowId, db: Session = Depends(get_db)) -> RosterResponse:
    """What this build actually used, corrections and all. A pure read.

    Distinct from `/shortages`, which asks "can this be built": this asks
    "what went into it", which is a question about the past and therefore has
    to admit that the past was not always recorded. So it reports parts used
    that no BOM line asked for, and it marks every row somebody wrote down
    after the fact — see `RosterEntryRead.is_after_the_fact` and
    `app.services.roster`.
    """
    build = _require_build(db, build_id)
    report = roster_for_build(db, build)
    return RosterResponse(
        build_id=report.build_id,
        assembly_count=report.assembly_count,
        after_the_fact_milli=report.after_the_fact_milli,
        off_bom_count=len(report.off_bom_lines),
        lines=[_roster_line_read(line) for line in report.lines],
    )


@builds_router.post("/{build_id}/record-used", response_model=RecordUsedResponse)
def record_used_stock(
    build_id: RowId, request: RecordUsedRequest, db: Session = Depends(get_db)
) -> RecordUsedResponse:
    """Record a part that was really used but never tracked.

    One ledger consume plus one `consumed` allocation, in one step, against a
    BOM line or against no line at all. Guarded by `app.api.idempotency` like
    every other movement, and for the sharper reason here: an append-only
    ledger has no way to take back a doubled correction except by writing a
    third row, and the user reaching for this endpoint is already
    reconstructing history by hand.

    **Deliberately permissive**, unlike `/allocate` and `/stage`: a closed
    build, a quarantined lot and a bin with less stock than the correction
    claims are all accepted. Refusing any of them would guarantee the roster
    stays wrong, which ADR 0004 is explicit is the worse outcome. See
    `reservations.record_used`.
    """
    build = _require_build(db, build_id)
    lot = _require_lot(db, request.lot_id)
    bom_line = None if request.bom_line_id is None else _require_bom_line(db, request.bom_line_id)

    def work() -> RecordUsedResponse:
        try:
            allocation, row = record_used(
                db,
                build,
                lot,
                request.qty_milli,
                attribution=_staging_attribution(request),
                bom_line=bom_line,
                part_id=request.part_id,
                note=request.note,
            )
        except ReservationError as error:
            raise _reservation_error(error) from error
        return RecordUsedResponse(
            allocation=_roster_entry_of(db, build, allocation.id),
            lot=lot_read(db, lot),
            seq=row.seq,
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="builds.record_used",
        payload=request,
        response_model=RecordUsedResponse,
        work=work,
    )


@builds_router.get("/{build_id}/pick-list", response_model=PickListResponse)
def read_pick_list(build_id: RowId, db: Session = Depends(get_db)) -> PickListResponse:
    """Where to go and what to take, **in walking order**. A pure read.

    Stops are sorted by `locations.id_path`, so every drawer of one cabinet is
    consecutive and the room is crossed once. That ordering is the feature;
    BOM order would cross it once per line. See `app.services.picking`.
    """
    build = _require_build(db, build_id)
    plan = pick_list_for_build(db, build)
    return PickListResponse(
        build_id=plan.build_id,
        is_complete=plan.is_complete,
        qty_milli=plan.qty_milli,
        stops=[_pick_stop_read(stop) for stop in plan.stops],
        gaps=[_pick_gap_read(gap) for gap in plan.gaps],
    )


# ---------------------------------------------------------------------------
# Deleting a project
# ---------------------------------------------------------------------------


@router.delete("/{project_id}", response_model=ProjectDeleted)
def delete_project(project_id: RowId, db: Session = Depends(get_db)) -> ProjectDeleted:
    """Delete a project, its BOM and its builds. **Refused while parts are
    still in its staging boxes.**

    That refusal is the ADR 0004 consequence, and it is not tidiness: those
    parts are real and on a shelf. Cascading the delete would remove the only
    record of why a box of components is sitting there, which is strictly
    worse than leaving the project in place until someone un-stages them or
    records them as used. Same reasoning as an over-capacity put-away being
    recorded rather than blocked — the physical world wins.

    Empty staging boxes are then removed only *if nothing references them*. A
    box that ever held stock is named by `stock_ledger.from_location_id` /
    `to_location_id`, which are `RESTRICT` against a table nothing can delete
    from, so in practice it stays — visible, empty and harmless. ADR 0004
    implies a cleanup here; the ledger makes that impossible for any box that
    was ever used, and pretending otherwise would mean either deleting history
    or failing the whole request over furniture.
    """
    project = _require_project(db, project_id)
    stuck = staging.stock_in_staging(db, project)
    if stuck:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "stock_in_project_staging",
                "message": (
                    f"{len(stuck)} lot(s) still hold stock in this project's staging boxes;"
                    " un-stage or record them as used before deleting"
                ),
                "lot_ids": [lot.id for lot in stuck],
            },
        )

    # Collected before the delete: every lookup below autoflushes, and reading
    # `project.id` back off an instance the flush has already deleted is a trap
    # worth not setting.
    boxes = staging.staging_locations_of_project(db, project)
    db.delete(project)
    removed = _remove_unreferenced_staging_boxes(db, boxes)
    db.commit()
    return ProjectDeleted(project_id=project_id, removed_location_ids=removed)


def _remove_unreferenced_staging_boxes(db: Session, boxes: Sequence[Location]) -> list[int]:
    """Delete the project's staging branch, keeping whatever anything still names.

    **Removable has to mean "and nothing retained sits under me".** Review found
    ADR 0004's headline workflow — stage straight into an assembly — making a
    project permanently undeletable with a 500: the lot lives at the *assembly*
    node, so the project node in the middle is named by no lot and no ledger row
    and looked removable, while its child was retained. `locations.parent_id` is
    `RESTRICT`, so its delete raised `IntegrityError` at commit and rolled the
    whole request back. Deepest-first ordering was standing in for this
    predicate, and ordering only ever helped when *both* nodes were removable.

    **The flush per node is load-bearing too**, and not a belt-and-braces flush:
    `locations` has no `relationship()` for `parent_id` (see `TreeMixin` — the
    tree is driven by the path cache, not by ORM cascades), so the unit of work
    has no dependency to sort on and batches same-table deletes into one
    `executemany` in arbitrary order. Parent-before-child then fails the
    `RESTRICT` even when every node is unreferenced. Deleting one row at a time
    is the only way the deepest-first order reaches SQL at all.

    `boxes` arrives deepest first, as `staging_locations_of_project` orders them.
    """
    retained: set[int] = set()
    removed: list[int] = []
    for location in boxes:
        if _location_is_referenced(db, location.id) or location.id in retained:
            # A retained node pins every ancestor, so mark the parent before the
            # loop reaches it — which deepest-first guarantees it will.
            retained.add(location.id)
            if location.parent_id is not None:
                retained.add(location.parent_id)
            continue
        db.delete(location)
        db.flush()
        removed.append(location.id)
    return removed
