"""The capture-dispatch queue: claim a photograph, report what was read (ADR 0021).

The third work queue, and deliberately the same machinery as the first two. Where
`document_text` hands out **documents that have no text** and `research` hands out
**parts that have no document**, this hands out **parked scans whose photograph
nobody has read**. The three chain without knowing about each other: a dispatch run
that proposes an identity mints a stub `parts` row, which — because
`Part.research_state` defaults to `PENDING` — arrives in the research queue and is
picked up by the research worker on its own schedule, whose validated PDF then
arrives in the extraction queue. No worker waits for another and no worker imports
another.

**This module is a copy, not an abstraction, and that is deliberate.**
`document_text` and `research` are already two independent copies of the same lease
machinery and both docstrings defend it. The reason is that the three queues differ
in exactly the places a shared base class would have to be parameterised — the
subject table, the default state, the terminal states, what counts as a result — and
each difference is load-bearing rather than incidental. A `QueueMixin` would make
every one of them a keyword argument, and the next queue would be added by copying a
call site instead of by reading an argument. Everything about the lease here is
copied from `research` on purpose: the same `LEASE_SECONDS` shape, the same "count
the attempt when the claim is granted", the same self-repairing `expire_abandoned` at
the top of every claim, the same pick-then-take compare-and-swap. Those arguments
transfer unchanged; what follows is only what differs.

## Dispatching is opt-in, which no other queue is

`ResearchState` defaults to `PENDING` and the queue drains itself. `DispatchState`
defaults to `NOT_REQUESTED`, because a run costs a **GPU handover** on a card that is
integral, exclusive and co-tenanted (ADR 0016) — a vision model becoming resident
means some other workload is not. A phone syncing forty labels would otherwise have
quietly queued forty model runs.

So there is a `request` verb here that the other two queues have no equivalent of,
and `NOT_REQUESTED` is excluded from queue depth: a capture nobody asked about is not
work waiting, and counting it would make the depth a number nobody could act on.

## Two terminal states, and only one of them is a problem

Exactly `research`'s `EXHAUSTED`/`FAILED` split.

* `UNIDENTIFIED` — the model looked and could name nothing. A blurred label, a bag
  photographed from the back, a bare part with its marking worn off. **Not an
  error**; `dispatch_error` is left NULL. ADR 0021: "we could not tell what this is"
  is a photograph problem whose fix is another photograph.
* `FAILED` — the run itself broke, or attempts ran out. `dispatch_error` says what.

A health check must count `FAILED` and must not count `UNIDENTIFIED`, or it fills
with photographs nothing is wrong with and stops being read.

## `PROPOSED` is terminal for the machine, and nothing here can resolve an entry

`record_result` writes candidates and moves `dispatch_state`. It does **not** touch
`pending_intakes.status`, `pending_intakes.mpn` or `pending_intakes.resolved_part_id`,
and there is no code path in this module that could:

* `mpn` is what the **barcode** said — a deterministic read off a checksummed
  symbology. Overwriting it with a model's reading would replace the strongest
  evidence on the row with the weakest.
* `resolved_part_id` is what a **person** decided. A machine writing it is the
  never-auto-accept rule broken, at any confidence.
* `status` stays `pending`, so the entry keeps appearing on the worklist until
  somebody looks at it.

A chosen candidate reaches `resolved_part_id` through the ordinary
`POST /api/intake/pending/{id}/resolve` door, pressed by a person. There is no second
acceptance mechanism.

## Why `record_result` takes the whole candidate list

Not just the winner — and for a stronger reason than `research`'s. There, the losers
are diagnostics. Here **the losers are still live options**: ADR 0021's mechanism is
that `datasheet_validation` eliminates a wrong reading by failing to find its part
number in a PDF it actually fetched, so the second and third readings have to survive
long enough to be tested. Storing only the best guess would discard the alternatives
before anything could check them, on the one interface whose measured failure mode is
confidently naming the wrong string on a label that has several.

The list **replaces** the entry's candidates rather than appending to them, keyed on
`(intake_id, mpn)`. A second run is a fresh opinion about the same photograph, not a
second opinion alongside the first, so the row count stays "how many distinct
identities were ever proposed" instead of growing with every retry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.models.captures import Capture
from app.models.dispatch import (
    MAX_MANUFACTURER_LENGTH,
    MAX_MPN_LENGTH,
    MAX_PACKAGE_LENGTH,
    MAX_SOURCE_TEXT_LENGTH,
    IntakeIdentityCandidate,
)
from app.models.documents import Document
from app.models.enums import DispatchState
from app.models.scanning import PendingIntake
from app.models.types import utcnow
from app.services.enrichment.candidates import AUTO_PROMOTE_CONFIDENCE

#: How long a claim is good for. **Thirty minutes, twice research's fifteen**, and the
#: difference is not padding.
#:
#: A research run is a cascade of HTTP calls; a dispatch run may sit behind a **model
#: swap** — the vision model has to become resident on a card that something else may
#: currently hold, which on this deployment means scaling one Deployment to zero and
#: another to one and waiting for weights to load. ADR 0021 measured the read itself
#: at 9 s median and 39 s on the hard case, so the lease is almost entirely allowance
#: for the handover in front of it. A lease that expires during a legitimate model
#: load hands the same photograph to a second worker while the first is still holding
#: the GPU, which is the one interleaving that actively wastes the scarce resource.
LEASE_SECONDS = 1800

#: How many times one entry may be handed out before it is called failed. **Two, where
#: extraction and research allow three**, because each attempt costs a GPU handover and
#: the third is very unlikely to differ: ADR 0021 measured temperature 0 as *not*
#: stability (the same model answered one case and exhausted its budget on the next
#: repeat of it), so a retry is worth one shot and not two. A photograph that fails
#: twice wants a person or a new photograph, not a third weight load.
MAX_DISPATCH_ATTEMPTS = 2

#: Candidates accepted in one submission. Three is `vision.DEFAULT_MAX_CANDIDATES` and
#: this ceiling is deliberately far above it: the bound exists because the body is JSON
#: held in memory, exactly as `document_text.MAX_SUBMITTED_PAGES` and
#: `research.MAX_SUBMITTED_CANDIDATES` do, not to enforce the vision schema's `maxItems`
#: a second time. A worker that submits more than this has looped.
MAX_SUBMITTED_CANDIDATES = 20

#: The highest confidence a vision reading may carry into the database.
#:
#: **Vision confidence must never reach `candidates.AUTO_PROMOTE_CONFIDENCE`**, and
#: this is where that becomes arithmetic rather than a rule somebody remembers.
#: `IdentityCandidate.confidence` is about reading characters off a photograph — glare,
#: focus, a crease through the third digit — while the promotion threshold is
#: calibrated against whether a *datasheet states a value*. They share a 0..1 range and
#: are different quantities, and mixing them would smuggle photo quality into a
#: parameter's trust (ADR 0021).
#:
#: The clamp is not defensive tidiness. ADR 0021 measured qwen3-vl:8b reporting **0.95
#: on an answer that was the item's FCC ID**, a string that is not a part number, from
#: a label that also carries a Canadian IC number, an OUI and a serial. A model's
#: self-reported certainty is not calibrated and the number is useful only for ranking
#: one reading against another.
MAX_VISION_CONFIDENCE = AUTO_PROMOTE_CONFIDENCE - 0.01


#: Why an entry cannot be dispatched. One reason, and there is deliberately not a
#: second.
#:
#: The obvious companion — "the `capture_id` points at a row that is gone" — **cannot
#: happen** and so is not checked for. `pending_intakes.capture_id` is `SET NULL` on
#: delete and `captures.document_id` is `CASCADE`, and foreign keys are enforced (see
#: `tests/integration/test_db_pragmas.py`), so deleting either the capture or its blob
#: row nulls this column rather than leaving it dangling. A check for it would be a
#: branch no test could reach honestly, which is worse than absent: the next reader
#: would believe the queue defends against something it has never been able to meet.
NO_CAPTURE = "no_capture"


@dataclass(frozen=True)
class CandidateReport:
    """One proposed identity as a worker reports it.

    A plain dataclass rather than the ORM row, so the service owns the mapping and the
    route's wire type owns validation — `research.CandidateReport`'s reasoning
    unchanged.

    `part_id` is the stub the worker minted for this candidate, if it minted one. It
    is stored on the candidate and **never** copied to
    `pending_intakes.resolved_part_id`; see the module docstring.
    """

    mpn: str
    confidence: float
    source_text: str
    manufacturer: str | None = None
    package: str | None = None
    note: str | None = None
    part_id: int | None = None
    rank: int = 0
    provider: str | None = None
    model: str | None = None


class DispatchError(RuntimeError):
    """A submission the queue refuses. `reason` maps to an HTTP status at the route."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def status_counts(session: Session) -> dict[DispatchState, int]:
    """Queue depth by state, every state present even at zero.

    Zeros included for the same reason `research.status_counts` includes them: a
    dashboard that omits a key when it is zero cannot distinguish "nothing failed"
    from "the failure count stopped being reported".

    Note `NOT_REQUESTED` is reported here and is *not* queue depth — see
    `pending_dispatch_count`. It is included because "how many photographs could be
    read if somebody asked" is exactly the number the panel offering the button needs.
    """
    rows = session.execute(
        select(PendingIntake.dispatch_state, func.count()).group_by(PendingIntake.dispatch_state)
    ).all()
    counted = {DispatchState(state): count for state, count in rows}
    return {state: counted.get(state, 0) for state in DispatchState}


