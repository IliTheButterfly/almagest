"""A `Requirement` becomes ranked candidates — and **the SQL decides which**.

This is the last stage of the sentence the front door started:

    prose  ->  a structured Requirement  ->  the existing filter executor  ->  here

and it is the stage where `CLAUDE.md`'s deterministic rule is actually enforced, so
it is the one that must not be clever. There is **no matcher in this module**.
Membership is `app.services.search.query_builder.execute` and nothing else; every
part offered came out of a `SearchQuery`. What this module adds is three things
that are not membership decisions:

1. **an ordering** over the executor's own result;
2. **an availability split**, because "based on what is available" is the question
   being asked, and "you own nothing that satisfies this" is the most valuable
   answer a BOM tool ever gives;
3. **an explanation** of why a substitute qualifies, rendered from the predicate
   the executor already applied.

## Ranking is separate, and cosmetic by construction

`_rank` is one `sorted()` call over a list the executor produced. A sort is a
permutation: it cannot add a part the filter excluded and it cannot drop one it
included, which is the whole reason the ranking is expressed as a sort key rather
than as a scoring pass that assembles its own list. `tests/integration/
test_suggestions.py` pins the property from the other side — a part engineered to
win every ranking term is still absent when one filter excludes it.

The key, in order, and why each term is in it:

1. **An exact match before a substitute.** A part that satisfies the requirement
   as written needs no judgement from the user; a substitute is a decision they
   have to make. Ranking a substitute above a literal match would be presuming on
   their behalf, which is the one thing substitution is not allowed to do.

   This term is **redundant with term 2 today, deliberately.** A part is reached
   only through `mode="substitute"` exactly when some numeric filter failed the
   search-mode overlap test, and that is the same condition `_distance` returns
   non-zero for — so `kind is EXACT` and `distance == 0.0` currently coincide, and
   `test_suggestions.py` pins that they do. Keeping both is the same belt-and-
   braces the value parser uses for a unit misread: the two are computed by
   different code (SQL in `query_builder._numeric_predicate`, Python in
   `_distance`), and if they ever drift this term is what keeps a literal match
   above a substitute while the drift is found.
2. **Closeness to the requested value**, as a log-ratio distance summed over the
   numeric filters, and **zero whenever the candidate's interval overlaps the
   requested one**. Log rather than linear because component values are
   logarithmic — 22 µF is nearer to 20 µF than 47 µF is, across any decade — and
   the direct consequence is that the *least* over-specified substitute wins: a
   50 V part is a better stand-in for a 25 V requirement than a 1 kV part, which
   is bigger, dearer and no more correct.
3. **Free stock, descending.** `qty_milli_cached` minus the reserved cache, so a
   quantity already promised to a build does not make a lot look like the easy
   answer. Convenience, so it sits below correctness of fit.
4. **A curated part before a stub.** A stub is an admission that nobody has
   described this part yet; offering it above a real row implies knowledge that
   is not there.
5. **The executor's own ordering**, as the final tie-break — position in the
   result list, which is bm25 relevance when the requirement carried a part
   number and stock-then-name otherwise. Total, because that ordering ends in
   `parts.id`, so the list is stable between identical calls.

**Availability is deliberately not a term.** "In stock first" is the *partition*
instead, and that is strictly stronger: two lists mean no ordering bug can ever
float a part nobody owns above one on a shelf, whereas a first sort term would
only make it unlikely. It also stops the two mechanisms disagreeing — a term and a
split expressing the same preference is one of them being redundant, and the
redundant one is the one that rots.

`covers_required` is not a term either: it is monotone in free stock, so it would
reorder nothing. It rides along as a label because "you need 3 and can draw on
500" is worth saying.

## Availability agrees with `in_stock_only` by construction

The two executor passes run with `in_stock_only=False` and the split is done here
on `qty_milli_cached > 0` — the identical predicate `SearchQuery.in_stock_only`
compiles to. One pass per mode therefore answers both halves, instead of two
passes answering one each, and `test_suggestions.py` asserts the split agrees with
what `in_stock_only=True` returns so the two can never drift.

Order matters for the truncation, too: the executor's own `_ordered` puts stocked
rows first, so when a requirement matches more parts than the fetch cap, the rows
that fall off the end are the ones nobody owns.

## What is deliberately not here

**No model, and no second definition of "matched".** A part number in the
requirement is handed to the executor as `SearchQuery.text` — a filter, ANDed with
every other predicate — rather than looked up by equality on `parts.mpn_norm`
beside the parametric query. An MPN lookup running *alongside* the filter is
exactly how a suggestion starts being offered that the filter excluded: ask for
`LM358N SOIC-8` when the catalogue's LM358N is DIP-8 and the honest answer is that
nothing matches, not that the part number wins.

A requirement with nothing to search on (`_is_searchable`) reaches no query at
all. This is not defensive: an empty `SearchQuery` is a request for the entire
catalogue, so a description nobody could parse would come back as "here are five
parts", which is the worst possible answer.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from elec_value_parser import ValueParseError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import SubstitutionDirection, ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate, ParameterValue
from app.models.stock import StockLot
from app.services.requirements.parser import (
    DeterministicRequirementParser,
    Requirement,
    RequirementFilter,
)
from app.services.requirements.vocabulary import load_vocabulary
from app.services.search import query_builder
from app.services.search.query_builder import Mode, SearchQuery
from app.services.search.value_parser import parse_for_template

#: Candidates per list, per requirement. Small because a suggestion is something a
#: human reads: twenty options is not a shortlist, it is the search results.
#: The ceiling is `app.api.limits.CandidateLimit`, which is where every bound a
#: request field carries lives; this is only the default a caller gets for free.
DEFAULT_LIMIT = 5

#: Lines in one batch. An agent emitting twenty at once is the case this exists
#: for; a hundred is five times the largest anyone described, and each line costs
#: two executor queries plus one parameter read.
MAX_BATCH = 100

#: How many rows to draw from each executor pass before ranking. A superset,
#: because the ranking has to see more than it returns or terms 2-6 would be
#: decided by the SQL's truncation instead. Bounded, because the alternative is
#: reading a whole matching catalogue into memory to reorder five rows out of it.
_FETCH_MULTIPLE = 4
_MAX_FETCH = 200

#: Part ids per stock aggregate. SQLite binds one parameter per id and the batch
#: cache can accumulate thousands across a hundred requirements.
_STOCK_CHUNK = 500


class MatchKind(StrEnum):
    """How a candidate came to be a candidate. Both are the executor's answer."""

    #: `mode="search"` — it matches the requirement as written.
    EXACT = "exact"
    #: `mode="substitute"` — it *satisfies* the requirement, per each template's
    #: `substitution_direction`. Reached only through the same executor with the
    #: operator table swapped; there is no second query engine.
    SUBSTITUTE = "substitute"


