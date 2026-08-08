"""`/api/dispatch` — the capture-dispatch work queue and its submit door (ADR 0021).

Deliberately the same shapes as `/api/extraction` and `/api/research`: claim, submit,
status. The API hands out parked scans whose photograph nobody has read and takes back
what was read; **it never decodes an image, never base64-encodes anything and never
calls a model.** There is no route here that could tempt it to, and
`tests/integration/test_route_fence.py` greps this directory to keep it that way — ADR
0005 structurally rather than by convention, which matters more here than anywhere
because this is the first pipeline stage whose input is pixels.

The worker reaches these paths over HTTP for ADR 0005's reason: two SQLite writers is
corruption, and the single-replica deployment exists to protect exactly that. It
fetches the image itself from `GET /api/documents/{sha256}`, which is why a claim
carries a **hash** and not bytes.

## Two routes the other queues do not have, because this one is opt-in

`POST /requests` and `DELETE /requests/{intake_id}`. Dispatching costs a **GPU
handover** on a card that is integral, exclusive and co-tenanted (ADR 0016), so
`DispatchState` defaults to `NOT_REQUESTED` and something has to ask. Research needs no
equivalent: its default is `PENDING` and its cost is somebody else's CDN.

`POST /requests` doubles as the requeue door, which is why there is no third route for
that. See `app.services.dispatch.request` — "read this for the first time" and "read
this again now that the model is better" are the same act from the queue's side, and
ADR 0021's upgrade path is the second one.

## The submit door takes the whole candidate list, not the winner

`DispatchResultRequest.candidates` is every identity the run proposed, ranked. That is
ADR 0021's requirement rather than a convenience, and for a stronger reason than
research's: **the losers are still live options.** A wrong reading is eliminated by
`datasheet_validation` failing to find its part number in a PDF it actually fetched,
which is arithmetic a reviewer can check — and that can only happen to a candidate that
was stored.

**The state is derived from those candidates, never sent.** There is no `state` field on
the request. A run that named something is `proposed`, a run that named nothing is
`unidentified`, and `app.services.dispatch.record_result` is the one place that decides
— so the stored state cannot disagree with the rows that are supposed to be its
evidence.

## What this module cannot do, structurally

There is no route here that resolves an intake entry, and no field on any request that
could set `pending_intakes.mpn` or `pending_intakes.resolved_part_id`. `PROPOSED` is
terminal for the machine at any confidence: a chosen candidate reaches
`resolved_part_id` through the existing `POST /api/intake/pending/{id}/resolve`, pressed
by a person. Adding a second acceptance mechanism here is the change that would quietly
undo the never-auto-accept rule, so the absence is the design.

## No `client_op_id`, for the same reason extraction and research have none

Keyed on `(intake_id, mpn)`, and every field written is a function of what was
submitted, so a retry lands the same rows by construction. And a *replay* would be
wrong: re-reading a photograph with a better model is a legitimate second submission
that **must** overwrite. See `app.services.dispatch`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.limits import ClaimLimit, RowId
from app.db.session import get_db
from app.models.captures import CaptureRegion
from app.models.dispatch import (
    MAX_MANUFACTURER_LENGTH,
    MAX_MODEL_LENGTH,
    MAX_MPN_LENGTH,
    MAX_PACKAGE_LENGTH,
    MAX_PROVIDER_LENGTH,
    MAX_SOURCE_TEXT_LENGTH,
)
from app.models.enums import CaptureRegionKind, DispatchState
from app.models.scanning import PendingIntake
from app.services import dispatch
from app.services.dispatch import CandidateReport, DispatchError

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

#: A worker's self-declared name, recorded for diagnostics only. Bounded and
#: pattern-free: nothing branches on it, it is never interpolated into SQL or a path,
#: and a hostname or a pod name is what will arrive.
WorkerId = Annotated[str, StringConstraints(min_length=1, max_length=64)]

MpnText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MPN_LENGTH)]
SourceText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOURCE_TEXT_LENGTH)]

#: How many of a capture's regions ride along on a claim. A creased label with a dozen
#: OCR'd lines is ordinary; a hundred means the OCR pass fragmented, and sending all of
#: them would put a model's whole context budget into noise. ADR 0021 measured ~3 270
#: prompt tokens for a bare image, so this bound is about the *hint* text staying a hint.
MAX_CLAIM_REGIONS = 40

_REASON_STATUS = {
    "too_many_candidates": status.HTTP_413_CONTENT_TOO_LARGE,
    dispatch.NO_CAPTURE: status.HTTP_409_CONFLICT,
}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class DispatchClaim(BaseModel):
    """One leased entry, with everything the worker needs to read its photograph.

    Both of the browser's readings ride along, weighted honestly, which is ADR 0021's
    central mechanism rather than a convenience:

    * `barcode_texts` is an **anchor**. A decoded symbology is checksummed and is
      stronger evidence than anything the model will produce, so a worker holding one
      narrows the request to a single candidate and asks only for confirmation of
      manufacturer and package.
    * `ocr_lines` go in **labelled as unreliable rather than omitted**. They are usually
      nearly right, and nearly right is exactly what a second reader can repair —
      `CFI4JT100K` for `CF14JT100K` is the recorded case.

    Sent rather than looked up by the worker so a run needs exactly one API call before
    it starts working, the same reason `ResearchClaim` carries `mpn_norm`.
    """

    intake_id: int
    capture_id: int
    #: The image's blob hash. The worker fetches the bytes from
    #: `GET /api/documents/{sha256}`; **this route does not read them**, which is what
    #: keeps the image pipeline out of the API process.
    capture_sha256: str
    media_type: str
    #: What `zxing-wasm` decoded in the browser. An anchor — see the class docstring.
    barcode_texts: list[str]
    #: What `tesseract.js` read. The least reliable input and the most useful to correct.
    ocr_lines: list[str]
    #: What the barcode said the part is, if it said anything. Carried so the worker can
    #: see it; **the worker may never write it back** — see the module docstring.
    mpn: str | None
    attempts: int
    lease_expires_at: datetime


class DispatchClaimRequest(BaseModel):
    worker_id: WorkerId
    limit: ClaimLimit = 1


class DispatchClaimBatch(BaseModel):
    worker_id: str
    claims: list[DispatchClaim]


class IdentitySubmission(BaseModel):
    """One proposed identity and what the model quoted for it."""

    mpn: MpnText
    #: 0..1. **Clamped strictly below `candidates.AUTO_PROMOTE_CONFIDENCE` on write** —
    #: see `app.services.dispatch.MAX_VISION_CONFIDENCE`. Reading characters off a
    #: photograph and trusting a datasheet's statement of a value are different
    #: quantities that happen to share a range, and ADR 0021 measured 0.95 on an answer
    #: that was the item's FCC ID.
    confidence: float = Field(ge=0.0, le=1.0)
    #: The characters on the label this reading came from, verbatim. **Required, and
    #: `min_length=1`.** If the model cannot quote what it read, it did not read it, and
    #: the quote is what a reviewer checks instead of taking its word.
    source_text: SourceText
    manufacturer: str | None = Field(default=None, max_length=MAX_MANUFACTURER_LENGTH)
    package: str | None = Field(default=None, max_length=MAX_PACKAGE_LENGTH)
    note: str | None = Field(default=None, max_length=2000)
    #: The stub `parts` row the worker minted for this candidate, if it minted one.
    #: Stored on the candidate; **never** copied to `pending_intakes.resolved_part_id`.
    part_id: RowId | None = None
    rank: int = Field(default=0, ge=0, le=10_000)
    provider: str | None = Field(default=None, max_length=MAX_PROVIDER_LENGTH)
    model: str | None = Field(default=None, max_length=MAX_MODEL_LENGTH)


class DispatchResultRequest(BaseModel):
    """A completed run: every identity proposed, or an error.

    Exactly one of `candidates` and `error`, enforced at the route rather than by a
    validator so the refusal carries the same `{reason, message}` shape as every other
    refusal in this module.
    """

    intake_id: RowId
    candidates: list[IdentitySubmission] | None = None
    #: What the photograph physically is, from `vision.LABEL_KINDS`. For the reviewer's
    #: reading only — it never becomes a field value.
    label_kind: str | None = Field(default=None, max_length=32)
    #: The run itself broke — the model server refused, the transport raised. **Not** the
    #: same as reading nothing, which is `candidates: []`.
    error: str | None = Field(default=None, max_length=2000)


class IdentityCandidateRead(BaseModel):
    mpn: str
    manufacturer: str | None
    package: str | None
    confidence: float
    source_text: str
    note: str | None
    part_id: int | None
    rank: int
    provider: str | None
    model: str | None
    created_at: datetime

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class IntakeDispatchRead(BaseModel):
    """One entry's dispatch standing, and what was proposed for it."""

    intake_id: int
    state: DispatchState
    attempts: int
    #: NULL for every state but `failed`. An `unidentified` entry carries no error,
    #: deliberately — a photograph nobody could read is not a fault. See
    #: `app.models.enums.DispatchState`.
    error: str | None
    label_kind: str | None
    candidates: list[IdentityCandidateRead]


