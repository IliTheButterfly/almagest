"""One intake entry's whole story, and the door a worker records a run through.

Two routes with one subject:

* `GET /api/intake/pending/{entry_id}/activity` — the timeline. Capture, dispatch,
  the model runs with their transcripts and their cost, the proposed candidates,
  the part a person accepted, and that part's research, extraction and field
  candidates. A read, and nothing but a read.
* `POST /api/runs` — a worker reporting that it called a model. The same kind of
  door as `/api/dispatch/results`, and for the same ADR 0005 reason: the worker is
  a separate process and two SQLite writers is corruption, so it reaches the
  database over HTTP or not at all.

## Why the transcript is stored and shown at all

`CLAUDE.md` forbids auto-accepting a model-read part number, and ADR 0021 makes
`source_text` `NOT NULL` so a reviewer has a quote to check against the photograph.
That covers *what the model said*. The question that follows a wrong reading is
always *what was it told* — did the browser's OCR hand it `CFI4JT100K` and did it
copy the typo; was the barcode anchor there; did the reasoning budget run out
before the answer began. **The never-auto-accept rule is only reviewable if the
prompt is reviewable**, and this is the surface that makes it so.

## The confidence this hands back is the stored, clamped one

`IdentityCandidateRead.confidence` comes off the candidate row, which
`app.services.dispatch.record_result` clamped strictly below
`candidates.AUTO_PROMOTE_CONFIDENCE` on the way in. The model's own self-report
survives only inside `response_text`, as part of a transcript that is labelled as a
transcript. Those two numbers must never be presented as the same quantity: ADR
0021 measured 0.95 self-reported on an answer that was the item's FCC ID.

## This module holds no pixels and calls no model

It reads stored text and hands it back. It imports neither `enrichment.vision` nor
its transport — `tests/integration/test_route_fence.py` greps this directory and
would fail if it did — and the image is referenced only by sha256, which is what
`request_json` carries in place of the bytes.

## Nothing here writes the three forbidden columns

There is no field on `ModelRunIn` that could reach `pending_intakes.mpn`,
`resolved_part_id` or `status`, and the activity route takes no body at all.
Recording that a model was asked a question is not the same act as accepting its
answer, and this module can only do the first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.api.routes.dispatch import IdentityCandidateRead
from app.db.session import get_db
from app.models.enums import DispatchState, ModelRunKind
from app.models.runs import (
    MAX_FINISH_REASON_LENGTH,
    MAX_MODEL_LENGTH,
    MAX_PROVIDER_LENGTH,
    ModelRun,
)
from app.models.scanning import PendingIntake
from app.services import activity, runs
from app.services.dispatch import MAX_DISPATCH_ATTEMPTS

router = APIRouter(prefix="/api/runs", tags=["runs"])
intake_router = APIRouter(prefix="/api/intake/pending", tags=["intake"])

ProviderName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PROVIDER_LENGTH)]
ModelName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MODEL_LENGTH)]
Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64)]


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class ModelRunIn(BaseModel):
    """One call to a model, as the worker that made it reports it.

    ## The two text fields carry no length limit here, deliberately

    `app.services.runs` truncates them at `MAX_TRANSCRIPT_CHARS` and sets
    `truncated`. Enforcing the same number on the wire would make a worker
    responsible for knowing it, and a worker that hit it would have no better
    option than to discard the transcript — which is the outcome the bound exists
    to avoid, not to cause. Refusing to record a model call because its reasoning
    was long is exactly backwards: ADR 0021's most informative measurement is a run
    that emitted 12 318 characters of reasoning and no answer.

    ## Counts are optional and are never defaulted to zero

    `usage` is absent from several local servers' responses. A zero would read as
    "the prompt was empty" and would pull any average taken over these rows toward
    whichever servers were quiet — `app.services.enrichment.calls.CallStats`' own
    rule, kept intact all the way to the column.
    """

    kind: ModelRunKind
    #: Which deployment answered (`local-ollama`), and which weights it served
    #: (`qwen3-vl:8b`). Two strings rather than one, matching the candidate rows, so
    #: "the same model behind a different server" stays visible.
    provider: ProviderName
    model: ModelName
    #: The parked scan this run was about. Absent on an extraction run, which is
    #: about a document.
    intake_id: RowId | None = None
    #: What the model was shown. For a vision run this is the photograph, and it is
    #: the same hash `request_json` carries in place of the image bytes.
    document_sha256: Sha256 | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = Field(default=None, max_length=MAX_FINISH_REASON_LENGTH)
    #: The payload as sent, **with the image already replaced by
    #: `{"image_sha256": ...}` by the transport that held it.** A worker sending
    #: base64 here would be putting megabytes of duplicated pixels into a table
    #: nothing prunes; `vision_openai_compat._sanitised` is where that is prevented.
    request_json: str | None = None
    #: The completion exactly as returned, before parsing.
    response_text: str | None = None
    #: What broke. Set on a run that failed — which is the case a transcript matters
    #: most for, since a failed run leaves no candidate row behind at all.
    error: str | None = Field(default=None, max_length=4000)

    model_config = {"protected_namespaces": ()}


class ModelRunRead(BaseModel):
    """One recorded call, transcript included."""

    id: int
    kind: ModelRunKind
    provider: str
    model: str
    intake_id: int | None
    document_sha256: str | None
    started_at: datetime
    finished_at: datetime | None
    #: NULL means the server did not report it. **Not zero** — see `ModelRunIn`.
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None
    request_json: str | None
    response_text: str | None
    error: str | None
    #: One of the two text fields above hit the ceiling and was cut.
    truncated: bool

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class ModelRunCreated(BaseModel):
    run: ModelRunRead


class CaptureRegionActivity(BaseModel):
    """One outline the browser read off the photograph.

    The geometry is left out. This view answers "what was read", and the quads are
    for drawing the overlay, which `GET /api/captures/{id}` already serves.
    """

    kind: str
    text: str
    symbology: str | None
    #: 0-100, and only ever set on a text region — a barcode checksummed or it did
    #: not. See `app.models.captures.CaptureRegion.confidence`.
    confidence: int | None
    order_index: int

    model_config = {"from_attributes": True}


class CaptureActivity(BaseModel):
    id: int
    created_at: datetime
    document_sha256: str
    width_px: int
    height_px: int
    #: Whether anybody has tried to read the printed lines. `not_attempted` and
    #: "found none" are different, and both are normal.
    text_status: str
    regions: list[CaptureRegionActivity]


class DispatchActivity(BaseModel):
    state: DispatchState
    attempts: int
    #: NULL for every state but `failed`. `unidentified` carries no error on
    #: purpose — a photograph nobody could read is not a fault.
    error: str | None
    label_kind: str | None
    max_attempts: int


class ResearchCandidateActivity(BaseModel):
    source: str
    url: str
    state: str
    reject_reason: str | None
    document_sha256: str | None
    rank: int
    note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentActivity(BaseModel):
    sha256: str
    media_type: str
    byte_size: int
    #: The third queue's standing. `pending` is **normal, not broken** — a stored
    #: PDF whose text nobody has read yet (ADR 0005).
    extraction_state: str
    extraction_attempts: int
    extraction_error: str | None

    model_config = {"from_attributes": True}


class FieldCandidateActivity(BaseModel):
    """One `parameter_value_candidate` row, with the field it is about named."""

    #: `parameter_template.name` — the stable key search and the decoders use.
    template_name: str
    #: `parameter_template.display_name` — what a person calls the field.
    template_label: str
    source: str
    source_ref: str
    confidence: float
    raw_value: str
    status: str
    review_reason: str | None
    requires_human: bool
    created_at: datetime


class ResolvedPartActivity(BaseModel):
    """The part a **person** accepted, and what the later workers made of it.

    Reached through `resolved_part_id` only. A candidate's stub `part_id` is a
    machine's proposal and is not followed — see `app.services.activity.PartStory`.
    """

    id: int
    name: str
    mpn: str | None
    is_stub: bool
    research_state: str
    research_attempts: int
    research_error: str | None
    research_candidates: list[ResearchCandidateActivity]
    documents: list[DocumentActivity]
    field_candidates: list[FieldCandidateActivity]


class IntakeEntryActivity(BaseModel):
    """The entry's own facts. Read-only here, like everything on this route."""

    id: int
    client_op_id: str
    raw_payload: str
    symbology: str | None
    decoded_kind: str | None
    mpn: str | None
    status: str
    device_id: str | None
    note: str | None
    queued_at: datetime | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_part_id: int | None

    model_config = {"from_attributes": True}