class Outcome(StrEnum):
    """The answer to "can I build this", in one word."""

    #: At least one part you own satisfies the requirement.
    STOCKED = "stocked"
    #: Parts satisfying it exist in the catalogue, and you have none of them.
    #: **This is the outcome that turns into an order**, so it is a distinct word
    #: rather than an empty candidate list.
    ORDER = "order"
    #: The filter returned nothing at all, in either mode. Not "you are out of
    #: stock" — nothing in the catalogue is this part, so somebody has to add one.
    NO_MATCH = "no_match"
    #: Nothing in the description could be turned into a query. A worklist item,
    #: never an error: `bom_lines.part_id` is nullable, and a line nobody
    #: understood is better kept with its text than dropped.
    NOT_ACTIONABLE = "not_actionable"


@dataclass(frozen=True)
class SubstitutionReason:
    """Why one predicate is satisfied — **a rendering, not a judgement**.

    Every field here is read back off the rows the executor already filtered on:
    `substitution_direction` from `parameter_template`, the offered value from the
    candidate's own `parameter_value`, the required value from the requirement's
    filter. So this sentence restates a predicate SQL enforced, which is exactly
    what makes a suggestion trustworthy rather than magical — and why it can be
    shown to a user without being hedged.
    """

    template: str
    display_name: str
    direction: SubstitutionDirection
    #: What was asked for, verbatim from the requirement's filter.
    required: str
    #: What the part has: `parameter_value.raw_input` for a numeric (lossless, and
    #: already in engineering notation as somebody typed it), the choice key for
    #: an enum.
    offered: str
    explanation: str


