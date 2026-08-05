"""`/api/research` — the datasheet-research work queue and its submit door.

ADR 0017's interface, and deliberately the same four shapes as `/api/extraction`:
claim, submit, requeue, status. The API hands out parts that have no datasheet and
takes back what was tried; **it never fetches a URL and never opens a PDF**, and
there is no route here that could tempt it to. The worker reaches these paths over
HTTP for ADR 0005's reason — two SQLite writers is corruption, and the single-replica
deployment shape exists to protect exactly that.

## The submit door takes the whole candidate list, not the winner

`ResearchResultRequest.candidates` is every URL the run tried, with its verdict.
That is ADR 0017's requirement rather than a convenience: a part that comes back
`exhausted` is a diagnosis waiting to be made, and four `mpn_absent` rejections
(a provider returning the wrong part) reads nothing like one `not_pdf` (a login
wall) or no rows at all (no provider covers this manufacturer). All three collapse
to "no datasheet" if only successes are stored.

**The state is derived from those candidates, never sent.** There is no `state`
field on the request. A run that validated something is `resolved`, a run that
validated nothing is `exhausted`, and `app.services.research.record_result` is the
one place that decides — so the stored state cannot disagree with the rows that are
supposed to be its evidence.

## No `client_op_id`, for the same reason extraction has none

Keyed on `(part_id, url)`, and every field written is a function of what was
submitted, so a retry lands the same rows by construction. And a *replay* would be
wrong: re-researching a part after a new provider is added is a legitimate second
submission that **must** overwrite. See `app.services.research`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from app.api.limits import ClaimLimit, RowId
from app.db.session import get_db
from app.models.catalog import Part
from app.models.enums import ResearchCandidateState, ResearchState
from app.models.research import MAX_SOURCE_LENGTH, MAX_URL_LENGTH
from app.services import research
from app.services.research import CandidateReport, ResearchError

router = APIRouter(prefix="/api/research", tags=["research"])

#: Hangs off the part's own path rather than off `/api/research`, because a part's
#: candidates are read by the part screen and not by the worker. Same split
#: `extraction.documents_router` makes for the same reason.
parts_router = APIRouter(prefix="/api/parts", tags=["research"])

#: A worker's self-declared name, recorded for diagnostics only. Bounded and
#: pattern-free: nothing branches on it, it is never interpolated into SQL or a
#: path, and a hostname or a pod name is what will arrive.
WorkerId = Annotated[str, StringConstraints(min_length=1, max_length=64)]

CandidateUrl = Annotated[str, StringConstraints(min_length=1, max_length=MAX_URL_LENGTH)]
SourceName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOURCE_LENGTH)]
RejectReasonText = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Sha256Text = Annotated[str, StringConstraints(min_length=64, max_length=64)]

_REASON_STATUS = {
    "too_many_candidates": status.HTTP_413_CONTENT_TOO_LARGE,
}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class ResearchClaim(BaseModel):
    """One leased part, with everything the worker needs to research it.

    The MPN and manufacturer are sent rather than looked up by the worker, so a run
    needs exactly one API call before it starts working. `mpn_norm` is included
    beside `mpn` because it is what validation compares against — the worker must
    not re-derive it and risk normalising differently from the catalogue.
    """

    part_id: int
    name: str
    mpn: str | None
    #: The catalogue's normalisation of `mpn`. **This is the string a candidate's
    #: text must contain** for ADR 0017's validation to pass; deriving it in the
    #: worker instead would put two normalisers in the system.
    mpn_norm: str | None
    manufacturer: str | None
    attempts: int
    lease_expires_at: datetime


class ResearchClaimRequest(BaseModel):
    worker_id: WorkerId
    limit: ClaimLimit = 1


class ResearchClaimBatch(BaseModel):
    worker_id: str
    claims: list[ResearchClaim]


class CandidateSubmission(BaseModel):
    """One tried URL and its verdict.

    `state` is the worker's finding about *this URL*, which it is entitled to
    report — unlike the part's overall state, which is derived. The distinction is
    that the worker fetched this URL and nothing else knows what came back.
    """

    source: SourceName
    url: CandidateUrl
    state: ResearchCandidateState
    #: Required when `state` is `rejected`, refused otherwise. See
    #: `app.services.research.RejectReason` for the vocabulary — free text at the
    #: wire and in the column, so a new provider's new failure mode needs no
    #: migration.
    reject_reason: RejectReasonText | None = None
    #: Required when `state` is `validated`: the blob the worker already stored via
    #: `POST /api/documents`. The API does not fetch it here — it is recording what
    #: the worker reports having stored, and the document route already verified the
    #: bytes hash to this.
    document_sha256: Sha256Text | None = None
    rank: int = Field(default=0, ge=0, le=10_000)
    note: str | None = Field(default=None, max_length=2000)


class ResearchResultRequest(BaseModel):
    """A completed run: every candidate tried, or an error.

    Exactly one of `candidates` and `error`, enforced at the route rather than by a
    validator so the refusal carries the same `{reason, message}` shape as every
    other refusal in this module.
    """

    part_id: RowId
    candidates: list[CandidateSubmission] | None = None
    #: The run itself broke — egress died, a provider raised. **Not** the same as
    #: finding nothing, which is `candidates: []`.
    error: str | None = Field(default=None, max_length=2000)


class ResearchCandidateRead(BaseModel):
    source: str
    url: str
    state: ResearchCandidateState
    reject_reason: str | None
    document_sha256: str | None
    rank: int
    note: str | None
    created_at: datetime


class PartResearchRead(BaseModel):
    """One part's research standing, and what was tried."""

    part_id: int
    state: ResearchState
    attempts: int
    #: NULL for every state but `failed`. An `exhausted` part carries no error,
    #: deliberately — finding no datasheet is not a fault. See
    #: `app.models.enums.ResearchState`.
    error: str | None
    candidates: list[ResearchCandidateRead]


