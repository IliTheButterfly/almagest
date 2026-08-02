"""Wire types shared by more than one route module.

Kept small on purpose — a route's request and response models belong next to the
route. What lands here is the handful of shapes that would otherwise be defined
twice and drift: a lot looks the same whether it was reached by moving stock, by
reading a part, or by reading a bin, and three spellings of that would mean three
different answers to "how much is in there".
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.limits import GridIndex, GridSpan
from app.models.catalog import Part
from app.models.enums import SizeClass
from app.models.stock import StockLot
from app.models.storage import Location
from app.services.requirements.matching import Candidate, SubstitutionReason, Suggestion
from app.services.requirements.parser import Requirement
from app.services.search.query_builder import Filter, Mode, SearchQuery


class ReplayableResponse(BaseModel):
    """Base for every write response that `app.api.idempotency` can store.

    `replayed` is the only difference a client can observe between having the
    work done and being handed the answer from an earlier identical request —
    and it has to be observable, because "your take was recorded just now" and
    "your take was already recorded a minute ago" are otherwise identical
    responses, and a UI that cannot tell them apart will offer an undo for a
    movement whose undo window closed long ago.
    """

    replayed: bool = Field(
        default=False,
        description=(
            "True when this is the stored response of an earlier request carrying "
            "the same client_op_id; no new movement was recorded."
        ),
    )


class LotRead(BaseModel):
    """One physical package of one part at one location."""

    id: int
    part_id: int
    #: What the part *is*, carried on the lot because the screens that list lots
    #: are exactly the screens that cannot afford a request per row. Without it
    #: a drawer renders "250 / part 4" — the landing screen for every tag tap
    #: and every QR, showing a primary key, so two lots in one bin cannot be
    #: told apart without opening each.
    part_name: str | None = None
    part_mpn: str | None = None
    location_id: int
    #: Read from `stock_lots.qty_milli_cached`. **Never** a `SUM(delta_milli)`
    #: over the ledger — that is the query that stops being sub-second somewhere
    #: around 200k rows, and it is on every screen.
    qty_milli: int
    qty_reserved_milli: int
    status: str
    packaging_id: int | None = None
    batch_code: str | None = None
    serial: str | None = None
    date_code: str | None = None
    unit_cost_micro: int | None = None
    currency: str | None = None
    #: Derived here and now from the location tree, never stored on the lot and
    #: never read off a tag: a container that moves would make an encoded path a
    #: lie the moment the drawer changed cabinet.
    location_label_path: str | None = None


def lot_read(session: Session, lot: StockLot, part: Part | None = None) -> LotRead:
    """Render a lot for the wire, resolving its location path and its part's name.

    `Session.get` is an identity-map lookup, so rendering every lot in one bin
    costs one query for the location, not one per lot.

    **`part` is passed in by callers rendering many lots at once.** The identity
    map alone is not enough: a request session starts *empty*, so the first read
    of each distinct part is a query — a drawer holding a 4k7 and a 10k costs
    two, which is exactly the case this name exists to disambiguate. (An earlier
    version of this comment blamed `commit` expiry; that is wrong for this app,
    which sets `expire_on_commit=False` in `app/db/session.py`. The N+1 is real
    for the simpler reason.) A caller that has already loaded the parts hands
    them over; one that has not still gets the right answer, one query at a
    time.
    """
    location = session.get(Location, lot.location_id)
    if part is None:
        part = session.get(Part, lot.part_id)
    return LotRead(
        id=lot.id,
        part_id=lot.part_id,
        part_name=part.name if part is not None else None,
        part_mpn=part.mpn if part is not None else None,
        location_id=lot.location_id,
        qty_milli=lot.qty_milli_cached,
        qty_reserved_milli=lot.qty_reserved_milli_cached,
        status=lot.status,
        packaging_id=lot.packaging_id,
        batch_code=lot.batch_code,
        serial=lot.serial,
        date_code=lot.date_code,
        unit_cost_micro=lot.unit_cost_micro,
        currency=lot.currency,
        location_label_path=location.label_path if location is not None else None,
    )


class SlotSpecIn(BaseModel):
    """One desired compartment — a base cell, or a merged rectangular region.

    Shared between `PUT /api/container-types/{id}/slot-template` (the type's
    reusable canvas) and `POST /api/locations/{id}/reapply-layout` (one
    instance's own copy of it): both are "here is the complete desired
    layout", and the request shape a merge or a split produces is identical
    either way.
    """

    row_idx: GridIndex
    col_idx: GridIndex
    row_span: GridSpan = 1
    col_span: GridSpan = 1
    #: `None` only makes sense when this cell is exactly what the generator
    #: would already produce there; a merge or a relabel must name it.
    slot_label: str | None = Field(default=None, max_length=64)
    size_class: SizeClass | None = None
    inner_volume_mm3: float | None = Field(default=None, gt=0)


class SlotSpecOut(BaseModel):
    row_idx: int
    col_idx: int
    row_span: int
    col_span: int
    slot_label: str
    size_class: str | None
    inner_volume_mm3: float | None
    sort_order: int


class FilterIn(BaseModel):
    template: str = Field(description="`parameter_template.name`, e.g. 'capacitance'")
    value: str = Field(
        description=(
            "Interpreted according to the template. Numeric templates accept the "
            "full shorthand grammar ('4k7', '20-30uF', '>=50V'); enum templates "
            "accept a choice key or any alias, comma-separated for OR."
        )
    )


class PartQueryRequest(BaseModel):
    """Everything that narrows a set of parts, shared by search and by facets.

    **Shared rather than duplicated, because a facet count has to describe
    exactly the set search returns.** The first version of the facets request
    listed its own fields and omitted two of these — `mode` and `part_kind` — so
    in substitute mode every count described the *search*-mode set instead. The
    counts stayed plausible, which is what made it bad: a panel whose numbers
    disagree with its own results teaches the user that the counts are
    decorative, and then they stop reading them.

    Inheriting means adding a narrowing field to search cannot silently leave
    facets behind. Only pagination differs, so only pagination lives downstream.
    """

    filters: list[FilterIn] = Field(default_factory=list)
    text: str | None = None
    category: str | None = Field(default=None, description="Category slug; includes descendants")
    part_kind: str | None = None
    in_stock_only: bool = False
    include_stubs: bool = True
    mode: Mode = Field(
        default="search",
        description=(
            "'search' matches a requirement; 'substitute' finds parts that would "
            "satisfy it, using each template's substitution_direction."
        ),
    )

    def to_query(self, *, limit: int = 50, offset: int = 0) -> SearchQuery:
        """The executor's query. One construction site, so search and facets
        cannot interpret the same request differently."""
        return SearchQuery(
            filters=tuple(Filter(f.template, f.value) for f in self.filters),
            text=self.text,
            category_slug=self.category,
            part_kind_slug=self.part_kind,
            in_stock_only=self.in_stock_only,
            include_stubs=self.include_stubs,
            mode=self.mode,
            limit=limit,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Requirements and their candidates
# ---------------------------------------------------------------------------
#
# Shared rather than defined per route for the reason this module exists: two
# route modules answer with these — `POST /api/requirements/suggest` for a batch
# of prose lines and `GET /api/projects/{id}/bom/suggestions` for a project's
# unmatched lines — and they are the *same* answer to the same question. Two
# spellings of it would mean a client rendering a suggestion twice, and the
# `is_substitute`/`reasons` pairing drifting apart in one of them.


class RequirementFilterRead(BaseModel):
    """One predicate the description was read as."""

    template: str
    #: A `parameter_choice.key` for an enum, the value text for a numeric — the
    #: same string `POST /api/search/parts` takes in `filters[].value`, so a client
    #: can hand the whole set straight back to search unchanged.
    value: str
    #: The words this came from, verbatim. A predicate nobody can trace back to
    #: something the user wrote cannot be reviewed.
    source_text: str
    #: `deterministic` (an exact lookup in the value grammar or a curated
    #: spelling) or `interpreted` (a model's reading, never overriding the first).
    origin: str
    confidence: float


class RequirementRejectionRead(BaseModel):
    """Text that *was* read and refused, with a reason a UI can route on.

    Distinct from `residue` on purpose: residue is words nothing accounted for, a
    rejection is a reading that was refused. A megafarad and an unknown word are
    different problems with different next actions.
    """

    source_text: str
    reason: str
    message: str
    template: str | None = None
    candidates: list[str] = Field(default_factory=list)


class RequirementRead(BaseModel):
    """What a description was understood to mean. **Not a part, and not an answer.**"""

    #: Verbatim input, always preserved.
    text: str
    #: Units wanted, or null for **unspecified** — not defaulted to 1, because
    #: `3x 10k` says three and `10k 0603` says nothing.
    quantity: int | None
    category: str | None
    filters: list[RequirementFilterRead]
    #: A part-number-shaped token, verbatim. A **lookup key**, never an identity:
    #: that a description contains `LM358N` is not evidence the catalogue's
    #: `LM358N` is the part meant.
    mpn: str | None
    mpn_norm: str | None
    #: Words nothing accounted for. The whole signal for "would a model help here".
    residue: list[str]
    rejections: list[RequirementRejectionRead]
    notes: list[str]
    #: The weakest field's confidence, so one guessed field drags the line down.
    confidence: float
    #: `none` / `deterministic` / `mixed` / `interpreted`.
    provenance: str
    is_actionable: bool
    #: Whether *everything* in the text was accounted for. The honest companion to
    #: `confidence`: a line can be 1.0 confident and not complete, and a UI showing
    #: only the first is lying by omission.
    is_complete: bool


class SubstitutionReasonRead(BaseModel):
    """Why one predicate is satisfied — a rendering of the predicate SQL applied.

    Not an independent judgement and not a model's opinion: `direction` is the
    template's own `substitution_direction`, `offered` is read off the candidate's
    `parameter_value`, and the executor had already proved the predicate before
    this sentence was written. That is what makes a suggestion trustworthy instead
    of magical.
    """

    template: str
    display_name: str
    #: `higher_ok` / `lower_ok` / `range_overlap` / `exact`.
    direction: str
    required: str
    offered: str
    explanation: str


class PartCandidateRead(BaseModel):
    """One part the filter returned, with the numbers the ranking used."""

    #: 1-based within its own list. Every candidate in `in_stock` outranks every
    #: candidate in `not_stocked` — the split *is* the first ranking term.
    rank: int
    part_id: int
    name: str
    mpn: str | None
    description: str | None
    is_stub: bool
    category_id: int | None
    #: From `stock_lots.qty_milli_cached`, never a ledger sum.
    qty_milli: int
    qty_reserved_milli: int
    lot_count: int
    location_count: int
    is_in_stock: bool
    #: True when this part was reached through `mode="substitute"` — it *satisfies*
    #: the requirement rather than matching it as written. `reasons` says how.
    is_substitute: bool
    #: Whether free stock (quantity less reservations) covers what the line needs,
    #: or null when nothing said how much is wanted.
    covers_required: bool | None
    #: Log-ratio distance from the requested value, summed over numeric filters.
    #: 0.0 means the part's own interval overlaps everything asked for.
    distance: float
    #: Populated for a substitute, empty for an exact match — which needs no
    #: explanation, being what was asked for.
    reasons: list[SubstitutionReasonRead]


class SuggestionLineRead(BaseModel):
    """One line's answer. **Nothing here is accepted; every candidate is a proposal.**

    Accepting one is an ordinary BOM edit through the existing route: `PUT
    /api/projects/{project_id}/bom` with `{"edits": [{"id": bom_line_id,
    "part_id": <the chosen part_id>}]}`, which sets `is_match_confirmed` because a
    human choosing a part through that route *is* the confirmation. Rejecting is
    calling nothing at all — the line keeps its `description` and stays unmatched,
    which is a normal state and not an error.
    """

    #: Position in the request, so twenty answers map back to twenty inputs.
    index: int
    #: Set when the line came from `bom_lines`; the id to accept a candidate onto.
    bom_line_id: int | None
    text: str
    #: `stocked` / `order` / `no_match` / `not_actionable`.
    outcome: str
    #: The sentence to show. `order` is the one that matters most: you own nothing
    #: that satisfies this, and here is what does.
    message: str
    required_milli: int | None
    requirement: RequirementRead
    in_stock: list[PartCandidateRead]
    #: Parts that satisfy the requirement and that you **do not have**. Returned
    #: separately rather than folded in, because ordering them is a different
    #: action from picking them.
    not_stocked: list[PartCandidateRead]
    #: True when the matching set was larger than the fetch cap, so these lists are
    #: a shortlist. `POST /api/search/parts` gives a true total.
    truncated: bool


def requirement_read(requirement: Requirement) -> RequirementRead:
    """Render a parsed requirement. Also the whole of `/api/requirements/parse`."""
    return RequirementRead(
        text=requirement.text,
        quantity=requirement.quantity,
        category=requirement.category_slug,
        filters=[
            RequirementFilterRead(
                template=item.template,
                value=item.value,
                source_text=item.source_text,
                origin=item.origin.value,
                confidence=item.confidence,
            )
            for item in requirement.filters
        ],
        mpn=requirement.mpn,
        mpn_norm=requirement.mpn_norm,
        residue=list(requirement.residue),
        rejections=[
            RequirementRejectionRead(
                source_text=item.source_text,
                reason=item.reason,
                message=item.message,
                template=item.template,
                candidates=list(item.candidates),
            )
            for item in requirement.rejections
        ],
        notes=list(requirement.notes),
        confidence=requirement.confidence,
        provenance=requirement.provenance.value,
        is_actionable=requirement.is_actionable,
        is_complete=requirement.is_complete,
    )


def _reason_read(reason: SubstitutionReason) -> SubstitutionReasonRead:
    return SubstitutionReasonRead(
        template=reason.template,
        display_name=reason.display_name,
        direction=reason.direction.value,
        required=reason.required,
        offered=reason.offered,
        explanation=reason.explanation,
    )


def _candidate_read(candidate: Candidate) -> PartCandidateRead:
    return PartCandidateRead(
        rank=candidate.rank,
        part_id=candidate.part.id,
        name=candidate.part.name,
        mpn=candidate.part.mpn,
        description=candidate.part.description,
        is_stub=candidate.part.is_stub,
        category_id=candidate.part.category_id,
        qty_milli=candidate.qty_milli,
        qty_reserved_milli=candidate.qty_reserved_milli,
        lot_count=candidate.lot_count,
        location_count=candidate.location_count,
        is_in_stock=candidate.is_in_stock,
        is_substitute=candidate.is_substitute,
        covers_required=candidate.covers_required,
        distance=candidate.distance,
        reasons=[_reason_read(reason) for reason in candidate.reasons],
    )


def suggestion_read(index: int, suggestion: Suggestion) -> SuggestionLineRead:
    """Render one `Suggestion` for the wire. One renderer, two routes."""
    return SuggestionLineRead(
        index=index,
        bom_line_id=suggestion.bom_line_id,
        text=suggestion.requirement.text,
        outcome=suggestion.outcome.value,
        message=suggestion.message,
        required_milli=suggestion.required_milli,
        requirement=requirement_read(suggestion.requirement),
        in_stock=[_candidate_read(candidate) for candidate in suggestion.in_stock],
        not_stocked=[_candidate_read(candidate) for candidate in suggestion.not_stocked],
        truncated=suggestion.truncated,
    )
