"""The datasheet-research queue: claim a part, report what was found (ADR 0017).

The second work queue, and deliberately the same machinery as the first. Where
`document_text` hands out **documents that have no text**, this hands out **parts
that have no document**, and the two chain without knowing about each other: a
research run that validates a PDF stores it as a `documents` row, which arrives in
the extraction queue as `PENDING` and is picked up by the extraction worker on its
own schedule. Neither worker waits for the other and neither imports the other.

Everything about the lease is copied from `document_text` on purpose — the same
`LEASE_SECONDS` shape, the same "count the attempt when the claim is granted", the
same self-repairing `expire_abandoned` at the top of every claim, the same
pick-then-take compare-and-swap. That module's docstring argues each of those and
the arguments transfer unchanged; what follows is only what differs.

## Two terminal states, and only one of them is a problem

`document_text` has one: `FAILED`. Research has two, because ADR 0017 requires that
"we looked and found nothing" be distinguishable from "something broke".

* `EXHAUSTED` — every provider ran, every candidate was fetched, and none survived
  validation. A genuinely obscure part with no datasheet on the open web reaches
  this, and it is **not an error**. `research_error` is left NULL.
* `FAILED` — the run itself broke, or attempts ran out. `research_error` says what.

`docs/PLAN.md` wants failed enrichment among the deterministic health checks. That
check must count `FAILED` and must not count `EXHAUSTED`, or it fills with parts
nothing is wrong with and stops being read.

## Why `record_result` takes the whole candidate list

Not just the winner. A part that comes back `EXHAUSTED` is a diagnosis waiting to
be made — four `mpn_absent` rejections is a provider returning the wrong part,
one `not_pdf` is a login wall, no rows at all is missing provider coverage — and
none of that is recoverable if only successes are stored. See
`app.models.research`.

The list **replaces** the part's candidates rather than appending to them, keyed on
`(part_id, url)`. A second run is a fresh opinion about the same URLs, not a second
opinion alongside the first, so the row count stays "how many distinct things were
tried" instead of growing with every retry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.models.catalog import Manufacturer, Part
from app.models.enums import ResearchCandidateState, ResearchState
from app.models.research import MAX_SOURCE_LENGTH, MAX_URL_LENGTH, ResearchCandidate
from app.models.types import utcnow

#: How long a claim is good for. Fifteen minutes, matching extraction — but for a
#: different reason worth stating, because the numbers agreeing is a coincidence.
#: Extraction's bound is a big PDF through a slow parser; research's is a cascade of
#: provider calls plus a fetch of every candidate, over the open internet, from a
#: pod whose egress may be slow. Both land in the same order of magnitude.
LEASE_SECONDS = 900

#: How many times one part may be handed out before it is called failed. Three, as
#: extraction — and note this bounds *runs*, not candidates: a single run tries
#: every provider, so three attempts is three full cascades, not three URLs.
MAX_RESEARCH_ATTEMPTS = 3

#: Candidates accepted in one submission. A cascade that proposes more than this
#: has looped; the bound exists because the body is JSON held in memory, exactly as
#: `document_text.MAX_SUBMITTED_PAGES` does.
MAX_SUBMITTED_CANDIDATES = 50


class RejectReason(StrEnum):
    """Why a fetched candidate was refused. The vocabulary, not a constraint.

    `research_candidates.reject_reason` is a plain string column with no `CHECK` and
    no foreign key, so a provider may record a reason this enum does not list and
    nothing breaks. This exists to keep the common ones spelled the same way across
    providers, because "which reason dominates" is the query these rows are for and
    it is useless if three providers spell the same failure differently.
    """

    #: The response was not a PDF. Checked by magic bytes as well as content type,
    #: because manufacturer CDNs mislabel routinely — and because a login wall
    #: returns `200 text/html` and would otherwise pass.
    NOT_PDF = "not_pdf"
    #: Over the byte ceiling.
    TOO_LARGE = "too_large"
    #: A PDF that no extractor could open — truncated, encrypted, malformed.
    PARSE_FAILED = "parse_failed"
    #: **The load-bearing one.** It is a PDF, it parses, and the part number is not
    #: in it. This is what separates the right datasheet from a plausible one, and
    #: it is the check that makes a hallucinated URL harmless rather than merely
    #: unlikely. See ADR 0017.
    MPN_ABSENT = "mpn_absent"
    #: The fetch itself failed — DNS, TLS, a 404, a timeout. Distinct from
    #: `not_pdf`: nothing was served, so no provider is accused of serving garbage.
    FETCH_FAILED = "fetch_failed"


@dataclass(frozen=True)
class CandidateReport:
    """One candidate as a worker reports it.

    A plain dataclass rather than the ORM row, so the service owns the mapping and
    the route's wire type owns validation. `document_sha256` is set only for a
    validated candidate and `reject_reason` only for a rejected one; `record_result`
    refuses the combinations that make no sense rather than storing them.
    """

    source: str
    url: str
    state: ResearchCandidateState
    reject_reason: str | None = None
    document_sha256: str | None = None
    rank: int = 0
    note: str | None = None


class ResearchError(RuntimeError):
    """A submission the queue refuses. `reason` maps to an HTTP status at the route."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def status_counts(session: Session) -> dict[ResearchState, int]:
    """Queue depth by state, every state present even at zero.

    Zeros included for the same reason `document_text.status_counts` includes them:
    a dashboard that omits a key when it is zero cannot distinguish "nothing failed"
    from "the failure count stopped being reported".
    """
    rows = session.execute(
        select(Part.research_state, func.count()).group_by(Part.research_state)
    ).all()
    counted = {ResearchState(state): count for state, count in rows}
    return {state: counted.get(state, 0) for state in ResearchState}


