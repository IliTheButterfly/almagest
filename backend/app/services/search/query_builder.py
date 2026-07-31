"""The parametric filter executor.

One filter shape, one executor, two modes. `mode="search"` finds parts that
*match* a requirement; `mode="substitute"` finds parts that would *satisfy* it.
The only difference is the operator chosen per predicate, read off
`parameter_template.substitution_direction`.

**There is deliberately no second query engine, and no model anywhere near
this.** `substitution_direction` is correct by construction: a 50 V capacitor
substitutes for a 25 V one and not the reverse, because that is what
`higher_ok` means. An LLM would return *plausible* substitutes, and a plausible
substitute with the wrong voltage rating is a field failure. Embeddings may
suggest candidates to explore; only this decides what actually qualifies.

The whole design rests on `UNIQUE(part_id, template_id)`: each joined filter
contributes **at most one row**, so N predicates are N plain JOINs that cannot
fan out into a cross product.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from elec_value_parser import ValueParseError
from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    column,
    exists,
    false,
    func,
    select,
    text,
    true,
)
from sqlalchemy.orm import Session, aliased

from app.models.catalog import Part, PartCategory
from app.models.enums import SubstitutionDirection, ValueType
from app.models.parameter import (
    ParameterChoice,
    ParameterTemplate,
    ParameterValue,
    ParameterValueChoice,
)
from app.models.stock import StockLot
from app.services.parameters import ChoiceNotFound, resolve_choice
from app.services.search.fts import build_match_query
from app.services.search.value_parser import parse_for_template

Mode = Literal["search", "substitute"]


class UnknownTemplate(ValueError):
    """A filter naming a `parameter_template` that does not exist."""


@dataclass(frozen=True)
class Filter:
    """One predicate. The template decides how `value` is interpreted.

    A single shape for numerics and enums alike is what lets the GET
    querystring alias and the POST body run through the identical code, so a
    pasted URL and an API call can never disagree about what a query means.
    """

    template: str
    value: str


@dataclass(frozen=True)
class SearchQuery:
    filters: tuple[Filter, ...] = ()
    text: str | None = None
    category_slug: str | None = None
    part_kind_slug: str | None = None
    in_stock_only: bool = False
    include_stubs: bool = True
    mode: Mode = "search"
    limit: int = 50
    offset: int = 0


class FilterError(ValueError):
    """A filter that could not be turned into a predicate."""

    def __init__(self, message: str, *, template: str, reason: str) -> None:
        super().__init__(message)
        self.template = template
        self.reason = reason


def execute(session: Session, query: SearchQuery) -> list[Part]:
    statement = build(session, query)
    return list(session.execute(statement).scalars().unique())


def count(session: Session, query: SearchQuery) -> int:
    inner = build(session, query, for_count=True).subquery()
    return int(session.execute(select(func.count()).select_from(inner)).scalar_one())


def build(session: Session, query: SearchQuery, *, for_count: bool = False) -> Select[tuple[Part]]:
    statement: Select[tuple[Part]] = select(Part)

    if not query.include_stubs:
        statement = statement.where(Part.is_stub.is_(False))

    if query.category_slug:
        statement = _restrict_to_category_subtree(session, statement, query.category_slug)

    if query.part_kind_slug:
        from app.models.catalog import PartKind

        statement = statement.join(PartKind, Part.part_kind_id == PartKind.id).where(
            PartKind.slug == query.part_kind_slug
        )

    if query.in_stock_only:
        # EXISTS rather than a JOIN: a part with three lots must not appear
        # three times, and DISTINCT to fix that would defeat the index.
        statement = statement.where(
            select(StockLot.id)
            .where(StockLot.part_id == Part.id, StockLot.qty_milli_cached > 0)
            .exists()
        )

    if query.text:
        statement = _apply_text(statement, query.text)

    for spec in query.filters:
        statement = _apply_filter(session, statement, spec, query.mode)

    if for_count:
        return statement

    return _ordered(statement, query).limit(query.limit).offset(query.offset)


def _ordered(statement: Select[tuple[Part]], query: SearchQuery) -> Select[tuple[Part]]:
    """Apply the ranking, then a total tie-break.

    The tie-break is not decoration. Pagination over a partially-ordered result
    silently drops and repeats rows between pages, because SQLite is free to
    return equal-ranking rows in a different order for each OFFSET.
    """
    match_query = build_match_query(query.text) if query.text else None

    if match_query is not None:
        # bm25() is more negative the better the match, so ascending is
        # best-first. Ranking happens *within* the already-filtered set: the
        # WHERE clauses above have narrowed this to a few hundred candidates
        # before any relevance is computed, which is the cheap ordering.
        return statement.order_by(_bm25_expression(match_query), Part.name, Part.id)

    # No free-text term: order by whether there is stock, then by name. A part
    # you actually have is nearly always the one being looked for.
    in_stock = (
        select(StockLot.id)
        .where(StockLot.part_id == Part.id, StockLot.qty_milli_cached > 0)
        .exists()
    )
    return statement.order_by(in_stock.desc(), Part.name, Part.id)


def _bm25_expression(match_query: str) -> ColumnElement[float]:
    """Relevance for the current row, correlated on `part_fts.rowid`.

    **The MATCH has to be repeated here.** `bm25()` is an auxiliary function of a
    full-text query: it scores the row against the query in its own SELECT, so a
    subquery that filtered on `rowid` alone would be asking for a relevance with
    no query to be relevant to. It does not error — it just returns nothing
    useful, which is why this was caught by a ranking-order test rather than by a
    crash.

    A correlated scalar subquery rather than a join, because `_apply_text` has
    already narrowed the candidate set; joining `part_fts` again would multiply
    rows and force a DISTINCT that discards the ordering.

    Column weights, in declaration order (mpn, description, manufacturer,
    keywords, param_digest): `mpn` dominates, because somebody typing
    "RC0603FR-0710KL" wants that exact part and not every part whose description
    mentions it. `manufacturer` is weighted lowest — otherwise every part from
    one vendor ranks equally for that vendor's name. `param_digest` sits above
    `description` since a value match ("10k") is a stronger signal of intent than
    a prose mention. Note `description` also carries the part's `name`, which the
    migration folds into that bucket.
    """
    return (
        select(func.bm25(text("part_fts"), 10.0, 3.0, 1.0, 2.0, 4.0))
        .select_from(text("part_fts"))
        .where(text("part_fts MATCH :rank_query"), text("part_fts.rowid = parts.id"))
        .params(rank_query=match_query)
        .scalar_subquery()
    )


def _apply_text(statement: Select[tuple[Part]], search_text: str) -> Select[tuple[Part]]:
    """Restrict to parts matching the free-text term, via FTS5.

    This is the *filter* half of "filter first, then rank" — an `IN` against
    `part_fts` rowids, which is a term lookup rather than a scan. The ranking
    lives in `_ordered`, so relevance is only ever computed for rows that already
    survived every other predicate.

    Falls back to nothing-matched rather than everything-matched when the input
    has no searchable token: a user who typed something and got the whole
    catalogue would reasonably conclude search is broken.
    """
    match_query = build_match_query(search_text)
    if match_query is None:
        return statement.where(false())

    matching = (
        select(column("rowid"))
        .select_from(text("part_fts"))
        .where(text("part_fts MATCH :fts_query"))
        .params(fts_query=match_query)
        .scalar_subquery()
    )
    return statement.where(Part.id.in_(matching))


def _restrict_to_category_subtree(
    session: Session, statement: Select[tuple[Part]], slug: str
) -> Select[tuple[Part]]:
    """Include descendants: searching "Passives" must find resistors.

    One indexed prefix match on `id_path`, no recursion — which is the entire
    reason the tree caches a path at all.
    """
    category = session.execute(
        select(PartCategory).where(PartCategory.slug == slug)
    ).scalar_one_or_none()
    if category is None:
        return statement.where(false())

    subtree = (
        select(PartCategory.id)
        .where(PartCategory.id_path.like(f"{category.id_path}%"))
        .scalar_subquery()
    )
    return statement.where(Part.category_id.in_(subtree))


def _apply_filter(
    session: Session, statement: Select[tuple[Part]], spec: Filter, mode: Mode
) -> Select[tuple[Part]]:
    template = session.execute(
        select(ParameterTemplate).where(ParameterTemplate.name == spec.template)
    ).scalar_one_or_none()
    if template is None:
        raise UnknownTemplate(f"no parameter template named {spec.template!r}")

    # One alias per filter. With UNIQUE(part_id, template_id) each contributes
    # at most one row, so these compose as plain JOINs without fan-out.
    pv = aliased(ParameterValue)
    joined = statement.join(pv, and_(pv.part_id == Part.id, pv.template_id == template.id))

    if template.value_type == ValueType.NUMERIC:
        return joined.where(_numeric_predicate(pv, template, spec, mode))
    if template.value_type == ValueType.ENUM:
        return joined.where(_choice_predicate(session, pv, template, spec))
    if template.value_type == ValueType.BOOL:
        wanted = spec.value.strip().casefold() in {"1", "true", "yes", "y"}
        return joined.where(pv.value_bool.is_(wanted))
    return joined.where(pv.value_text.ilike(f"%{spec.value.strip()}%"))


def _numeric_predicate(
    pv: type[ParameterValue],
    template: ParameterTemplate,
    spec: Filter,
    mode: Mode,
) -> ColumnElement[bool]:
    try:
        parsed = parse_for_template(spec.value, template)
    except ValueParseError as error:
        raise FilterError(str(error), template=template.name, reason=error.reason) from error

    low, high = parsed.to_interval()

    if mode == "search":
        # Interval overlap, in both directions. This is why every numeric row
        # must carry min/max even when it is a scalar: a 22 uF part stored with
        # null bounds would never match a 20-30 uF query.
        conditions = []
        if high is not None:
            conditions.append(pv.value_min <= high)
        if low is not None:
            conditions.append(pv.value_max >= low)
        return and_(*conditions) if conditions else true()

    return _substitution_predicate(pv, template, low, high)


def _substitution_predicate(
    pv: type[ParameterValue],
    template: ParameterTemplate,
    low: float | None,
    high: float | None,
) -> ColumnElement[bool]:
    """What *satisfies* the requirement, per the template's declared direction."""
    direction = template.substitution_direction

    if direction == SubstitutionDirection.HIGHER_OK:
        # A rating at least as high as required. Compare against the
        # candidate's lower bound, so a tolerance band that dips below the
        # requirement disqualifies it rather than passing on its midpoint.
        return pv.value_min >= low if low is not None else true()

    if direction == SubstitutionDirection.LOWER_OK:
        return pv.value_max <= high if high is not None else true()

    if direction == SubstitutionDirection.RANGE_OVERLAP:
        conditions = []
        if high is not None:
            conditions.append(pv.value_min <= high)
        if low is not None:
            conditions.append(pv.value_max >= low)
        return and_(*conditions) if conditions else true()

    # EXACT: the candidate's whole interval must sit inside the requirement.
    conditions = []
    if low is not None:
        conditions.append(pv.value_min >= low)
    if high is not None:
        conditions.append(pv.value_max <= high)
    return and_(*conditions) if conditions else true()


