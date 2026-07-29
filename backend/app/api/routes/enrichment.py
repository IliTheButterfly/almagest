"""`/api/enrichment/candidates` — the review queue, and the only door humans use
to close what `app.services.enrichment.candidates` refused to guess.

Every rule in that module exists to make a wrong value **queue instead of
write**. That is only safe if the queue actually gets worked, so this module's
whole job is to make working it fast: **grouped by part** (fixing a family's
five candidates is one pass, not five), **with every candidate's evidence
attached** (a `datasheet_table`/`llm_inferred` row's `note` already carries the
quoted line it was read from — `app.services.enrichment.cross_check.review_note`
put it there — and this module never re-derives or drops it), and with the
priority order's own pick surfaced rather than merely implied by list order.

**Accept and correct both go through `candidates.promote(..., force=True)`.**
`force` exists in that module for exactly one reason — "reachable only from a
human decision" — and a reviewer clicking a button in this screen *is* that
decision, for the row shown and for nothing else. A **correction** is recorded
first as a fresh `manual` candidate (never as an edit to the row a source
wrote — that would erase which source said what) and only then promoted; by
`PROVENANCE_PRIORITY` a `manual` row already outranks every automated source,
so once it lands here it is the value nothing but another human can move.

**Accepting or correcting one candidate for a field closes the *whole field*,
not just that row.** Every other pending candidate for the same
`(part, template)` is dismissed alongside it — sticky, not deleted, so the
losing reading stays visible in history — because the review screen shows a
field's candidates as one group and a human who chose between them has, by
construction, already looked at the rest. Leaving them pending would reopen a
question the same click just answered.

**Bulk accept is the same one-candidate operation repeated**, not a second
code path: `docs/PLAN.md`'s only concession to volume is "twelve parts from one
family decoded identically", not a bypass of any rule above. A failure on one
id (already resolved, unparseable, gone) is reported for that id and does not
abort its neighbours — a batch of otherwise-good picks must not be held hostage
by one stale one.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.db.session import get_db
from app.models.catalog import Part
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import CandidateStatus, Provenance
from app.models.parameter import ParameterChoice, ParameterTemplate, ParameterValue
from app.services.enrichment import candidates as candidate_rules

router = APIRouter(prefix="/api/enrichment/candidates", tags=["enrichment"])

#: A human correction is an assertion, not a measurement — there is nothing to
#: report a confidence *less than* certain about, and `parameters.set_numeric`
#: records whatever is given verbatim.
MANUAL_CONFIDENCE = 1.0


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class EnrichmentCandidateRead(BaseModel):
    """One source's proposal, exactly as `parameter_value_candidate` holds it.

    Deliberately carries `note` and `source_ref` verbatim rather than
    reshaping them: `note` is where a model or table extraction's quoted
    evidence already lives (see `cross_check.review_note`), and a value nobody
    can trace to that text is not reviewable — it is a prompt to guess, which
    is the one thing this screen must not become.
    """

    id: RowId
    source: str
    #: A datasheet's content hash, an MPN-decoder family, a provider response
    #: id — whatever `record()` was given. Shown verbatim so a
    #: `datasheet_table` row's reference is visible even though this phase has
    #: no `GET /api/datasheets/{sha256}` yet to link it to.
    source_ref: str
    confidence: float
    raw_value: str
    #: Resolved display label, when this is an enum facet.
    choice_key: str | None
    status: str
    review_reason: str | None
    #: An OCR'd/model-read identity or a printed marking — never eligible for
    #: acceptance verbatim by count alone; the route still allows a human to
    #: accept it, because that is precisely what this queue is for.
    requires_human: bool
    note: str | None
    created_at: datetime


class EnrichmentFieldGroup(BaseModel):
    """One `(part, template)`'s pending candidates, plus what is there already.

    `recommended_candidate_id` is `candidates[0].id` when there is more than
    one row — a pure function of `PROVENANCE_PRIORITY`, the same ordering
    `candidate_rules.pending()` already sorts by, surfaced explicitly rather
    than left for the reviewer to infer from list order.
    """

    template_id: RowId
    template_name: str
    template_unit: str | None
    existing_raw_input: str | None
    existing_provenance: str | None
    existing_confidence: float | None
    recommended_candidate_id: RowId | None
    candidates: list[EnrichmentCandidateRead]


class EnrichmentPartGroup(BaseModel):
    part_id: RowId
    part_name: str
    part_mpn: str | None
    fields: list[EnrichmentFieldGroup]


class EnrichmentQueueResponse(BaseModel):
    #: Every pending row matching the filter, ignoring the `limit` on parts.
    total_candidates: int
    #: Distinct parts with at least one pending candidate, ignoring `limit`.
    total_parts: int
    parts: list[EnrichmentPartGroup]


class EnrichmentCorrectRequest(BaseModel):
    raw_value: str = Field(min_length=1, max_length=2000)
    note: str | None = Field(default=None, max_length=2000)


class EnrichmentBulkAcceptRequest(BaseModel):
    candidate_ids: list[RowId] = Field(min_length=1, max_length=500)


class EnrichmentBulkAcceptResult(BaseModel):
    candidate_id: RowId
    accepted: bool
    #: Why not, when `accepted` is false. `None` on success.
    reason: str | None = None


class EnrichmentBulkAcceptResponse(BaseModel):
    results: list[EnrichmentBulkAcceptResult]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _candidate_read(db: Session, row: ParameterValueCandidate) -> EnrichmentCandidateRead:
    choice_key = None
    if row.choice_id is not None:
        choice = db.get(ParameterChoice, row.choice_id)
        choice_key = choice.key if choice is not None else None
    return EnrichmentCandidateRead(
        id=row.id,
        source=row.source,
        source_ref=row.source_ref,
        confidence=row.confidence,
        raw_value=row.raw_value,
        choice_key=choice_key,
        status=row.status,
        review_reason=row.review_reason,
        requires_human=row.requires_human,
        note=row.note,
        created_at=row.created_at,
    )


def _field_group(db: Session, part: Part, template: ParameterTemplate) -> EnrichmentFieldGroup:
    """The current state of one field: what is stored, and what is still pending.

    Re-queried after every write rather than assembled from what the caller
    already had in hand, so accept/correct/dismiss all return the same
    post-action truth — including the case where an action leaves the field
    with zero pending rows, which is the caller's signal to drop it from view.
    """
    rows = [row for row in candidate_rules.pending(db, part=part) if row.template_id == template.id]
    existing = db.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id,
            ParameterValue.template_id == template.id,
        )
    ).scalar_one_or_none()
    return EnrichmentFieldGroup(
        template_id=template.id,
        template_name=template.name,
        template_unit=template.base_unit,
        existing_raw_input=existing.raw_input if existing is not None else None,
        existing_provenance=existing.provenance if existing is not None else None,
        existing_confidence=existing.confidence if existing is not None else None,
        recommended_candidate_id=rows[0].id if len(rows) > 1 else None,
        candidates=[_candidate_read(db, row) for row in rows],
    )


# ---------------------------------------------------------------------------
# Internals shared by accept / correct / bulk-accept
# ---------------------------------------------------------------------------


def _require_candidate(db: Session, candidate_id: int) -> ParameterValueCandidate:
    row = db.get(ParameterValueCandidate, candidate_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "message": f"no candidate with id {candidate_id}"},
        )
    return row


def _unacceptable_reason(row: ParameterValueCandidate) -> tuple[str, str] | None:
    """Why `accept` must refuse this row verbatim, if it must. `(reason, message)`.

    Delegates the predicate to `candidate_rules.is_promotable` rather than
    re-deriving it, because the two must agree: a second copy of "could this be
    stored" drifts, and the drift shows up as a 500 from a button this screen
    offered. Both refusals are *correctable*, never dead ends — the raw text is
    kept and the reviewer types a value that can be stored.
    """
    if candidate_rules.is_promotable(row):
        return None
    if candidate_rules.is_one_sided(row):
        # It parsed; it is a bound rather than a value. `parameters.set_numeric`
        # refuses it because a null-bounded row matches no range query at all, so
        # accepting it would file the part under a rating it never appears at.
        return (
            "one_sided_limit",
            row.note
            or "that reads as a one-sided limit, not a value; correct it to a value or a range",
        )
    return (
        "unparseable",
        row.note or "this candidate's raw value could not be parsed; correct it instead",
    )


def _dismiss_losing_siblings(
    db: Session, part: Part, template: ParameterTemplate, *, keep_id: int
) -> None:
    """After one candidate for a field is promoted, close the rest of the group.

    `candidate_rules.promote()` already marks an agreeing sibling `SUPERSEDED`
    (closed, no human judgement recorded) and leaves a disagreeing one
    `PENDING`/`FIELD_OCCUPIED` (open, because the promotion rules alone never
    saw a human look at it). This screen groups a field's candidates together
    precisely so a human *does* look at all of them at once — so once one is
    chosen, whatever is still `PENDING` for the same field has also been seen
    and loses, and is dismissed rather than left to reopen the same question
    the accepting click just answered. `dismiss()` is sticky and non-destructive,
    so the losing reading stays visible in history rather than disappearing.
    """
    for row in candidate_rules.pending(db, part=part):
        if row.template_id != template.id or row.id == keep_id:
            continue
        candidate_rules.dismiss(db, row)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=EnrichmentQueueResponse)
def list_enrichment_queue(
    db: Session = Depends(get_db),
    part_id: RowId | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200, description="Cap on distinct parts returned."),
) -> EnrichmentQueueResponse:
    """The review queue, grouped by part then by field.

    Grouping is free: `candidate_rules.pending()` already sorts
    `(part_id, template_id, -priority, -confidence, id)`, so bucketing the
    already-ordered list preserves the "obvious click is the top of the list"
    property within each field without a second sort here.
    """
    part_filter = None
    if part_id is not None:
        part_filter = db.get(Part, part_id)
        if part_filter is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"reason": "unknown_part", "message": f"no part with id {part_id}"},
            )

    rows = candidate_rules.pending(db, part=part_filter)

    by_part: dict[int, dict[int, list[ParameterValueCandidate]]] = {}
    part_order: list[int] = []
    for row in rows:
        fields = by_part.setdefault(row.part_id, {})
        if row.part_id not in part_order:
            part_order.append(row.part_id)
        fields.setdefault(row.template_id, []).append(row)

    groups: list[EnrichmentPartGroup] = []
    for pid in part_order[:limit]:
        part = db.get(Part, pid)
        if part is None:  # pragma: no cover - FK-guaranteed
            continue
        field_groups: list[EnrichmentFieldGroup] = []
        for template_id, field_rows in by_part[pid].items():
            template = db.get(ParameterTemplate, template_id)
            if template is None:  # pragma: no cover - FK-guaranteed
                continue
            existing = db.execute(
                select(ParameterValue).where(
                    ParameterValue.part_id == pid,
                    ParameterValue.template_id == template_id,
                )
            ).scalar_one_or_none()
            field_groups.append(
                EnrichmentFieldGroup(
                    template_id=template.id,
                    template_name=template.name,
                    template_unit=template.base_unit,
                    existing_raw_input=existing.raw_input if existing is not None else None,
                    existing_provenance=existing.provenance if existing is not None else None,
                    existing_confidence=existing.confidence if existing is not None else None,
                    recommended_candidate_id=(field_rows[0].id if len(field_rows) > 1 else None),
                    candidates=[_candidate_read(db, row) for row in field_rows],
                )
            )
        groups.append(
            EnrichmentPartGroup(
                part_id=part.id, part_name=part.name, part_mpn=part.mpn, fields=field_groups
            )
        )

    return EnrichmentQueueResponse(
        total_candidates=len(rows), total_parts=len(part_order), parts=groups
    )


@router.post("/{candidate_id}/accept", response_model=EnrichmentFieldGroup)
def accept_enrichment_candidate(
    candidate_id: RowId, db: Session = Depends(get_db)
) -> EnrichmentFieldGroup:
    """Take this candidate's value exactly as its source wrote it.

    Provenance on `parameter_value` stays the candidate's own source — accepting
    is agreeing with what was read, not asserting it yourself. Use **correct**
    when the value itself needs to change; that is what earns `manual`.
    """
    row = _require_candidate(db, candidate_id)
    if row.status != CandidateStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "not_pending",
                "message": f"candidate {candidate_id} is already {row.status}",
            },
        )
    refusal = _unacceptable_reason(row)
    if refusal is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": refusal[0], "message": refusal[1]},
        )
    part = db.get(Part, row.part_id)
    template = db.get(ParameterTemplate, row.template_id)
    if part is None or template is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason": "not_found"})

    candidate_rules.promote(db, row, force=True)
    _dismiss_losing_siblings(db, part, template, keep_id=row.id)
    db.commit()
    return _field_group(db, part, template)


@router.post("/{candidate_id}/correct", response_model=EnrichmentFieldGroup)
def correct_enrichment_candidate(
    candidate_id: RowId, request: EnrichmentCorrectRequest, db: Session = Depends(get_db)
) -> EnrichmentFieldGroup:
    """A human's replacement value. Recorded as a fresh `manual` candidate, then
    promoted — never as an edit to the row a source actually wrote, which would
    erase the evidence of what that source said."""
    row = _require_candidate(db, candidate_id)
    part = db.get(Part, row.part_id)
    template = db.get(ParameterTemplate, row.template_id)
    if part is None or template is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason": "not_found"})

    note = f"corrected in review; the {row.source} reading was {row.raw_value!r}"
    if request.note:
        note = f"{note}; {request.note}"

    manual = candidate_rules.record(
        db,
        part,
        template,
        request.raw_value,
        source=Provenance.MANUAL,
        confidence=MANUAL_CONFIDENCE,
        source_ref=f"review:candidate-{row.id}",
        note=note,
    )
    refusal = _unacceptable_reason(manual)
    if refusal is not None:
        # The `manual` row written just above is rolled back with the request, so
        # a refused correction leaves no candidate behind. `>=50V` lands here:
        # `FilterIn.value` advertises that syntax for *searching*, so a reviewer
        # typing it into the correction box is expected, and the message has to
        # say what to type instead rather than merely refuse.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": refusal[0], "message": refusal[1]},
        )

    candidate_rules.promote(db, manual, force=True)
    _dismiss_losing_siblings(db, part, template, keep_id=manual.id)
    db.commit()
    return _field_group(db, part, template)


@router.post("/{candidate_id}/dismiss", response_model=EnrichmentCandidateRead)
def dismiss_enrichment_candidate(
    candidate_id: RowId, db: Session = Depends(get_db)
) -> EnrichmentCandidateRead:
    """A human said no to this one reading. Sticks across re-runs of the source
    that produced it; does not touch any sibling candidate for the same field."""
    row = _require_candidate(db, candidate_id)
    if row.status != CandidateStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "not_pending",
                "message": f"candidate {candidate_id} is already {row.status}",
            },
        )
    candidate_rules.dismiss(db, row)
    db.commit()
    return _candidate_read(db, row)


@router.post("/bulk-accept", response_model=EnrichmentBulkAcceptResponse)
def bulk_accept_enrichment_candidates(
    request: EnrichmentBulkAcceptRequest, db: Session = Depends(get_db)
) -> EnrichmentBulkAcceptResponse:
    """Accept many candidates in one call — the common case of a whole decoded
    family being obviously right. Each id is independent: one stale or
    unparseable id is reported and skipped, never abandoning the rest of the
    batch, and each accepted id closes its own field exactly as the single
    accept route does (siblings dismissed, provenance kept as the source's own).
    """
    results: list[EnrichmentBulkAcceptResult] = []
    for candidate_id in request.candidate_ids:
        row = db.get(ParameterValueCandidate, candidate_id)
        if row is None:
            results.append(
                EnrichmentBulkAcceptResult(
                    candidate_id=candidate_id, accepted=False, reason="not_found"
                )
            )
            continue
        if row.status != CandidateStatus.PENDING:
            results.append(
                EnrichmentBulkAcceptResult(
                    candidate_id=candidate_id, accepted=False, reason="not_pending"
                )
            )
            continue
        refusal = _unacceptable_reason(row)
        if refusal is not None:
            results.append(
                EnrichmentBulkAcceptResult(
                    candidate_id=candidate_id, accepted=False, reason=refusal[0]
                )
            )
            continue
        part = db.get(Part, row.part_id)
        template = db.get(ParameterTemplate, row.template_id)
        if part is None or template is None:  # pragma: no cover - FK-guaranteed
            results.append(
                EnrichmentBulkAcceptResult(
                    candidate_id=candidate_id, accepted=False, reason="missing_part_or_template"
                )
            )
            continue

        candidate_rules.promote(db, row, force=True)
        _dismiss_losing_siblings(db, part, template, keep_id=row.id)
        results.append(EnrichmentBulkAcceptResult(candidate_id=candidate_id, accepted=True))

    db.commit()
    return EnrichmentBulkAcceptResponse(results=results)