@dataclass(frozen=True)
class Candidate:
    """One part the executor returned, with the numbers the ordering used."""

    part: Part
    kind: MatchKind
    #: Total across every lot, from `stock_lots.qty_milli_cached`. **Never a
    #: `SUM(stock_ledger.delta_milli)`** — that is the query that stops being
    #: sub-second around 200k rows.
    qty_milli: int
    qty_reserved_milli: int
    lot_count: int
    location_count: int
    #: Log-ratio distance from the requested value, summed over numeric filters.
    #: 0.0 means the part's interval overlaps everything that was asked for.
    distance: float
    reasons: tuple[SubstitutionReason, ...] = ()
    #: Whether free stock covers what the line needs, or None when nothing said
    #: how much is wanted — `Requirement.quantity` is None for unspecified, and
    #: inventing a 1 here would manufacture the same BOM figure the parser
    #: refuses to.
    covers_required: bool | None = None
    #: 1-based position within its own list.
    rank: int = 0

    @property
    def free_milli(self) -> int:
        """What could actually be drawn on: stock less what is already promised."""
        return max(0, self.qty_milli - self.qty_reserved_milli)

    @property
    def is_in_stock(self) -> bool:
        """The same test `SearchQuery.in_stock_only` compiles to."""
        return self.qty_milli > 0

    @property
    def is_substitute(self) -> bool:
        return self.kind is MatchKind.SUBSTITUTE


@dataclass(frozen=True)
class Suggestion:
    """One line's answer: what it was read as, and what could satisfy it."""

    requirement: Requirement
    outcome: Outcome
    #: Ranked. Every one of these is a part you own.
    in_stock: tuple[Candidate, ...] = ()
    #: Ranked, and **clearly a different thing**: parts that satisfy the
    #: requirement and that you do not have. Populated whether or not `in_stock`
    #: is, because a well-stocked line still benefits from knowing what else
    #: qualifies, and an empty list here on top of an empty `in_stock` is what
    #: distinguishes `NO_MATCH` from `ORDER`.
    not_stocked: tuple[Candidate, ...] = ()
    #: True when either executor pass filled the fetch cap, so the lists are a
    #: shortlist of a larger matching set. `/api/search/parts` is where a true
    #: total lives; inventing one here would cost a second COUNT per line.
    truncated: bool = False
    #: What the line needs, in milli-units — from the caller, or from the text.
    required_milli: int | None = None
    #: Set when this line came from a `bom_lines` row, so accepting a candidate
    #: is an ordinary edit of that line through the existing route.
    bom_line_id: int | None = None

    @property
    def message(self) -> str:
        """The sentence a UI shows. Written per outcome, and never rounded up."""
        if self.outcome is Outcome.NOT_ACTIONABLE:
            unread = ", ".join(self.requirement.residue)
            return (
                "nothing in this description could be turned into a search"
                + (f" ({unread})" if unread else "")
                + "; it stays as a line with its text intact"
            )
        if self.outcome is Outcome.NO_MATCH:
            return (
                "no part in the catalogue satisfies this, as a match or as a "
                "substitute — so this is not a stock problem, it is a part that "
                "has to be added"
            )
        if self.outcome is Outcome.ORDER:
            return (
                f"you own nothing that satisfies this; {len(self.not_stocked)} "
                "part(s) in the catalogue do, so this line turns into an order"
            )
        substitutes = sum(1 for candidate in self.in_stock if candidate.is_substitute)
        text = f"{len(self.in_stock)} part(s) you own satisfy this"
        if substitutes:
            return f"{text}, {substitutes} of them as a substitute rather than an exact match"
        return text


@dataclass(frozen=True)
class RequirementInput:
    """One line to answer."""

    text: str
    #: How much is wanted, in milli-units. Overrides whatever the text said; None
    #: means "take it from the text", and if the text did not say either then
    #: nothing is assumed.
    required_milli: int | None = None
    bom_line_id: int | None = None


# ---------------------------------------------------------------------------
# Stock, once per batch
# ---------------------------------------------------------------------------