class IntakeActivityRead(BaseModel):
    """The whole story of one parked scan, in the order it happened.

    Every section is present even when it is empty, and that is the point rather
    than a shape accident: a client has to be able to say *"no worker has run"*
    where nothing has run, and *"the model could name nothing"* where one ran and
    proposed nothing. An absent key cannot carry that difference, and rendering an
    empty section as a blank reads as a failure.
    """

    entry: IntakeEntryActivity
    #: NULL when the scan was a bare barcode with no photograph, which is the
    #: ordinary fast-path scan.
    capture: CaptureActivity | None
    dispatch: DispatchActivity
    #: Oldest first — a history read backwards is a history nobody follows. Empty
    #: means no model has been asked, not that one answered nothing.
    model_runs: list[ModelRunRead]
    #: Ranked, best first, losers kept. `confidence` here is the **stored, clamped**
    #: value; the model's own self-report survives only inside a transcript.
    identity_candidates: list[IdentityCandidateRead]
    resolved_part: ResolvedPartActivity | None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=ModelRunCreated, status_code=status.HTTP_201_CREATED)
def record_model_run(request: ModelRunIn, db: Session = Depends(get_db)) -> ModelRunCreated:
    """Record that a model was called. A worker's door, over HTTP for ADR 0005.

    **Always inserts; deliberately not idempotent.** Two calls happened, and a
    second row is the only way to see that the first failed and the retry
    succeeded — or that both failed identically, which is what says the problem is
    not transient. `app.services.runs.record` argues it at length.

    A dangling `intake_id` is refused rather than silently stored as NULL: a run
    recorded against a photograph that does not exist is a worker bug, and
    swallowing it would produce a row nothing can ever show.
    """
    if request.intake_id is not None and db.get(PendingIntake, request.intake_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "unknown_intake",
                "message": f"no pending intake with id {request.intake_id}",
            },
        )
    run = runs.record(
        db,
        kind=request.kind,
        provider=request.provider,
        model=request.model,
        intake_id=request.intake_id,
        document_sha256=request.document_sha256,
        started_at=request.started_at,
        finished_at=request.finished_at,
        latency_ms=request.latency_ms,
        prompt_tokens=request.prompt_tokens,
        completion_tokens=request.completion_tokens,
        finish_reason=request.finish_reason,
        request_json=request.request_json,
        response_text=request.response_text,
        error=request.error,
    )
    created = ModelRunCreated(run=_run_read(run))
    db.commit()
    return created