class DispatchResultResponse(BaseModel):
    entry: IntakeDispatchRead


class DispatchQueueStatus(BaseModel):
    counts: dict[DispatchState, int]
    #: Requested and waiting. **Not** the same as `not_requested`, which is every
    #: photograph that could be read if somebody asked and is reported separately for
    #: exactly that reason.
    pending: int
    not_requested: int
    failed: int
    #: Counted separately from `failed` and surfaced separately, because a health check
    #: must not treat "nobody could read this photograph" as breakage.
    unidentified: int
    lease_seconds: int
    max_attempts: int


class DispatchRequestBody(BaseModel):
    intake_id: RowId


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_entry(db: Session, intake_id: int) -> PendingIntake:
    entry = db.get(PendingIntake, intake_id)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "no_such_intake",
                "message": f"no pending intake with id {intake_id}",
            },
        )
    return entry


def _entry_read(db: Session, entry: PendingIntake) -> IntakeDispatchRead:
    return IntakeDispatchRead(
        intake_id=entry.id,
        state=DispatchState(entry.dispatch_state),
        attempts=entry.dispatch_attempts,
        error=entry.dispatch_error,
        label_kind=entry.dispatch_label_kind,
        candidates=[
            IdentityCandidateRead.model_validate(row)
            for row in dispatch.candidates_for(db, intake_id=entry.id)
        ],
    )