class _StockIndex:
    """Quantity, reservations, lot and container counts, cached across a batch.

    Deliberately its own thing rather than `app.api.routes.search._stock_by_part`:
    a service must not import a route module, and this one differs where it
    matters — it remembers what it has already asked about, so twenty BOM lines
    that all suggest the same 0603 resistor cost one aggregate rather than twenty,
    which is most of the point of batching at all.

    Reads `stock_lots.qty_milli_cached` and `qty_reserved_milli_cached`, on the
    same `qty > 0` predicate `in_stock_only` and the ordering use, so a row can
    never read "0 lots" while sorting as though it were stocked.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._rows: dict[int, tuple[int, int, int, int]] = {}

    def ensure(self, part_ids: Sequence[int]) -> None:
        missing = sorted({part_id for part_id in part_ids if part_id not in self._rows})
        for start in range(0, len(missing), _STOCK_CHUNK):
            chunk = missing[start : start + _STOCK_CHUNK]
            rows = self._session.execute(
                select(
                    StockLot.part_id,
                    func.coalesce(func.sum(StockLot.qty_milli_cached), 0),
                    func.coalesce(func.sum(StockLot.qty_reserved_milli_cached), 0),
                    func.count(StockLot.id),
                    func.count(func.distinct(StockLot.location_id)),
                )
                .where(StockLot.part_id.in_(chunk), StockLot.qty_milli_cached > 0)
                .group_by(StockLot.part_id)
            ).all()
            found = {
                int(row[0]): (int(row[1]), int(row[2]), int(row[3]), int(row[4])) for row in rows
            }
            for part_id in chunk:
                self._rows[part_id] = found.get(part_id, (0, 0, 0, 0))

    def of(self, part_id: int) -> tuple[int, int, int, int]:
        return self._rows.get(part_id, (0, 0, 0, 0))


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------


def parse_batch(session: Session, texts: Sequence[str]) -> tuple[Requirement, ...]:
    """Read every line against one vocabulary snapshot.

    One `load_vocabulary` and one parser for the whole batch: the phrase index and
    the trial-parse sweep are both per-vocabulary, so a page of BOM descriptions
    shares them. That, and one HTTP round trip, is what "batch it" buys.
    """
    parser = DeterministicRequirementParser(load_vocabulary(session))
    return tuple(parser.parse(text) for text in texts)


def suggest_batch(
    session: Session,
    inputs: Sequence[RequirementInput],
    *,
    limit: int = DEFAULT_LIMIT,
    include_stubs: bool = True,
) -> tuple[Suggestion, ...]:
    """Answer a batch of lines. One vocabulary, one template map, one stock cache."""
    requirements = parse_batch(session, [item.text for item in inputs])
    templates = _templates_by_name(session)
    stock = _StockIndex(session)
    return tuple(
        _suggest_one(
            session,
            requirement,
            item,
            templates=templates,
            stock=stock,
            limit=limit,
            include_stubs=include_stubs,
        )
        for requirement, item in zip(requirements, inputs, strict=True)
    )


def _templates_by_name(session: Session) -> dict[str, ParameterTemplate]:
    return {row.name: row for row in session.execute(select(ParameterTemplate)).scalars()}


def _suggest_one(
    session: Session,
    requirement: Requirement,
    item: RequirementInput,
    *,
    templates: dict[str, ParameterTemplate],
    stock: _StockIndex,
    limit: int,
    include_stubs: bool,
) -> Suggestion:
    required_milli = item.required_milli
    if required_milli is None and requirement.quantity is not None:
        required_milli = requirement.quantity * 1000

    if not _is_searchable(requirement):
        return Suggestion(
            requirement=requirement,
            outcome=Outcome.NOT_ACTIONABLE,
            required_milli=required_milli,
            bom_line_id=item.bom_line_id,
        )

    fetch = min(_MAX_FETCH, max(limit * _FETCH_MULTIPLE, limit))
    matched = _matched(
        session, requirement, mode="search", fetch=fetch, include_stubs=include_stubs
    )
    satisfying = _matched(
        session, requirement, mode="substitute", fetch=fetch, include_stubs=include_stubs
    )
    truncated = len(matched) >= fetch or len(satisfying) >= fetch

    # The union, with `exact` winning a part both passes returned: it is the
    # stronger claim, and offering the same part twice under two labels would
    # make a shortlist of five hold three parts.
    tiers: list[tuple[Part, MatchKind, int]] = [
        (part, MatchKind.EXACT, position) for position, part in enumerate(matched)
    ]
    seen = {part.id for part in matched}
    tiers.extend(
        (part, MatchKind.SUBSTITUTE, len(matched) + position)
        for position, part in enumerate(satisfying)
        if part.id not in seen
    )

    stock.ensure([part.id for part, _kind, _position in tiers])
    reasons = _reasons_by_part(
        session,
        requirement,
        [part for part, kind, _position in tiers if kind is MatchKind.SUBSTITUTE],
        templates=templates,
    )
    distances = _distances_by_part(
        session,
        requirement,
        [part for part, _kind, _position in tiers],
        templates=templates,
    )

    candidates: list[tuple[Candidate, int]] = []
    for part, kind, position in tiers:
        qty_milli, reserved_milli, lot_count, location_count = stock.of(part.id)
        free_milli = max(0, qty_milli - reserved_milli)
        candidates.append(
            (
                Candidate(
                    part=part,
                    kind=kind,
                    qty_milli=qty_milli,
                    qty_reserved_milli=reserved_milli,
                    lot_count=lot_count,
                    location_count=location_count,
                    distance=distances.get(part.id, 0.0),
                    reasons=reasons.get(part.id, ()),
                    covers_required=(
                        None if required_milli is None else free_milli >= required_milli
                    ),
                ),
                position,
            )
        )

    ranked = _rank(candidates)
    in_stock = _numbered([one for one in ranked if one.is_in_stock][:limit])
    not_stocked = _numbered([one for one in ranked if not one.is_in_stock][:limit])

    if in_stock:
        outcome = Outcome.STOCKED
    elif not_stocked:
        outcome = Outcome.ORDER
    else:
        outcome = Outcome.NO_MATCH

    return Suggestion(
        requirement=requirement,
        outcome=outcome,
        in_stock=in_stock,
        not_stocked=not_stocked,
        truncated=truncated,
        required_milli=required_milli,
        bom_line_id=item.bom_line_id,
    )


def _is_searchable(requirement: Requirement) -> bool:
    """Whether `_matched` would build a `SearchQuery` with any predicate in it.

    **The reason this is a local predicate and not `Requirement.is_actionable`**,
    which today happens to be the same three fields: they answer different
    questions. `is_actionable` is the parser's "there is something a later stage
    could do with this", and a field could be added to `Requirement` that satisfies
    it without producing a predicate — at which point the executor would receive a
    query with no WHERE clause and answer it with the entire catalogue. Naming the
    condition here, against the three things `_matched` actually puts on the query,
    is what makes that impossible rather than merely unlikely.

    `mpn` rather than `mpn_norm` because the part number reaches the executor as
    `SearchQuery.text`, and FTS indexes the verbatim spelling.
    """
    return bool(requirement.filters or requirement.category or requirement.mpn)


def _matched(
    session: Session,
    requirement: Requirement,
    *,
    mode: Mode,
    fetch: int,
    include_stubs: bool,
) -> list[Part]:
    """One executor pass. **The only place membership is decided.**

    `in_stock_only` is left False on purpose: the availability split downstream is
    the same `qty_milli_cached > 0` test the flag compiles to, so one pass answers
    both halves of the question instead of two passes answering one each. The
    executor's own ordering is stock-first, so the rows lost to `fetch` are the
    ones nobody owns.
    """
    return query_builder.execute(
        session,
        SearchQuery(
            filters=requirement.to_filters(),
            text=requirement.mpn,
            category_slug=requirement.category_slug,
            include_stubs=include_stubs,
            mode=mode,
            limit=fetch,
        ),
    )


def _rank(candidates: Sequence[tuple[Candidate, int]]) -> tuple[Candidate, ...]:
    """Order the executor's result. **One `sorted()`, so it is a permutation.**

    This is where "ranking may never promote a part the filter excluded" is
    enforced, and it is enforced by shape rather than by a check: the input is the
    list `_matched` returned and the output is that same list reordered. There is
    no branch here that could append a part, and none that could drop one — which
    is why the ranking is a sort key and not a scoring loop that builds its own
    result.

    See the module docstring for what each term is and why, including why
    availability is **not** one of them: it is the partition applied to this
    output, which is stronger than a first sort term and does not duplicate it.
    `position` is the index in the executor's own output, which makes the key
    total.
    """
    return tuple(
        candidate
        for candidate, _position in sorted(
            candidates,
            key=lambda pair: (
                0 if pair[0].kind is MatchKind.EXACT else 1,
                pair[0].distance,
                -pair[0].free_milli,
                1 if pair[0].part.is_stub else 0,
                pair[1],
            ),
        )
    )


def _numbered(candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    return tuple(
        replace(candidate, rank=position) for position, candidate in enumerate(candidates, start=1)
    )


# ---------------------------------------------------------------------------
# Reading the candidates' own values back
# ---------------------------------------------------------------------------


def _values_by_part(
    session: Session, part_ids: Sequence[int], template_ids: Sequence[int]
) -> dict[tuple[int, int], ParameterValue]:
    """The `parameter_value` rows behind the filters, for the parts on the page.

    One query for the whole page rather than one per candidate, and keyed by
    `(part_id, template_id)` — which is unique by `uq_parameter_value_part_
    template`, the constraint the whole search design rests on.
    """
    if not part_ids or not template_ids:
        return {}
    rows = session.execute(
        select(ParameterValue).where(
            ParameterValue.part_id.in_(sorted(set(part_ids))),
            ParameterValue.template_id.in_(sorted(set(template_ids))),
        )
    ).scalars()
    return {(row.part_id, row.template_id): row for row in rows}


def _distances_by_part(
    session: Session,
    requirement: Requirement,
    parts: Sequence[Part],
    *,
    templates: dict[str, ParameterTemplate],
) -> dict[int, float]:
    """Ranking term 3, per candidate. Numeric filters only; enums have no distance."""
    numeric = [
        (item, templates[item.template])
        for item in requirement.filters
        if item.template in templates and templates[item.template].value_type == ValueType.NUMERIC
    ]
    if not numeric:
        return {}

    wanted: dict[str, tuple[float | None, float | None]] = {}
    for item, template in numeric:
        interval = _requested_interval(item, template)
        if interval is not None:
            wanted[item.template] = interval

    values = _values_by_part(
        session, [part.id for part in parts], [template.id for _item, template in numeric]
    )
    distances: dict[int, float] = {}
    for part in parts:
        total = 0.0
        for item, template in numeric:
            if item.template not in wanted:
                continue
            row = values.get((part.id, template.id))
            if row is None:
                # Unreachable while the filter is doing its job — it JOINs this
                # very row — so this is a narrowing, not a fallback with an
                # opinion. A missing row contributes no distance rather than a
                # made-up one.
                continue
            total += _distance(wanted[item.template], (row.value_min, row.value_max))
        distances[part.id] = total
    return distances


def _requested_interval(
    item: RequirementFilter, template: ParameterTemplate
) -> tuple[float | None, float | None] | None:
    """What the requirement asked for, as bounds — through the one value grammar.

    `RequirementFilter.value` is deliberately the value *text*, so this is the
    same `parse_for_template` the executor calls on the same string. Nothing here
    computes an interval of its own; a second implementation of `20-30uF` is how
    the ranking would start disagreeing with the filter about what was asked.
    """
    try:
        return parse_for_template(item.value, template).to_interval()
    except ValueParseError:
        # The parser already validated this string against this template, so a
        # refusal here means the two disagree — worth not crashing a whole batch
        # over, and worth contributing no ordering.
        return None


def _distance(
    required: tuple[float | None, float | None], offered: tuple[float | None, float | None]
) -> float:
    """Log-ratio distance between two intervals; 0.0 when they overlap.

    Zero for an overlap because a part inside the requested band *is* what was
    asked for, and there is nothing to prefer between two of them on value.
    Otherwise the ratio of the interval midpoints in decades, which is the metric
    component values live in — and which makes the least over-specified substitute
    the closest one.
    """
    required_low, required_high = required
    offered_low, offered_high = offered

    above = required_high is None or offered_low is None or offered_low <= required_high
    below = required_low is None or offered_high is None or offered_high >= required_low
    if above and below:
        return 0.0

    required_mid = _midpoint(required_low, required_high)
    offered_mid = _midpoint(offered_low, offered_high)
    if required_mid is None or offered_mid is None or required_mid <= 0 or offered_mid <= 0:
        return 0.0
    return abs(math.log10(offered_mid / required_mid))


def _midpoint(low: float | None, high: float | None) -> float | None:
    """The geometric mean of a band, because these values are logarithmic.

    A one-sided bound (`>=50V`) is its own midpoint: there is no other number
    available, and an arithmetic mean against an invented ceiling would be worse
    than using the bound that was actually stated.
    """
    if low is not None and high is not None:
        if low <= 0 or high <= 0:
            return (low + high) / 2.0
        return math.sqrt(low * high)
    return low if low is not None else high


# ---------------------------------------------------------------------------
# Why a substitute qualifies
# ---------------------------------------------------------------------------

_DIRECTION_TEXT: dict[SubstitutionDirection, str] = {
    SubstitutionDirection.HIGHER_OK: (
        "{display} {offered} is at or above the {required} asked for, and a higher "
        "rating satisfies a lower requirement"
    ),
    SubstitutionDirection.LOWER_OK: (
        "{display} {offered} is at or below the {required} asked for, and a lower "
        "value satisfies the requirement"
    ),
    # **Overlap, not containment.** `query_builder._substitution_predicate` for
    # this direction is `value_min <= high AND value_max >= low`, which is
    # satisfied by a part whose band merely *reaches into* the requested one — a
    # 20-100 µF part qualifies against a 20-30 µF requirement. "Falls inside" was
    # a stronger claim than the SQL made, and it is the sentence a user reads
    # immediately before pressing "Use this", so it has to restate the predicate
    # and nothing more. See `SubstitutionReason`.
    SubstitutionDirection.RANGE_OVERLAP: ("{display} {offered} overlaps the {required} asked for"),
    SubstitutionDirection.EXACT: "{display} {offered} is exactly the {required} asked for",
}


def _reasons_by_part(
    session: Session,
    requirement: Requirement,
    parts: Sequence[Part],
    *,
    templates: dict[str, ParameterTemplate],
) -> dict[int, tuple[SubstitutionReason, ...]]:
    """One sentence per filter, for each substitute. Read off the filtered rows.

    Produced for **every** filter of a substitute, not only the ones whose
    direction did work: collectively they are the justification, and "package
    0603 exactly as asked, voltage rating 100 V at or above the 50 V asked for" is
    the answer to "why is this being offered" in a way that either half alone is
    not.

    Not produced for an exact match, which needs no explanation — it is what was
    asked for, and a sentence saying so on every row is noise that trains the user
    to stop reading the ones that matter.
    """
    if not parts:
        return {}

    used = [
        (item, templates[item.template])
        for item in requirement.filters
        if item.template in templates
    ]
    if not used:
        return {}

    values = _values_by_part(
        session, [part.id for part in parts], [template.id for _item, template in used]
    )
    choices = _choices_by_id(session, [template.id for _item, template in used])

    reasons: dict[int, tuple[SubstitutionReason, ...]] = {}
    for part in parts:
        rendered: list[SubstitutionReason] = []
        for item, template in used:
            row = values.get((part.id, template.id))
            if row is None:  # see `_distances_by_part` — the filter JOINed this row
                continue
            offered = _offered_text(row, choices)
            if offered is None:
                continue
            direction = SubstitutionDirection(template.substitution_direction)
            required = item.value
            if direction is SubstitutionDirection.EXACT and "," in required:
                explanation = (
                    f"{template.display_name} {offered} is one of the {required} asked for"
                )
            else:
                explanation = _DIRECTION_TEXT[direction].format(
                    display=template.display_name, offered=offered, required=required
                )
            rendered.append(
                SubstitutionReason(
                    template=template.name,
                    display_name=template.display_name,
                    direction=direction,
                    required=required,
                    offered=offered,
                    explanation=explanation,
                )
            )
        reasons[part.id] = tuple(rendered)
    return reasons


def _choices_by_id(session: Session, template_ids: Sequence[int]) -> dict[int, str]:
    if not template_ids:
        return {}
    rows = session.execute(
        select(ParameterChoice).where(ParameterChoice.template_id.in_(sorted(set(template_ids))))
    ).scalars()
    return {row.id: row.key for row in rows}


def _offered_text(row: ParameterValue, choices: dict[int, str]) -> str | None:
    """What the part has, in the spelling a user would recognise.

    `raw_input` for a numeric rather than a re-rendered float: it is kept verbatim
    for exactly this, so `4700 Ω` shows as the `4k7` somebody typed. The choice
    *key* for an enum, because that is the spelling `query_builder.Filter` carries
    and the one the requirement's own filter is written in — showing a label here
    while the requirement shows a key would read as a mismatch.
    """
    if row.choice_id is not None:
        return choices.get(row.choice_id)
    if row.raw_input:
        return row.raw_input
    return row.value_text