def manufacturer_names(session: Session, *, parts: Sequence[Part]) -> dict[int, str]:
    """Manufacturer names for a claimed batch, in one query, keyed by id.

    A claim has to hand the worker a manufacturer *name*: URL patterns are keyed on
    the manufacturer and an integer id means nothing outside this database. `Part`
    carries `manufacturer_id` and deliberately no relationship, so this is an
    explicit lookup rather than a lazy load — which also keeps it one query for the
    batch instead of one per part.

    Parts with no manufacturer contribute no key, so `dict.get` on a NULL
    `manufacturer_id` yields `None` without a branch at the call site.
    """
    ids = {part.manufacturer_id for part in parts if part.manufacturer_id is not None}
    if not ids:
        return {}
    rows = session.execute(
        select(Manufacturer.id, Manufacturer.name).where(Manufacturer.id.in_(ids))
    ).all()
    return dict(rows)  # type: ignore[arg-type]


def candidates_for(session: Session, *, part_id: int) -> list[ResearchCandidate]:
    """Every candidate tried for one part, best rank first. The part screen's query."""
    return list(
        session.execute(
            select(ResearchCandidate)
            .where(ResearchCandidate.part_id == part_id)
            .order_by(ResearchCandidate.rank, ResearchCandidate.id)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def _claimable(cutoff: datetime) -> ColumnElement[bool]:
    """The predicate for "a worker may take this part now".

    One expression used by both the select that picks and the update that takes, so
    the two cannot drift and the update is a genuine compare-and-swap. Same
    construction, and same reason, as `document_text._claimable`.
    """
    return and_(
        Part.research_attempts < MAX_RESEARCH_ATTEMPTS,
        or_(
            Part.research_state == ResearchState.PENDING,
            # An expired lease. `research_claimed_at` is fixed-width ISO-8601 text
            # (`app.models.types.UtcDateTime`), so lexicographic order is
            # chronological order and this comparison is sound in SQL.
            and_(
                Part.research_state == ResearchState.CLAIMED,
                Part.research_claimed_at < cutoff,
            ),
        ),
    )


def expire_abandoned(
    session: Session, *, now: datetime | None = None, lease_seconds: int = LEASE_SECONDS
) -> int:
    """Fail every claim whose lease ran out with no attempts left. Returns the count.

    Called at the top of every `claim`, so the queue repairs itself as a side effect
    of being used and needs no scheduler to be correct. Without it a thrice-killed
    worker leaves a part in `CLAIMED` with nobody holding it: not pending, not
    failed, not claimable, and absent from every count that would have shown the
    queue had stopped moving.
    """
    moment = now or utcnow()
    cutoff = moment - timedelta(seconds=lease_seconds)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Part)
            .where(
                Part.research_state == ResearchState.CLAIMED,
                Part.research_claimed_at < cutoff,
                Part.research_attempts >= MAX_RESEARCH_ATTEMPTS,
            )
            .values(
                research_state=ResearchState.FAILED,
                research_claimed_at=None,
                research_error=(
                    f"abandoned: {MAX_RESEARCH_ATTEMPTS} leases expired with no result submitted"
                ),
            )
        ),
    )
    session.flush()
    return result.rowcount