class ResearchResultResponse(BaseModel):
    part: PartResearchRead


class ResearchQueueStatus(BaseModel):
    counts: dict[ResearchState, int]
    pending: int
    failed: int
    #: Counted separately from `failed` and surfaced separately, because a health
    #: check must not treat "no datasheet exists for this part" as breakage.
    exhausted: int
    lease_seconds: int
    max_attempts: int


class ResearchRequeueRequest(BaseModel):
    part_id: RowId


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_part(db: Session, part_id: int) -> Part:
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "no_such_part", "message": f"no part with id {part_id}"},
        )
    return part


def _part_read(db: Session, part: Part) -> PartResearchRead:
    return PartResearchRead(
        part_id=part.id,
        state=ResearchState(part.research_state),
        attempts=part.research_attempts,
        error=part.research_error,
        candidates=[
            ResearchCandidateRead(
                source=row.source,
                url=row.url,
                state=ResearchCandidateState(row.state),
                reject_reason=row.reject_reason,
                document_sha256=row.document_sha256,
                rank=row.rank,
                note=row.note,
                created_at=row.created_at,
            )
            for row in research.candidates_for(db, part_id=part.id)
        ],
    )


def _lease_expiry(part: Part) -> datetime:
    """When a just-granted lease runs out.

    Derived from the stored `research_claimed_at` rather than a fresh clock read, so
    the worker's deadline is the same instant the queue will use to decide the lease
    is gone. Two clock reads would put them microseconds apart in the direction that
    has the worker believing it has longer than it does.
    """
    claimed_at = part.research_claimed_at
    if claimed_at is None:  # pragma: no cover - claim() always writes it
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"reason": "unclaimed", "message": "claimed part has no claim timestamp"},
        )
    return claimed_at + timedelta(seconds=research.LEASE_SECONDS)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=ResearchQueueStatus)
def read_research_status(db: Session = Depends(get_db)) -> ResearchQueueStatus:
    """Queue depth. One grouped count over an indexed column, cheap enough to poll."""
    counts = research.status_counts(db)
    return ResearchQueueStatus(
        counts=counts,
        pending=counts[ResearchState.PENDING],
        failed=counts[ResearchState.FAILED],
        exhausted=counts[ResearchState.EXHAUSTED],
        lease_seconds=research.LEASE_SECONDS,
        max_attempts=research.MAX_RESEARCH_ATTEMPTS,
    )


