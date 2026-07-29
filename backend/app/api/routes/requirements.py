"""`/api/requirements` — prose in, ranked candidates out. **Its own router.**

Not on `projects`: a requirement is not project-scoped. "What do I already have
that can level-shift 3.3 V to 5 V" is a question about the catalogue and the
shelves, and hanging it under `/api/projects/{id}` would force inventing a project
to ask it. The project-scoped door exists too — `GET /api/projects/{id}/bom/
suggestions`, which answers the same question for one project's unmatched lines —
and both render through `app.api.schemas.suggestion_read`, so the two can never
answer differently.

Not on `search` either, though it is much closer. `search.py`'s contract is
"structured filters in, parts out": every one of its inputs is something a client
built deliberately. Putting a prose door on that router would read as though
`/api/search/parts` accepts prose, and the entire architecture depends on the
opposite being visible — the translation is a *separate* stage whose output is a
`SearchQuery` a human can inspect, which is why `requirement` is returned
alongside the candidates on every line.

**Both routes are pure reads and write nothing**, so neither takes a
`client_op_id`: there is no movement to replay and no row to double-create. That
is the whole reason a suggestion is safe to ask for speculatively — see
`SuggestionLineRead` for how one is accepted, which is an ordinary BOM edit
through a route that already exists.

## What a model is and is not doing here

Nothing, today. `app.services.requirements.parser` reads most real lines outright,
and what it cannot read it lists in `requirement.residue` — returned on every
line, so a caller can see when a model *would* help without one having been
called. When a provider exists (ADR 0005: a Job that releases the GPU, never the
API process) it will fill in the middle term only. It will never pick a part:
membership is `app.services.search.query_builder` and nothing else, and a
suggestion is a candidate for a human either way.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.limits import CandidateLimit, QtyMilli
from app.api.schemas import RequirementRead, SuggestionLineRead, requirement_read, suggestion_read
from app.db.session import get_db
from app.services.requirements import matching
from app.services.requirements.matching import RequirementInput

router = APIRouter(prefix="/api/requirements", tags=["requirements"])

#: One description. Generous for a BOM `Description` column joined to a `Value`,
#: and far short of anything that is really a paragraph of prose — at which point
#: the answer is a worklist item, not a longer parse.
_TEXT_MAX = 500


class RequirementLineIn(BaseModel):
    """One line to answer."""

    text: str = Field(
        min_length=1,
        max_length=_TEXT_MAX,
        description=(
            "A description, not a part number: '3x 10k 1% 0603 resistor', "
            "'100nF 50V X7R 0603', 'a dual op-amp, rail-to-rail, SOIC-8'."
        ),
    )
    #: How much is wanted. Omitted means "whatever the text said" (`3x` says
    #: three), and if the text said nothing either then **nothing is assumed** —
    #: `covers_required` comes back null rather than being answered against an
    #: invented quantity of one.
    required_milli: QtyMilli | None = None


class SuggestionRequest(BaseModel):
    """A batch. Twenty lines is the case this exists for.

    Batched because an agent emits a whole BOM at once and twenty round trips is
    the wrong shape — but also because the work genuinely shares state: one
    vocabulary snapshot, one `parameter_template` map and one stock cache serve
    every line, so twenty lines that all want the same 0603 resistor read its
    stock once. See `app.services.requirements.matching.suggest_batch`.
    """

    lines: list[RequirementLineIn] = Field(min_length=1, max_length=matching.MAX_BATCH)
    #: Candidates per list, per line. Small by default because a suggestion is
    #: read by a human; `POST /api/search/parts` is the door for a full result set.
    limit: CandidateLimit = matching.DEFAULT_LIMIT
    #: Stub parts are included by default: a stub with 500 in a drawer is a real
    #: answer, and the ranking already prefers a curated row over it.
    include_stubs: bool = True


class SuggestionBatchResponse(BaseModel):
    #: One entry per request line, in request order, carrying `index` as well so a
    #: client that reorders still maps answers back to inputs.
    lines: list[SuggestionLineRead]


class RequirementParseRequest(BaseModel):
    """Descriptions to translate, with no matching at all."""

    lines: list[str] = Field(min_length=1, max_length=matching.MAX_BATCH)


class RequirementParseResponse(BaseModel):
    requirements: list[RequirementRead]


@router.post("/parse", response_model=RequirementParseResponse)
def parse_requirements(
    request: RequirementParseRequest, db: Session = Depends(get_db)
) -> RequirementParseResponse:
    """Translate descriptions into structured requirements. **No search runs.**

    The half of the feature worth having on its own: it shows what a line was
    understood to mean before anything is matched against it, which is what a UI
    needs to render the parse as somebody types, and what makes the refusals
    (`rejections`) and the admissions (`residue`) reviewable.

    Never errors on unreadable input. `parse("that thing Dave used on the mixer
    board")` returns a requirement with the text preserved, the residue listed and
    `is_actionable: false` — a normal outcome, because `bom_lines.part_id` is
    nullable and losing the line is worse than not understanding it.
    """
    parsed = matching.parse_batch(db, request.lines)
    return RequirementParseResponse(
        requirements=[requirement_read(requirement) for requirement in parsed]
    )


@router.post("/suggest", response_model=SuggestionBatchResponse)
def suggest_parts(
    request: SuggestionRequest, db: Session = Depends(get_db)
) -> SuggestionBatchResponse:
    """What you own that satisfies each line, then what you would have to order.

    Every candidate came out of `app.services.search.query_builder` — the same
    executor `/api/search/parts` uses, in `mode="search"` for an exact match and
    `mode="substitute"` for a part that *satisfies* the requirement per each
    template's `substitution_direction`. The ranking reorders that result and can
    never extend it.

    **A line with nothing in stock is the answer worth having.** `outcome:
    "order"` with a populated `not_stocked` says "you own nothing that satisfies
    this, and here is what does" — which is the list that becomes a purchase.
    `outcome: "no_match"` is a different and worse thing: nothing in the catalogue
    is this part at all, so no amount of buying the parts you know about will fix
    the line.

    Nothing is accepted here. Accepting a candidate is a BOM edit through the
    existing route — see `SuggestionLineRead`.
    """
    suggestions = matching.suggest_batch(
        db,
        [
            RequirementInput(text=line.text, required_milli=line.required_milli)
            for line in request.lines
        ],
        limit=request.limit,
        include_stubs=request.include_stubs,
    )
    return SuggestionBatchResponse(
        lines=[suggestion_read(index, suggestion) for index, suggestion in enumerate(suggestions)]
    )