def _lease_expiry(entry: PendingIntake) -> datetime:
    """When a just-granted lease runs out.

    Derived from the stored `dispatch_claimed_at` rather than a fresh clock read, so the
    worker's deadline is the same instant the queue will use to decide the lease is gone.
    Two clock reads would put them microseconds apart in the direction that has the
    worker believing it has longer than it does.
    """
    claimed_at = entry.dispatch_claimed_at
    if claimed_at is None:  # pragma: no cover - claim() always writes it
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"reason": "unclaimed", "message": "claimed entry has no claim timestamp"},
        )
    return claimed_at + timedelta(seconds=dispatch.LEASE_SECONDS)


def _hints(db: Session, *, capture_ids: list[int]) -> dict[int, tuple[list[str], list[str]]]:
    """Every claimed capture's barcode texts and OCR lines, in one query.

    One query for the whole batch rather than one per capture, the same choice
    `research.manufacturer_names` makes. Ordered by `id` so the lines arrive in the order
    the reader produced them, which for an OCR pass is roughly reading order and is what
    makes a hint legible to the model rather than a bag of strings.

    Truncated at `MAX_CLAIM_REGIONS` per capture and per kind — see that constant.
    """
    if not capture_ids:
        return {}
    rows = db.execute(
        select(CaptureRegion.capture_id, CaptureRegion.kind, CaptureRegion.text)
        .where(CaptureRegion.capture_id.in_(capture_ids))
        .order_by(CaptureRegion.capture_id, CaptureRegion.id)
    ).all()
    hints: dict[int, tuple[list[str], list[str]]] = {
        capture_id: ([], []) for capture_id in capture_ids
    }
    for capture_id, kind, text in rows:
        barcodes, lines = hints[capture_id]
        target = barcodes if kind == CaptureRegionKind.BARCODE else lines
        if len(target) < MAX_CLAIM_REGIONS:
            target.append(text)
    return hints


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=DispatchQueueStatus)
def read_dispatch_status(db: Session = Depends(get_db)) -> DispatchQueueStatus:
    """Queue depth. One grouped count over an indexed column, cheap enough to poll."""
    counts = dispatch.status_counts(db)
    return DispatchQueueStatus(
        counts=counts,
        pending=counts[DispatchState.PENDING],
        not_requested=counts[DispatchState.NOT_REQUESTED],
        failed=counts[DispatchState.FAILED],
        unidentified=counts[DispatchState.UNIDENTIFIED],
        lease_seconds=dispatch.LEASE_SECONDS,
        max_attempts=dispatch.MAX_DISPATCH_ATTEMPTS,
    )