@intake_router.get("/{entry_id}/activity", response_model=IntakeActivityRead)
def read_intake_activity(entry_id: RowId, db: Session = Depends(get_db)) -> IntakeActivityRead:
    """Everything that has happened to one parked scan.

    A `GET` with no side effects at all — see the module docstring. It reads seven
    tables and writes none, which is what makes it safe to open while a worker is
    mid-drain.
    """
    entry = db.get(PendingIntake, entry_id)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "message": f"no pending intake with id {entry_id}"},
        )
    story = activity.story_for(db, entry=entry)
    return IntakeActivityRead(
        entry=IntakeEntryActivity.model_validate(entry),
        capture=(
            None
            if story.capture is None
            else CaptureActivity(
                id=story.capture.capture.id,
                created_at=story.capture.capture.created_at,
                document_sha256=story.capture.document_sha256,
                width_px=story.capture.capture.width_px,
                height_px=story.capture.capture.height_px,
                text_status=story.capture.capture.text_status,
                regions=[
                    CaptureRegionActivity.model_validate(region) for region in story.capture.regions
                ],
            )
        ),
        dispatch=DispatchActivity(
            state=DispatchState(entry.dispatch_state),
            attempts=entry.dispatch_attempts,
            error=entry.dispatch_error,
            label_kind=entry.dispatch_label_kind,
            # Sent so the screen can say "attempt 2 of 2" rather than a bare count
            # the reader has to know the ceiling to interpret.
            max_attempts=MAX_DISPATCH_ATTEMPTS,
        ),
        model_runs=[_run_read(run) for run in story.model_runs],
        identity_candidates=[
            IdentityCandidateRead.model_validate(row) for row in story.identity_candidates
        ],
        resolved_part=(
            None
            if story.resolved is None
            else ResolvedPartActivity(
                id=story.resolved.part.id,
                name=story.resolved.part.name,
                mpn=story.resolved.part.mpn,
                is_stub=story.resolved.part.is_stub,
                research_state=story.resolved.part.research_state,
                research_attempts=story.resolved.part.research_attempts,
                research_error=story.resolved.part.research_error,
                research_candidates=[
                    ResearchCandidateActivity.model_validate(row)
                    for row in story.resolved.research_candidates
                ],
                documents=[
                    DocumentActivity.model_validate(row) for row in story.resolved.documents
                ],
                field_candidates=[
                    FieldCandidateActivity(
                        template_name=template.name,
                        template_label=template.display_name,
                        source=candidate.source,
                        source_ref=candidate.source_ref,
                        confidence=candidate.confidence,
                        raw_value=candidate.raw_value,
                        status=candidate.status,
                        review_reason=candidate.review_reason,
                        requires_human=candidate.requires_human,
                        created_at=candidate.created_at,
                    )
                    for candidate, template in story.resolved.field_candidates
                ],
            )
        ),
    )


def _run_read(run: ModelRun) -> ModelRunRead:
    return ModelRunRead.model_validate(run)