def _candidates(session: Session, *, cutoff: datetime, limit: int) -> list[int]:
    """The part ids `claim` will try to take.

    A named function rather than an inlined query for the same two reasons as its
    twin in `document_text`: it keeps the pick and the take visibly separate, and it
    is the seam a test substitutes to hand `claim` a **stale** candidate list — the
    interleave a real concurrent claim produces, which nothing single-threaded can
    otherwise reach, leaving the re-check untested and free to be tidied away.
    """
    return list(
        session.execute(
            select(Part.id)
            .where(_claimable(cutoff))
            # Fresh work before retries; `id` makes the order total, so the batch a
            # given queue state produces is deterministic and therefore testable.
            .order_by(Part.research_attempts, Part.id)
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
) -> list[Part]:
    """Hand out up to `limit` parts needing a datasheet, taking a lease on each.

    `now` and `lease_seconds` are parameters rather than clock reads so the
    dead-worker path is testable without waiting fifteen minutes; the route passes
    neither.

    Pick, then take, and the take re-checks — the compare-and-swap
    `document_text.claim` explains at length. Route handlers are `def`, so FastAPI
    runs them in a threadpool and two concurrent claims are two threads on two
    connections; the `update` repeats `_claimable` so the loser matches nothing and
    gets a shorter batch, rather than both workers researching the same part.
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
            update(Part)
            .where(Part.id.in_(picked), _claimable(cutoff))
            .values(
                research_state=ResearchState.CLAIMED,
                research_claimed_at=moment,
                research_claimed_by=worker_id,
                # Counted on the way out, so a worker that never reports still burns
                # one and a part that kills whatever picks it up runs out.
                research_attempts=Part.research_attempts + 1,
            )
            .returning(Part.id)
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
            select(Part)
            .where(Part.id.in_(granted))
            .order_by(Part.id)
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

    Three refusals, all of them "the worker has a bug" rather than "this part is
    hard". Storing an incoherent report would put a row in the diagnostics table
    that misleads the person the table exists for.
    """
    if len(reports) > MAX_SUBMITTED_CANDIDATES:
        raise ResearchError(
            "too_many_candidates",
            f"{len(reports)} candidates submitted; the ceiling is {MAX_SUBMITTED_CANDIDATES}",
        )
    seen: set[str] = set()
    for report in reports:
        if not report.url or len(report.url) > MAX_URL_LENGTH:
            raise ResearchError("invalid_url", f"url must be 1..{MAX_URL_LENGTH} characters")
        if not report.source or len(report.source) > MAX_SOURCE_LENGTH:
            raise ResearchError(
                "invalid_source", f"source must be 1..{MAX_SOURCE_LENGTH} characters"
            )
        if report.url in seen:
            # Two verdicts for one URL in one run. Refused rather than resolved by
            # last-wins: the unique constraint would silently keep whichever the
            # loop happened to write second, and a worker reporting a URL twice has
            # already contradicted itself.
            raise ResearchError("duplicate_candidate", f"{report.url} reported twice")
        seen.add(report.url)

        if report.state == ResearchCandidateState.VALIDATED and not report.document_sha256:
            raise ResearchError(
                "missing_document",
                f"{report.url} is validated but names no stored document",
            )
        if report.state == ResearchCandidateState.REJECTED and not report.reject_reason:
            # A rejection with no reason is the one that makes `EXHAUSTED`
            # undiagnosable, which is the whole point of storing rejections.
            raise ResearchError(
                "missing_reject_reason", f"{report.url} is rejected but gives no reason"
            )


def record_result(
    session: Session,
    *,
    part: Part,
    candidates: Sequence[CandidateReport],
    now: datetime | None = None,
) -> ResearchState:
    """Store one run's candidates and settle the part's state. Returns that state.

    **The outcome is derived from the candidates, never declared by the worker.** A
    run that validated something is `RESOLVED`; a run that validated nothing is
    `EXHAUSTED`. Letting the worker send the state as well would create a second
    source of truth that could disagree with the rows underneath it, and the rows
    are the evidence.

    Idempotent, and for the same reason as text submission: keyed on
    `(part_id, url)`, and every field written is a function of what was submitted, so
    a retry lands the same rows. A *re-run* legitimately overwrites — a new provider
    finding a datasheet for a part that was `EXHAUSTED` is exactly the upgrade path
    this has to support, and it needs no new machinery because it is this.
    """
    _validate(candidates)
    moment = now or utcnow()

    existing = {row.url: row for row in candidates_for(session, part_id=part.id)}
    for report in candidates:
        row = existing.get(report.url)
        if row is None:
            row = ResearchCandidate(part_id=part.id, url=report.url, created_at=moment)
            session.add(row)
        row.source = report.source
        row.state = report.state
        row.reject_reason = report.reject_reason
        row.document_sha256 = report.document_sha256
        row.rank = report.rank
        row.note = report.note

    resolved = any(report.state == ResearchCandidateState.VALIDATED for report in candidates)
    state = ResearchState.RESOLVED if resolved else ResearchState.EXHAUSTED

    part.research_state = state
    part.research_claimed_at = None
    part.research_claimed_by = None
    # Cleared on both outcomes. `EXHAUSTED` must not carry an error — see the module
    # docstring — and a `RESOLVED` part keeping the message from a previous failed
    # run would show a stale complaint next to a datasheet that is right there.
    part.research_error = None
    session.flush()
    return state


def record_failure(
    session: Session, *, part: Part, error: str, now: datetime | None = None
) -> None:
    """Record that the run itself broke. Distinct from finding nothing.

    Leaves the part claimable if it has attempts left — the queue offers it again —
    and moves it to `FAILED` only once they are spent. That is what makes a
    transient egress failure a retry and a persistent one a health-check entry,
    without the worker having to tell the two apart.
    """
    del now  # the failure's timestamp is the row's `updated_at`; no second clock read
    part.research_claimed_at = None
    part.research_claimed_by = None
    part.research_error = error[:2000]
    part.research_state = (
        ResearchState.FAILED
        if part.research_attempts >= MAX_RESEARCH_ATTEMPTS
        else ResearchState.PENDING
    )
    session.flush()


def requeue(session: Session, *, part: Part) -> None:
    """Offer a part to the queue again, from zero attempts.

    Two uses, one operation, exactly as `document_text.requeue`: retry a `FAILED`
    part once its cause is fixed, and **re-research an `EXHAUSTED` one now that a new
    provider exists**. The second is the upgrade path ADR 0017 depends on — provider
    coverage grows, and the parts that grew coverage should benefit — and it needs
    no new machinery because it is this.

    Candidate rows are deliberately **left in place**. They are the record of what
    was already tried, which is the most useful thing to have when deciding whether
    a re-run is worth it, and `record_result` overwrites them per URL anyway.
    """
    part.research_state = ResearchState.PENDING
    part.research_attempts = 0
    part.research_claimed_at = None
    part.research_claimed_by = None
    part.research_error = None
    session.flush()