def _choice_predicate(
    session: Session,
    pv: type[ParameterValue],
    template: ParameterTemplate,
    spec: Filter,
) -> ColumnElement[bool]:
    """Match any of the named choices.

    Comma-separated values are OR-ed, so "ceramic,film" is one facet with two
    acceptable answers rather than two contradictory filters.
    """
    wanted: list[int] = []
    for token in spec.value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            wanted.append(resolve_choice(session, template, token).id)
        except ChoiceNotFound as error:
            raise FilterError(
                str(error), template=template.name, reason="unknown_choice"
            ) from error

    if not wanted:
        raise FilterError(
            f"no choices given for {template.name}",
            template=template.name,
            reason="empty_choice",
        )
    # `EXISTS` over the child table, not `pv.choice_id IN (...)`: a multi-valued
    # field keeps its options only in `parameter_value_choice` and leaves
    # `choice_id` null, so the old predicate would silently match none of them.
    #
    # A semi-join is also the reason multiplicity was allowed to live in a child
    # table at all — it contributes no rows, so `UNIQUE(part_id, template_id)`
    # keeps doing its job and a five-predicate query still cannot fan out. Joining
    # the child table instead would multiply this value row by the options it
    # holds, which is the cross product that invariant exists to prevent.
    predicate: ColumnElement[bool] = exists(
        select(ParameterValueChoice.value_id)
        .where(ParameterValueChoice.value_id == pv.id)
        .where(ParameterValueChoice.choice_id.in_(wanted))
        .correlate(pv)
    )
    return predicate


def available_facets(session: Session, template_name: str) -> list[ParameterChoice]:
    """The choices of an enum template, for building a filter UI."""
    return list(
        session.execute(
            select(ParameterChoice)
            .join(ParameterTemplate)
            .where(ParameterTemplate.name == template_name)
            .order_by(ParameterChoice.sort_order, ParameterChoice.key)
        ).scalars()
    )


class SearchMode(StrEnum):
    SEARCH = "search"
    SUBSTITUTE = "substitute"