@router.post("/requests", response_model=DispatchResultResponse)
def request_dispatch(
    request: DispatchRequestBody, db: Session = Depends(get_db)
) -> DispatchResultResponse:
    """Ask for one entry's photograph to be read by a model.

    The route the other two queues have no equivalent of, and the reason is the GPU —
    see the module docstring. Also the requeue door: a `proposed`, `unidentified` or
    `failed` entry is re-offered from zero attempts, which is ADR 0021's
    read-it-again-with-a-better-model path and needs no separate route because it is
    this one.

    Refuses an entry with no photograph rather than letting a worker discover it and
    burn an attempt reporting it.
    """
    entry = _require_entry(db, request.intake_id)
    try:
        dispatch.request(db, entry=entry)
    except DispatchError as error:
        raise HTTPException(
            _REASON_STATUS.get(error.reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
            detail={"reason": error.reason, "message": str(error)},
        ) from error
    response = DispatchResultResponse(entry=_entry_read(db, entry))
    db.commit()
    return response


@router.delete("/requests/{intake_id}", response_model=DispatchResultResponse)
def cancel_dispatch(intake_id: RowId, db: Session = Depends(get_db)) -> DispatchResultResponse:
    """Take an entry back out of the queue.

    Requesting is a person's act and so is changing their mind — and the resource being
    spent is visible to them, so forty queued photographs is a number somebody may want
    to cut down before a drain starts.

    **The candidates already read are kept.** They are the record a person consults to
    decide whether another handover is worth it.
    """
    entry = _require_entry(db, intake_id)
    dispatch.cancel(db, entry=entry)
    response = DispatchResultResponse(entry=_entry_read(db, entry))
    db.commit()
    return response


@router.post("/claims", response_model=DispatchClaimBatch)
def claim_dispatch_work(
    request: DispatchClaimRequest, db: Session = Depends(get_db)
) -> DispatchClaimBatch:
    """Lease up to `limit` entries whose photograph wants reading.

    A POST despite reading like a query, because it **writes**: it takes a lease and
    burns an attempt. A GET that mutated the queue would be retried by every proxy and
    prefetched by every crawler.

    An empty `claims` list is the ordinary answer and not an error — it means nobody has
    asked for a photograph to be read, which on this queue is where a healthy install
    spends nearly all of its life.
    """
    claimed = dispatch.claim(db, worker_id=request.worker_id, limit=request.limit)

    # `capture_id` is non-NULL on anything claimable: `dispatch.request` refuses an
    # entry without one, and `NOT_REQUESTED` is not claimable. The `is not None` guard
    # is a type narrowing rather than a runtime expectation.
    capture_ids = [entry.capture_id for entry in claimed if entry.capture_id is not None]
    hints = _hints(db, capture_ids=capture_ids)
    images = {
        capture_id: dispatch.capture_image(db, capture_id=capture_id) for capture_id in capture_ids
    }

    claims: list[DispatchClaim] = []
    for entry in claimed:
        image = None if entry.capture_id is None else images.get(entry.capture_id)
        if entry.capture_id is None or image is None:  # pragma: no cover - see above
            continue
        barcodes, lines = hints.get(entry.capture_id, ([], []))
        claims.append(
            DispatchClaim(
                intake_id=entry.id,
                capture_id=entry.capture_id,
                capture_sha256=image.sha256,
                media_type=image.media_type,
                barcode_texts=barcodes,
                ocr_lines=lines,
                mpn=entry.mpn,
                attempts=entry.dispatch_attempts,
                lease_expires_at=_lease_expiry(entry),
            )
        )

    batch = DispatchClaimBatch(worker_id=request.worker_id, claims=claims)
    # Committed before the response is written: a lease that is not durable is not a
    # lease, and a claim lost on the way back would leave the worker holding a GPU for a
    # photograph the queue still considers unclaimed.
    db.commit()
    return batch


@router.post("/results", response_model=DispatchResultResponse)
def submit_dispatch_result(
    request: DispatchResultRequest, db: Session = Depends(get_db)
) -> DispatchResultResponse:
    """Record one run's outcome — the identities it proposed, or a failure.

    Note `candidates: []` and `error` are **different submissions**. An empty list says
    the model looked and could name nothing, which settles the entry `unidentified`; an
    error says the run broke, which leaves it claimable until attempts run out.
    Collapsing them would make a model server that was still loading indistinguishable
    from a photograph nobody can read — and would put the second in a health check.
    """
    candidates, failure = request.candidates, request.error
    if (candidates is None) == (failure is None):
        # Both or neither. Refused rather than resolved by precedence: a worker that
        # sent both has a bug, and picking one would record an outcome it did not
        # unambiguously report. Same refusal as the other two submit doors.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "ambiguous_result",
                "message": "send exactly one of `candidates` (a result) or `error` (a failure)",
            },
        )

    entry = _require_entry(db, request.intake_id)
    try:
        if candidates is not None:
            dispatch.record_result(
                db,
                entry=entry,
                candidates=[
                    CandidateReport(
                        mpn=item.mpn,
                        confidence=item.confidence,
                        source_text=item.source_text,
                        manufacturer=item.manufacturer,
                        package=item.package,
                        note=item.note,
                        part_id=item.part_id,
                        rank=item.rank,
                        provider=item.provider,
                        model=item.model,
                    )
                    for item in candidates
                ],
                label_kind=request.label_kind,
            )
        elif failure is not None:
            dispatch.record_failure(db, entry=entry, error=failure)
    except DispatchError as error:
        raise HTTPException(
            _REASON_STATUS.get(error.reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
            detail={"reason": error.reason, "message": str(error)},
        ) from error

    response = DispatchResultResponse(entry=_entry_read(db, entry))
    db.commit()
    return response
