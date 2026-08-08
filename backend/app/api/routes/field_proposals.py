"""`/api/field-proposals` — fields something thinks are missing, for a person to make.

The propose half of authoring. `POST /api/parameter-fields` mints a
`parameter_template` and is the **only** thing that does; this module records that
somebody or something believes one is missing, with the quote that says so and the
nearest existing field it might be a duplicate of. Then a person clicks, and the
click calls that other route.

## There is no create path here, structurally

Not "there is no create path by convention" — there is no field on any request in
this module from which a `parameter_template` could be built, `app.services
.field_proposals` neither imports `create_template` nor constructs a
`ParameterTemplate`, and `POST /{proposal_id}/decision` takes an existing
`field_id` and refuses one that does not resolve. `tests/integration/
test_field_proposals.py` asserts all three, because this is the invariant the whole
feature is shaped around and the one a later edit would quietly undo.

The reason is the duplicate, and it is worth restating where somebody adding a
route will read it: a `parameter_template` is shared by every part in its category.
A second template for one concept fragments parametric search silently — a part
with a value under `on_resistance` reads as having none under `Rds(on)` — and
`substitution_direction` then decides substitutions on the half it can see. A wrong
`base_unit` is the same harm from the other end. Neither ever surfaces as an error.
So the machine's contribution is evidence, and the decision is a person's.

This is the same shape the intake queue already has: a vision run reaches
`PROPOSED` and stops, and a chosen candidate becomes real through the existing
`POST /api/intake/pending/{id}/resolve`. Two doors onto one write is how the
never-auto-accept rule stops being true.

## `POST ""` is a *write* an agent may make, and the only one

`create_parameter_field` stays `Excluded(Reason.AUTHORING)` in
`mcpserver/coverage.py`. This route is what an agent gets instead: it may say "this
datasheet states an on-resistance and you have no field for it, here is the line",
and it may not decide. See ADR 0011 (fields) and ADR 0012 (mcp).

## No `client_op_id`

Keyed on the normalised name, and every field written is a function of what was
sent, so a retry lands the same rows by construction — the reasoning
`app.services.document_text` sets out for text submission, and the same conclusion.
A *replay* would also be wrong here for the same reason it is there: a second pass
under a better model is a legitimate re-submission that must overwrite the quote.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.db.session import get_db
from app.models.enums import FieldProposalStatus, Provenance, SubstitutionDirection, ValueType
from app.models.field_proposals import (
    MAX_BASE_UNIT_LENGTH,
    MAX_CATEGORY_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_FIELD_NAME_LENGTH,
    MAX_MODEL_LENGTH,
    MAX_PROVIDER_LENGTH,
    MAX_QUOTE_LENGTH,
    MAX_STATED_VALUE_LENGTH,
    ParameterFieldProposal,
)
from app.models.parameter import ParameterTemplate
from app.services import field_proposals as proposals

router = APIRouter(prefix="/api/field-proposals", tags=["field-proposals"])

Quote = Annotated[str, StringConstraints(min_length=1, max_length=MAX_QUOTE_LENGTH)]

#: How many sightings one request may carry. A family datasheet covering two dozen
#: parts is the realistic top end; the bound exists so a malformed worker cannot
#: send a megabyte of quotes in one body.
MAX_EVIDENCE_PER_REQUEST = 50


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class EvidenceIn(BaseModel):
    """One document's sighting of the field.

    `quote` is `min_length=1` at the wire level as well as refused by the service,
    which is not belt-and-braces for its own sake: this is the field that makes a
    proposal reviewable at all, and a client that omits it should be told so by the
    schema rather than by a 422 from three layers down.
    """

    quote: Quote = Field(
        description=(
            "The heading, cell or line the field name was read from, verbatim. "
            "Required. If you cannot quote it, you did not read it."
        )
    )
    document_ref: str = Field(
        default="",
        max_length=255,
        description=(
            "Which document said so — a datasheet sha256, normally. Part of the "
            "uniqueness key, so re-reading the same document updates its own "
            "sighting rather than inflating the count. Empty means 'this source has "
            "one observation'."
        ),
    )
    source: Provenance = Field(
        default=Provenance.LLM_INFERRED,
        description="Which kind of source stated it. Ordered by PROVENANCE_PRIORITY.",
    )
    part_id: RowId | None = Field(
        default=None,
        description=(
            "A part the field was stated for, if known. Not part of the uniqueness "
            "key — one family datasheet covers many part numbers, and the count this "
            "produces is of documents."
        ),
    )
    stated_value: str | None = Field(
        default=None,
        max_length=MAX_STATED_VALUE_LENGTH,
        description=(
            "The value printed under the heading, verbatim — the best available hint "
            "about type and unit. Never written to parameter_value: there is no "
            "template to write it against."
        ),
    )
    page: int | None = Field(default=None, ge=1)
    note: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "For the person reviewing: why this source thinks the field is worth "
            "having, what it was unsure about. Never parsed, only shown."
        ),
    )
    provider: str | None = Field(default=None, max_length=MAX_PROVIDER_LENGTH)
    model: str | None = Field(default=None, max_length=MAX_MODEL_LENGTH)


class FieldProposalIn(BaseModel):
    """One proposed field, with everything a person needs to judge it."""

    name: str = Field(
        min_length=1,
        max_length=MAX_FIELD_NAME_LENGTH,
        description=(
            "The field name as the document stated it. Normalised for dedup, so the "
            "same concept spelled three ways is one proposal with three sightings."
        ),
    )
    display_name: str | None = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH)
    suggested_value_type: ValueType | None = None
    suggested_base_unit: str | None = Field(
        default=None,
        max_length=MAX_BASE_UNIT_LENGTH,
        description=(
            "A suggested physical quantity name. Not validated here — an "
            "unrecognised suggestion is something to correct in the create form, and "
            "refusing to store it would throw the quote away with it. "
            "GET /api/parameter-fields/base-units is the accepted list."
        ),
    )
    suggested_category: str | None = Field(
        default=None,
        max_length=MAX_CATEGORY_LENGTH,
        description="A part category slug the field would belong to.",
    )
    suggested_substitution_direction: SubstitutionDirection | None = Field(
        default=None,
        description=(
            "A suggestion only, and deliberately optional with no default: "
            "POST /api/parameter-fields requires it precisely because defaulting a "
            "voltage rating to 'exact' makes substitution wrong by construction, and "
            "a proposal must not launder that decision by arriving with one filled in."
        ),
    )
    evidence: list[EvidenceIn] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_PER_REQUEST,
        description="At least one sighting. A proposal with no quote cannot be reviewed.",
    )


class NearestMatchRead(BaseModel):
    """The existing field this proposal is most likely a duplicate of."""

    template_id: int
    name: str
    display_name: str
    value_type: str
    base_unit: str | None
    #: How it matched — `exact_name`, `display_name`, `synonym`, `tokens`. The UI
    #: shows the reason, because "these share words" and "these are the same symbol
    #: and word for one quantity" deserve different amounts of trust.
    match: str
    score: float


class EvidenceRead(BaseModel):
    id: int
    source: str
    document_ref: str
    part_id: int | None
    quote: str
    stated_value: str | None
    page: int | None
    note: str | None
    provider: str | None
    model: str | None
    created_at: datetime


class FieldProposalRead(BaseModel):
    id: int
    name: str
    name_norm: str
    display_name: str | None
    suggested_value_type: str | None
    suggested_base_unit: str | None
    suggested_category: str | None
    suggested_substitution_direction: str | None
    status: str
    #: How many documents stated it. Derived from the evidence rows, never stored —
    #: the headline signal, and the one number that says "real field" rather than
    #: "one manufacturer's private vocabulary".
    seen_count: int
    nearest: NearestMatchRead | None
    #: The template a person created or merged into, once accepted.
    created_template_id: int | None
    decision_note: str | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    evidence: list[EvidenceRead] = Field(default_factory=list)


class FieldProposalList(BaseModel):
    proposals: list[FieldProposalRead]


class FieldProposalDecision(BaseModel):
    """What a person decided about one proposal.

    **Records a decision; it does not perform one.** `accepted` requires a
    `field_id` that already resolves to a `parameter_template`, which the client
    obtained by calling `POST /api/parameter-fields` itself. That is what keeps the
    number of ways to create a field at one — the same reason the intake queue's
    acceptance goes through the existing `resolve` route.
    """

    status: FieldProposalStatus = Field(
        description="`accepted` or `dismissed`. `pending` is refused — it is not a decision."
    )
    field_id: RowId | None = Field(
        default=None,
        description=(
            "The parameter field this became: the one created through "
            "POST /api/parameter-fields, or the existing one it was merged into. "
            "Required for 'accepted' and refused for 'dismissed'."
        ),
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Why, for whoever sees this proposed again.",
    )


class FieldProposalDecided(BaseModel):
    proposal: FieldProposalRead


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _nearest(db: Session, proposal: ParameterFieldProposal) -> NearestMatchRead | None:
    if proposal.nearest_template_id is None or proposal.nearest_match is None:
        return None
    template = db.get(ParameterTemplate, proposal.nearest_template_id)
    if template is None:
        # The FK is `SET NULL`, so this is only reachable inside a transaction that
        # deleted the template without flushing. Answering `None` is honest: there
        # is no nearest field to show.
        return None
    return NearestMatchRead(
        template_id=template.id,
        name=template.name,
        display_name=template.display_name,
        value_type=template.value_type,
        base_unit=template.base_unit,
        match=proposal.nearest_match,
        score=proposal.nearest_score or 0.0,
    )


def _read(db: Session, proposal: ParameterFieldProposal) -> FieldProposalRead:
    return FieldProposalRead(
        id=proposal.id,
        name=proposal.name,
        name_norm=proposal.name_norm,
        display_name=proposal.display_name,
        suggested_value_type=proposal.suggested_value_type,
        suggested_base_unit=proposal.suggested_base_unit,
        suggested_category=proposal.suggested_category,
        suggested_substitution_direction=proposal.suggested_substitution_direction,
        status=proposal.status,
        seen_count=proposals.seen_count(db, proposal),
        nearest=_nearest(db, proposal),
        created_template_id=proposal.created_template_id,
        decision_note=proposal.decision_note,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
        decided_at=proposal.decided_at,
        evidence=[
            EvidenceRead(
                id=row.id,
                source=row.source,
                document_ref=row.document_ref,
                part_id=row.part_id,
                quote=row.quote,
                stated_value=row.stated_value,
                page=row.page,
                note=row.note,
                provider=row.provider,
                model=row.model,
                created_at=row.created_at,
            )
            for row in proposals.evidence_of(db, proposal)
        ],
    )


def _refusal(error: proposals.FieldProposalError) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"reason": error.reason, "message": str(error)},
    )


def _require(db: Session, proposal_id: RowId) -> ParameterFieldProposal:
    proposal = db.get(ParameterFieldProposal, proposal_id)
    if proposal is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_field_proposal",
                "message": f"no field proposal with id {proposal_id}",
            },
        )
    return proposal


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("", response_model=FieldProposalList)
def list_field_proposals(
    db: Session = Depends(get_db),
    proposal_status: FieldProposalStatus | None = Query(
        default=FieldProposalStatus.PENDING,
        alias="status",
        description=(
            "Which proposals to return. Defaults to the pending ones, which is the "
            "review surface's question. Pass nothing explicitly for all of them."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> FieldProposalList:
    """Proposed fields, likeliest duplicates first.

    Ordered by how strongly each resembles an existing field, so the rows that must
    **not** be waved through are the ones at the top. A proposal with no near match
    is genuinely new information and can wait.
    """
    return FieldProposalList(
        proposals=[
            _read(db, row)
            for row in proposals.list_proposals(db, status=proposal_status, limit=limit)
        ]
    )


@router.get("/{proposal_id}", response_model=FieldProposalRead)
def read_field_proposal(proposal_id: RowId, db: Session = Depends(get_db)) -> FieldProposalRead:
    return _read(db, _require(db, proposal_id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@router.post("", response_model=FieldProposalRead, status_code=status.HTTP_201_CREATED)
def propose_field(request: FieldProposalIn, db: Session = Depends(get_db)) -> FieldProposalRead:
    """Record that a document states a field this install has no template for.

    An upsert on the normalised name, and an upsert per `(source, document_ref)` on
    the evidence, so a nightly pass over the same datasheets adds nothing the second
    time. It also **re-runs the duplicate check**: a template created since the first
    sighting turns "nothing resembles this" into "merge into that one" rather than
    leaving a stale answer a reviewer would trust.

    Creates no `parameter_template` and cannot — see the module docstring.
    """
    try:
        proposal = proposals.record_proposal(
            db,
            name=request.name,
            display_name=request.display_name,
            suggested_value_type=request.suggested_value_type,
            suggested_base_unit=request.suggested_base_unit,
            suggested_category=request.suggested_category,
            suggested_substitution_direction=request.suggested_substitution_direction,
            evidence=[
                proposals.EvidenceReport(
                    quote=row.quote,
                    document_ref=row.document_ref,
                    source=row.source,
                    part_id=row.part_id,
                    stated_value=row.stated_value,
                    page=row.page,
                    note=row.note,
                    provider=row.provider,
                    model=row.model,
                )
                for row in request.evidence
            ],
        )
    except proposals.FieldProposalError as error:
        db.rollback()
        raise _refusal(error) from error
    db.commit()
    return _read(db, proposal)


@router.post("/{proposal_id}/decision", response_model=FieldProposalDecided)
def decide_field_proposal(
    proposal_id: RowId, request: FieldProposalDecision, db: Session = Depends(get_db)
) -> FieldProposalDecided:
    """Record what a person decided. **Creates nothing.**

    `accepted` needs a `field_id` that already resolves, because the field was
    created by the client's own call to `POST /api/parameter-fields` — with every
    guard in `app.services.parameter_fields` applied, including the `base_unit`
    check that is an invariant of the search engine rather than input validation.
    Accepting without one is refused rather than treated as "create it for me": that
    branch is the one a later edit would grow into a second creation path.
    """
    proposal = _require(db, proposal_id)

    if request.status is FieldProposalStatus.PENDING:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "not_a_decision",
                "message": (
                    "'pending' is where a proposal already is, not something to decide. "
                    "Send 'accepted' with the field it became, or 'dismissed'."
                ),
            },
        )

    if request.status is FieldProposalStatus.ACCEPTED:
        if request.field_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "reason": "missing_field_id",
                    "message": (
                        "accepting a proposal records which field it became, so it needs "
                        "field_id. Create the field with POST /api/parameter-fields first — "
                        "this route deliberately cannot create one, because a "
                        "parameter_template is shared by every part in its category and a "
                        "duplicate fragments parametric search silently."
                    ),
                },
            )
        template = db.get(ParameterTemplate, request.field_id)
        if template is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "reason": "unknown_parameter_field",
                    "message": f"no parameter field with id {request.field_id}",
                },
            )
        proposals.accept(proposal, template=template, note=request.note)
    else:
        if request.field_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "reason": "field_id_on_dismissal",
                    "message": (
                        "a dismissed proposal became no field, so field_id means nothing "
                        "here. Send 'accepted' if one was created."
                    ),
                },
            )
        proposals.dismiss(proposal, note=request.note)

    db.commit()
    return FieldProposalDecided(proposal=_read(db, proposal))