def candidates_for(session: Session, *, intake_id: int) -> list[IntakeIdentityCandidate]:
    """Every identity proposed for one entry, best rank first. The intake panel's query."""
    return list(
        session.execute(
            select(IntakeIdentityCandidate)
            .where(IntakeIdentityCandidate.intake_id == intake_id)
            .order_by(IntakeIdentityCandidate.rank, IntakeIdentityCandidate.id)
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class CaptureImage:
    """How to fetch a capture's pixels, without any pixels.

    Two strings, and deliberately not bytes. The worker fetches the image itself from
    `GET /api/documents/{sha256}` like every other worker fetches its input, so neither
    this service nor the route ever holds an image — which is what keeps ADR 0005
    structural rather than a promise.

    `media_type` comes off the `documents` row rather than being sniffed, matching
    `VisionRequest.media_type`'s own rule: `blobstore.store` already checked the magic
    bytes on the way in, so re-deriving it would be a second opinion that is allowed to
    disagree with the row.
    """

    sha256: str
    media_type: str


def capture_image(session: Session, *, capture_id: int) -> CaptureImage | None:
    """The blob behind a capture, or None if the capture is gone.

    One explicit join rather than a relationship, matching `research.manufacturer_names`:
    `Capture` carries `document_id` and no relationship, so this is a lookup instead of a
    lazy load — and one query per claimed entry rather than a lazy load per attribute.
    """
    row = session.execute(
        select(Document.sha256, Document.media_type)
        .join(Capture, Capture.document_id == Document.id)
        .where(Capture.id == capture_id)
    ).one_or_none()
    return None if row is None else CaptureImage(sha256=row[0], media_type=row[1])


def dispatchable(entry: PendingIntake) -> bool:
    """Is there a photograph on this entry for a model to look at?

    Checked when a run is *requested* rather than when it is claimed, deliberately. A
    worker that claims an entry with no photograph has already burned an attempt and
    has to report a failure to give the lease back; refusing at the door means the
    person pressing the button learns immediately, and the queue never contains work
    that cannot be done.

    Needs no session: `capture_id` is the whole question — see `NO_CAPTURE` for why the
    row behind it cannot be missing.
    """
    return entry.capture_id is not None


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------


def request(session: Session, *, entry: PendingIntake) -> DispatchState:
    """Ask for this entry's photograph to be read. Returns the resulting state.

    The verb the other two queues do not have, and the reason is the GPU — see the
    module docstring. Idempotent on an entry already `PENDING` or `CLAIMED`: pressing
    the button twice must not reset a live lease or burn the attempt count, because the
    second press is what a person does when the first appeared to do nothing.

    A `PROPOSED`, `UNIDENTIFIED` or `FAILED` entry is re-offered **from zero attempts**,
    which makes this the requeue verb as well. That is one operation rather than two on
    purpose: "read this again now that the model is better" and "read this for the
    first time" are the same act from the queue's point of view, and ADR 0021's own
    upgrade path — a re-read after a model swap — needs no new machinery because it is
    this.
    """
    if not dispatchable(entry):
        raise DispatchError(
            NO_CAPTURE,
            f"intake {entry.id} carries no photograph, so there is nothing to read",
        )

    if entry.dispatch_state in {DispatchState.PENDING, DispatchState.CLAIMED}:
        # Already queued or already being worked. Left exactly as it is — see above.
        return DispatchState(entry.dispatch_state)

    entry.dispatch_state = DispatchState.PENDING
    entry.dispatch_attempts = 0
    entry.dispatch_claimed_at = None
    entry.dispatch_claimed_by = None
    entry.dispatch_error = None
    session.flush()
    return DispatchState.PENDING


def cancel(session: Session, *, entry: PendingIntake) -> DispatchState:
    """Take an entry back out of the queue, to `NOT_REQUESTED`.

    Exists because requesting is a person's act and so is changing their mind, and
    because the resource being spent is visible to them: forty queued photographs is a
    number somebody may want to cut down before a drain starts.

    **Candidate rows are left in place**, exactly as `research.requeue` leaves its
    candidates. They are the record of what was already read, which is the most useful
    thing to have when deciding whether a re-read is worth it, and `record_result`
    overwrites them per MPN anyway.

    A `CLAIMED` entry is cancellable and that is not a race worth preventing: the
    worker holding the lease will submit into a `NOT_REQUESTED` row, `record_result`
    will store what it read, and the state it lands in is the honest one. Refusing here
    would leave the entry stuck for the whole thirty-minute lease with no way for the
    person watching to act on it.
    """
    entry.dispatch_state = DispatchState.NOT_REQUESTED
    entry.dispatch_attempts = 0
    entry.dispatch_claimed_at = None
    entry.dispatch_claimed_by = None
    entry.dispatch_error = None
    session.flush()
    return DispatchState.NOT_REQUESTED


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def _claimable(cutoff: datetime) -> ColumnElement[bool]:
    """The predicate for "a worker may take this entry now".

    One expression used by both the select that picks and the update that takes, so the
    two cannot drift and the update is a genuine compare-and-swap. Same construction,
    and same reason, as `research._claimable`.

    Note `NOT_REQUESTED` is absent from it, which is the whole opt-in mechanism: an
    entry nobody asked about matches nothing here however long it sits.
    """
    return and_(
        PendingIntake.dispatch_attempts < MAX_DISPATCH_ATTEMPTS,
        or_(
            PendingIntake.dispatch_state == DispatchState.PENDING,
            # An expired lease. `dispatch_claimed_at` is fixed-width ISO-8601 text
            # (`app.models.types.UtcDateTime`), so lexicographic order is
            # chronological order and this comparison is sound in SQL.
            and_(
                PendingIntake.dispatch_state == DispatchState.CLAIMED,
                PendingIntake.dispatch_claimed_at < cutoff,
            ),
        ),
    )


def expire_abandoned(
    session: Session, *, now: datetime | None = None, lease_seconds: int = LEASE_SECONDS
) -> int:
    """Fail every claim whose lease ran out with no attempts left. Returns the count.

    Called at the top of every `claim`, so the queue repairs itself as a side effect of
    being used and needs no scheduler to be correct. Without it a twice-killed worker
    leaves an entry in `CLAIMED` with nobody holding it: not pending, not failed, not
    claimable, and absent from every count that would have shown the queue had stopped
    moving.
    """
    moment = now or utcnow()
    cutoff = moment - timedelta(seconds=lease_seconds)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(PendingIntake)
            .where(
                PendingIntake.dispatch_state == DispatchState.CLAIMED,
                PendingIntake.dispatch_claimed_at < cutoff,
                PendingIntake.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS,
            )
            .values(
                dispatch_state=DispatchState.FAILED,
                dispatch_claimed_at=None,
                dispatch_error=(
                    f"abandoned: {MAX_DISPATCH_ATTEMPTS} leases expired with no result submitted"
                ),
            )
        ),
    )
    session.flush()
    return result.rowcount