@router.post("/claims", response_model=ResearchClaimBatch)
def claim_research_work(
    request: ResearchClaimRequest, db: Session = Depends(get_db)
) -> ResearchClaimBatch:
    """Lease up to `limit` parts that want a datasheet.

    A POST despite reading like a query, because it **writes**: it takes a lease and
    burns an attempt. A GET that mutated the queue would be retried by every proxy
    and prefetched by every crawler.

    An empty `claims` list is the ordinary answer and not an error — it means the
    queue is drained, which is where a healthy install spends most of its life.
    """
    claimed = research.claim(db, worker_id=request.worker_id, limit=request.limit)
    # One query for the whole batch rather than a lazy load per part. `Part` has a
    # `manufacturer_id` and deliberately no `manufacturer` relationship, so there is
    # nothing to lazy-load anyway — and a name is what the worker needs, since a URL
    # pattern is keyed on the manufacturer and an id means nothing outside this
    # database.
    names = research.manufacturer_names(db, parts=claimed)
    batch = ResearchClaimBatch(
        worker_id=request.worker_id,
        claims=[
            ResearchClaim(
                part_id=part.id,
                name=part.name,
                mpn=part.mpn,
                mpn_norm=part.mpn_norm,
                manufacturer=(
                    None if part.manufacturer_id is None else names.get(part.manufacturer_id)
                ),
                attempts=part.research_attempts,
                lease_expires_at=_lease_expiry(part),
            )
            for part in claimed
        ],
    )
    # Committed before the response is written: a lease that is not durable is not a
    # lease, and a claim lost on the way back would leave the worker researching a
    # part the queue still considers unclaimed.
    db.commit()
    return batch


@router.post("/results", response_model=ResearchResultResponse)
def submit_research_result(
    request: ResearchResultRequest, db: Session = Depends(get_db)
) -> ResearchResultResponse:
    """Record one run's outcome — the candidates it tried, or a failure.

    Note `candidates: []` and `error` are **different submissions**. An empty list
    says the cascade ran and proposed nothing, which settles the part `exhausted`;
    an error says the run broke, which leaves it claimable until attempts run out.
    Collapsing them would make a dead network indistinguishable from an obscure part.
    """
    candidates, failure = request.candidates, request.error
    if (candidates is None) == (failure is None):
        # Both or neither. Refused rather than resolved by precedence: a worker that
        # sent both has a bug, and picking one would record an outcome it did not
        # unambiguously report. Same refusal as the extraction submit door.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "ambiguous_result",
                "message": "send exactly one of `candidates` (a result) or `error` (a failure)",
            },
        )

    part = _require_part(db, request.part_id)
    try:
        if candidates is not None:
            research.record_result(
                db,
                part=part,
                candidates=[
                    CandidateReport(
                        source=item.source,
                        url=item.url,
                        state=item.state,
                        reject_reason=item.reject_reason,
                        document_sha256=item.document_sha256,
                        rank=item.rank,
                        note=item.note,
                    )
                    for item in candidates
                ],
            )
        elif failure is not None:
            research.record_failure(db, part=part, error=failure)
    except ResearchError as error:
        raise HTTPException(
            _REASON_STATUS.get(error.reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
            detail={"reason": error.reason, "message": str(error)},
        ) from error

    response = ResearchResultResponse(part=_part_read(db, part))
    db.commit()
    return response


@router.post("/requeue", response_model=ResearchResultResponse)
def requeue_research(
    request: ResearchRequeueRequest, db: Session = Depends(get_db)
) -> ResearchResultResponse:
    """Offer a part to the queue again, from zero attempts.

    Two uses, one operation: retry a `failed` part once its cause is fixed, and
    **re-research an `exhausted` one now that a new provider exists**. The second is
    ADR 0017's upgrade path and needs no new machinery because it is this.
    """
    part = _require_part(db, request.part_id)
    research.requeue(db, part=part)
    response = ResearchResultResponse(part=_part_read(db, part))
    db.commit()
    return response


@parts_router.get("/{part_id}/research", response_model=PartResearchRead)
def read_part_research(part_id: RowId, db: Session = Depends(get_db)) -> PartResearchRead:
    """One part's research standing and every candidate tried for it.

    Answers 200 for a part nobody has researched, with `state: pending` and an empty
    candidate list — the same shape and the same reasoning as
    `GET /api/documents/{sha256}/text` answering 200 for a document with no text. A
    part that has not been researched is not an error; only the datasheet waits.
    """
    part = _require_part(db, part_id)
    return _part_read(db, part)
