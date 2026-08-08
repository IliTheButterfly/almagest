"""One intake entry's whole story, and the doors a worker records a run through.

Five routes with one subject:

* `GET /api/intake/pending/{entry_id}/activity` — the timeline. Capture, dispatch,
  the model runs with their transcripts and their cost, the proposed candidates,
  the part a person accepted, and that part's research, extraction and field
  candidates. A read, and nothing but a read.
* `POST /api/runs` — a worker reporting that it *started* calling a model. The same
  kind of door as `/api/dispatch/results`, and for the same ADR 0005 reason: the
  worker is a separate process and two SQLite writers is corruption, so it reaches
  the database over HTTP or not at all.
* `POST /api/runs/{run_id}/progress` — the answer so far, republished while the
  model is still generating it.
* `POST /api/runs/{run_id}/finish` — the final answer and the bill, or the failure.
* `POST /api/intake/pending/{entry_id}/chat` — open a conversation *about* this scan.

## A run is watchable because the row exists before the answer does

The transcript used to be written once, afterwards. That is simpler and it cannot
show anything live: ADR 0021 measured reads spending their whole budget on reasoning
before answering — once 12 318 characters with no answer at all — and for those forty
seconds there was no row, so a person watching could not tell an overrunning read
from a hung worker.

So the row is opened at the start, the partial text is republished onto it about once
a second, and it is settled at the end. **`finished_at IS NULL` is what "in flight"
means** and `ModelRunRead.in_flight` is the flag a client polls on.

**The throttle is the worker's, and it is load-bearing rather than tidy.** The API is
one SQLite writer on an RWO volume; a per-token republish would put thousands of
transactions through it for one photograph, competing with every scan and every
put-away. `app.services.enrichment.progress` holds the policy and has a test.

**And a client must stop polling.** `runs_in_flight` is served precisely so it can:
above zero, refresh; at zero, stop. `runs.STALE_AFTER_SECONDS` bounds the case where
a worker dies mid-read, so a killed pod cannot leave a tab polling forever.

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

There is no field on `ModelRunIn`, `ModelRunProgress` or `ModelRunFinish` that could
reach `pending_intakes.mpn`, `resolved_part_id` or `status`; the activity route takes
no body at all; and the chat route takes none either — it creates a thread and one
message. Recording that a model was asked a question, watching it answer, and
arguing with the answer are all different acts from accepting it, and this module can
only do the first three.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints, computed_field
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
from app.services import activity, capture_chat, chat, runs
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


class ModelRunProgress(BaseModel):
    """The answer so far, from the worker watching it arrive.

    **The whole accumulated text, not a delta.** `app.services.runs.progress` argues
    it: an append endpoint would need to be exactly-once to be correct, and a
    retried POST would otherwise duplicate a fragment into a transcript the model
    never emitted. Replacement makes the failure modes HTTP actually has cost a
    frame and nothing worse.

    No length limit here, for the same reason `ModelRunIn`'s two text fields have
    none: the bound is `MAX_TRANSCRIPT_CHARS` and it is applied in the service, so a
    worker never has to know the number and never has to choose between discarding a
    transcript and being refused.
    """

    response_text: str


class ModelRunFinish(BaseModel):
    """A run settling: the final answer and the bill, or the failure.

    Every count is optional and **never defaulted to zero** — `ModelRunIn`'s note
    applies unchanged. `response_text` absent means the worker had nothing to
    report, and the partial already stored is then kept rather than cleared: on a
    stream that died, what arrived before it died is the evidence.
    """

    finished_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = Field(default=None, max_length=MAX_FINISH_REASON_LENGTH)
    response_text: str | None = None
    error: str | None = Field(default=None, max_length=4000)


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_flight(self) -> bool:
        """Is a model still working on this, right now?

        Computed from the two timestamps through `app.services.runs.in_flight_at`
        rather than stored, so there is exactly one definition of "in flight" and no
        column that can disagree with `finished_at`.

        **This is the field the client polls on.** While any run on an entry is in
        flight the screen refreshes about once a second and shows a loading card with
        elapsed time; when none is, it stops. `in_flight_at` is also what bounds
        that: a worker killed mid-read leaves a row that never finishes, and after
        `STALE_AFTER_SECONDS` this goes false so the polling ends rather than running
        against the single writer for as long as the tab is open.
        """
        return runs.in_flight_at(self.started_at, self.finished_at)


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
    #: How many of `model_runs` a model is still working on.
    #:
    #: Said as a number at the top level rather than left for the client to count,
    #: because it is the **whole polling contract**: refresh while this is above zero,
    #: stop when it is not. A client counting `in_flight` itself would be a second
    #: implementation of a rule that has a staleness bound in it, and the one that
    #: got it wrong would poll the single SQLite writer forever.
    runs_in_flight: int


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


def _require_run(db: Session, run_id: int) -> ModelRun:
    run = db.get(ModelRun, run_id)
    if run is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "message": f"no model run with id {run_id}"},
        )
    return run


@router.post("/{run_id}/progress", response_model=ModelRunRead)
def record_run_progress(
    run_id: RowId, request: ModelRunProgress, db: Session = Depends(get_db)
) -> ModelRunRead:
    """The answer so far, while the model is still generating it.

    **This is what makes a read watchable, and it is a store rather than a stream.**
    The worker asks its model for a streamed response, accumulates the text, and
    republishes it here about once a second; the activity screen polls. No push
    transport was added and this process still speaks to no model — ADR 0005 is
    untouched, because storing text somebody else generated is exactly what the API
    already does for a finished run.

    **The rate limit lives in the worker, not here**, and that is a deliberate
    division: `app.services.enrichment.progress.ProgressReporter` publishes at most
    once per second and only when the text grew. This route could not enforce it
    without keeping per-run state in a single-replica process, and a route that
    accepted every frame while *hoping* callers throttle would put thousands of
    transactions per photograph through the one SQLite writer. The reason it is safe
    to trust the caller is that the only caller is ours and its throttle has a test.

    409 on a finished run, rather than a silent no-op: a partial that lands after the
    final answer would overwrite it with an earlier prefix, and a worker doing that
    has a bug worth surfacing.
    """
    run = _require_run(db, run_id)
    try:
        runs.progress(db, run=run, response_text=request.response_text)
    except runs.RunError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": error.reason, "message": str(error)},
        ) from error
    read = _run_read(run)
    db.commit()
    return read


@router.post("/{run_id}/finish", response_model=ModelRunRead)
def finish_model_run(
    run_id: RowId, request: ModelRunFinish, db: Session = Depends(get_db)
) -> ModelRunRead:
    """Settle a run: the final answer and what it cost, or what broke.

    The other half of the split `record` starts. Setting `finished_at` is what takes
    the run out of flight and stops every watching client polling, so it is called on
    the failure path too — a read that broke is finished, and leaving it unsettled
    would have the screen waiting on an answer that is never coming.

    Deliberately **not** refused on a second call: unlike a partial, finishing twice
    is a worker retrying a call whose 200 it never saw, and both bodies carry the
    same facts. `app.services.runs.finish` argues it.
    """
    run = _require_run(db, run_id)
    runs.finish(
        db,
        run=run,
        finished_at=request.finished_at,
        latency_ms=request.latency_ms,
        prompt_tokens=request.prompt_tokens,
        completion_tokens=request.completion_tokens,
        finish_reason=request.finish_reason,
        response_text=request.response_text,
        error=request.error,
    )
    read = _run_read(run)
    db.commit()
    return read


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
    run_reads = [_run_read(run) for run in story.model_runs]
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
        model_runs=run_reads,
        # Counted off the same `in_flight` the rows carry, so the number and the
        # badges can never disagree.
        runs_in_flight=sum(1 for run in run_reads if run.in_flight),
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


class CaptureChatRead(BaseModel):
    """Where the conversation about this scan lives.

    Deliberately thin: a thread id and whether it was just started. The transcript
    itself is served by `GET /api/chat/threads/{id}` and sent through
    `POST /api/chat/threads/{id}/send`, which are the routes the PWA's chat component
    already drives. Returning a whole `ThreadDetail` here would be a second way to
    read a thread, and the second one is always the one that goes stale.
    """

    thread_id: int
    #: False when an earlier conversation was reopened. Surfaced so the UI can say
    #: "picking up where you left off" rather than presenting old questions as new.
    created: bool
    #: Characters of context seeded into the opening turn. A number rather than the
    #: text, because the text is in the thread — this is here so a client can tell a
    #: briefing that assembled from one that came out empty.
    briefing_chars: int


@intake_router.post(
    "/{entry_id}/chat", response_model=CaptureChatRead, status_code=status.HTTP_201_CREATED
)
def open_capture_chat(entry_id: RowId, db: Session = Depends(get_db)) -> CaptureChatRead:
    """Open the conversation about this scan, seeding it with the context once.

    **Not a second way to read a photograph.** The dispatch run stays single-shot and
    schema-constrained — that decode is what makes an invented datasheet URL
    unrepresentable and a missing `source_text` a refusal — and a conversation cannot
    be constrained that way. So this is a place to *ask about* the reading, beside it
    (ADR 0018), and `app.services.capture_chat` argues the whole split.

    **The model does not see the photograph.** Chat is text-only all the way down and
    giving the API process a multimodal transport is the one thing ADR 0005's fence
    exists to prevent, so the thread is seeded with the browser's readings, the
    candidates with their quotes, and the raw completion instead. Enough for "why did
    you read the FCC ID as a part number"; not the same as looking.

    **No path from here to an identity.** This creates a thread and one message. There
    is no field on this route, no tool in `chat_tools`, and no call in
    `capture_chat` that could reach `pending_intakes.mpn`, `resolved_part_id`,
    `status` or `intake_identity_candidates` — accepting a candidate is still
    `POST /api/intake/pending/{entry_id}/resolve`, pressed by a person.

    201 even when an existing thread is reopened. The response says which happened;
    the alternative — 200 for a reopen — would make a client's success handling depend
    on a distinction it does not act on.
    """
    entry = db.get(PendingIntake, entry_id)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "message": f"no pending intake with id {entry_id}"},
        )
    thread, created = capture_chat.thread_for(db, entry=entry)
    messages = chat.messages(db, thread_id=thread.id)
    read = CaptureChatRead(
        thread_id=thread.id,
        created=created,
        briefing_chars=len(messages[0].content) if messages else 0,
    )
    db.commit()
    return read


def _run_read(run: ModelRun) -> ModelRunRead:
    return ModelRunRead.model_validate(run)