def _candidates(session: Session, *, cutoff: datetime, limit: int) -> list[int]:
    """The intake ids `claim` will try to take.

    A named function rather than an inlined query for the same two reasons as its twins
    in `document_text` and `research`: it keeps the pick and the take visibly separate,
    and it is the seam a test substitutes to hand `claim` a **stale** candidate list —
    the interleave a real concurrent claim produces, which nothing single-threaded can
    otherwise reach, leaving the re-check untested and free to be tidied away.
    """
    return list(
        session.execute(
            select(PendingIntake.id)
            .where(_claimable(cutoff))
            # Fresh work before retries; `id` makes the order total, so the batch a
            # given queue state produces is deterministic and therefore testable.
            .order_by(PendingIntake.dispatch_attempts, PendingIntake.id)
            .limit(limit)
        ).scalars()
    )


def claim(
    session: Session,
    *,
    worker_id: str,
    limit: int = 1,
    now: datetime | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> list[PendingIntake]:
    """Hand out up to `limit` entries whose photograph wants reading, leasing each.

    `now` and `lease_seconds` are parameters rather than clock reads so the dead-worker
    path is testable without waiting half an hour; the route passes neither.

    Pick, then take, and the take re-checks — the compare-and-swap
    `document_text.claim` explains at length. Route handlers are `def`, so FastAPI runs
    them in a threadpool and two concurrent claims are two threads on two connections;
    the `update` repeats `_claimable` so the loser matches nothing and gets a shorter
    batch, rather than two workers loading a model each for the same photograph.
    """
    moment = now or utcnow()
    cutoff = moment - timedelta(seconds=lease_seconds)

    expire_abandoned(session, now=moment, lease_seconds=lease_seconds)

    picked = _candidates(session, cutoff=cutoff, limit=limit)
    if not picked:
        return []

    # `RETURNING` so the batch is exactly what this statement flipped. Inferring it
    # afterwards from `(claimed_by, claimed_at)` is not sound — that tuple is not
    # unique per call, and two claims by one worker in the same microsecond each
    # report everything that worker holds. `document_text.claim` carries the full
    # account of that bug; it is the same statement here.
    granted = set(
        session.execute(
            update(PendingIntake)
            .where(PendingIntake.id.in_(picked), _claimable(cutoff))
            .values(
                dispatch_state=DispatchState.CLAIMED,
                dispatch_claimed_at=moment,
                dispatch_claimed_by=worker_id,
                # Counted on the way out, so a worker that never reports still burns
                # one and an entry that kills whatever picks it up runs out.
                dispatch_attempts=PendingIntake.dispatch_attempts + 1,
            )
            .returning(PendingIntake.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    session.flush()
    if not granted:
        return []

    return list(
        session.execute(
            select(PendingIntake)
            .where(PendingIntake.id.in_(granted))
            .order_by(PendingIntake.id)
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _validate(reports: Sequence[CandidateReport]) -> None:
    """Refuse a candidate list that cannot mean what it says.

    All of these are "the worker has a bug" rather than "this photograph is hard".
    Storing an incoherent report would put a proposal in front of the person the
    proposal exists for, which is worse than refusing the submission and retrying.
    """
    if len(reports) > MAX_SUBMITTED_CANDIDATES:
        raise DispatchError(
            "too_many_candidates",
            f"{len(reports)} candidates submitted; the ceiling is {MAX_SUBMITTED_CANDIDATES}",
        )
    seen: set[str] = set()
    for report in reports:
        if not report.mpn.strip() or len(report.mpn) > MAX_MPN_LENGTH:
            raise DispatchError("invalid_mpn", f"mpn must be 1..{MAX_MPN_LENGTH} characters")
        if not report.source_text.strip() or len(report.source_text) > MAX_SOURCE_TEXT_LENGTH:
            # **The load-bearing refusal.** ADR 0021: if the model cannot quote the
            # characters it read, it did not read them, and the quote is what a
            # reviewer checks instead of taking its word. A candidate with an empty
            # `source_text` is not a weak proposal, it is an unreviewable one.
            raise DispatchError(
                "missing_source_text",
                f"{report.mpn} quotes no source text; "
                f"it must be 1..{MAX_SOURCE_TEXT_LENGTH} characters",
            )
        if report.manufacturer is not None and len(report.manufacturer) > MAX_MANUFACTURER_LENGTH:
            raise DispatchError(
                "invalid_manufacturer",
                f"manufacturer must be at most {MAX_MANUFACTURER_LENGTH} characters",
            )
        if report.package is not None and len(report.package) > MAX_PACKAGE_LENGTH:
            raise DispatchError(
                "invalid_package", f"package must be at most {MAX_PACKAGE_LENGTH} characters"
            )
        if not 0.0 <= report.confidence <= 1.0:
            # `vision._confidence` already normalises a percentage and refuses
            # anything past 100, so a value arriving here out of range means the
            # submission did not come through that parser at all.
            raise DispatchError(
                "invalid_confidence",
                f"{report.mpn} reports confidence {report.confidence}, which is not in 0..1",
            )
        if report.mpn in seen:
            # Two readings of one part number in one run. Refused rather than resolved
            # by last-wins: the unique constraint would silently keep whichever the
            # loop happened to write second, and a worker reporting the same MPN twice
            # has already contradicted itself.
            raise DispatchError("duplicate_candidate", f"{report.mpn} reported twice")
        seen.add(report.mpn)


def record_result(
    session: Session,
    *,
    entry: PendingIntake,
    candidates: Sequence[CandidateReport],
    label_kind: str | None = None,
    now: datetime | None = None,
) -> DispatchState:
    """Store one run's candidates and settle the entry's state. Returns that state.

    **The outcome is derived from the candidates, never declared by the worker.** A run
    that named something is `PROPOSED`; a run that named nothing is `UNIDENTIFIED`.
    Letting the worker send the state as well would create a second source of truth
    that could disagree with the rows underneath it, and the rows are the evidence.

    **Nothing here touches `status`, `mpn` or `resolved_part_id`** — see the module
    docstring. `PROPOSED` is terminal for the machine; the entry stays on the worklist
    and a person chooses through the ordinary resolve door.

    Idempotent, keyed on `(intake_id, mpn)`, and every field written is a function of
    what was submitted, so a retry lands the same rows. A *re-run* legitimately
    overwrites — reading the same photograph with a better model is exactly the upgrade
    path ADR 0021 describes, and it needs no new machinery because it is this.
    """
    _validate(candidates)
    moment = now or utcnow()

    existing = {row.mpn: row for row in candidates_for(session, intake_id=entry.id)}
    for report in candidates:
        row = existing.get(report.mpn)
        if row is None:
            row = IntakeIdentityCandidate(intake_id=entry.id, mpn=report.mpn, created_at=moment)
            session.add(row)
        row.manufacturer = report.manufacturer
        row.package = report.package
        # Clamped, not trusted. See `MAX_VISION_CONFIDENCE`: a vision reading must not
        # be able to carry a number that the promotion rules would act on, and the
        # measured case is 0.95 on a wrong answer.
        #
        # The clamp collapses ties above the bar, and that costs nothing: **`rank` is
        # what orders these rows**, stored from the model's own ordering rather than
        # derived from this number. Clamping rather than refusing follows
        # `vision._confidence`'s precedent — refusing discards an otherwise correct
        # reading and fails the whole photograph over a number that decides nothing.
        row.confidence = min(report.confidence, MAX_VISION_CONFIDENCE)
        row.source_text = report.source_text
        row.note = report.note
        row.part_id = report.part_id
        row.rank = report.rank
        row.provider = report.provider
        row.model = report.model

    state = DispatchState.PROPOSED if candidates else DispatchState.UNIDENTIFIED

    entry.dispatch_state = state
    entry.dispatch_claimed_at = None
    entry.dispatch_claimed_by = None
    # Cleared on both outcomes. `UNIDENTIFIED` must not carry an error — see the module
    # docstring — and a `PROPOSED` entry keeping the message from a previous failed run
    # would show a stale complaint next to the candidates it did produce.
    entry.dispatch_error = None
    entry.dispatch_label_kind = label_kind
    session.flush()
    return state


def record_failure(
    session: Session, *, entry: PendingIntake, error: str, now: datetime | None = None
) -> None:
    """Record that the run itself broke. Distinct from reading nothing.

    Leaves the entry claimable if it has attempts left — the queue offers it again —
    and moves it to `FAILED` only once they are spent. That is what makes a model
    server that was still loading a retry and a genuinely broken one a health-check
    entry, without the worker having to tell the two apart.
    """
    del now  # the failure's timestamp is the row's `updated_at`; no second clock read
    entry.dispatch_claimed_at = None
    entry.dispatch_claimed_by = None
    entry.dispatch_error = error[:2000]
    entry.dispatch_state = (
        DispatchState.FAILED
        if entry.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS
        else DispatchState.PENDING
    )
    session.flush()
