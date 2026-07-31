"""Filterable attributes and their facets — what a parametric filter UI is built from.

Until now nothing enumerated `parameter_template`, so a client had no way to
discover what it could filter on: the search endpoint accepts a template *name*
but never told anyone which names exist. The frontend was reduced to a free-text
field with a hardcoded hint list, which goes stale the moment a template is added.

**Counts are the point, not a nicety.** A facet list without them ("Ceramic,
Electrolytic, Tantalum") makes every option look equally promising, so the user
discovers an empty result set by clicking into it. DigiKey-style parametric
search works because the counts tell you where the parts actually are *before*
you narrow — and for a personal inventory that matters more, not less, because
most facet values legitimately have zero.

The counts are computed against the same filter set the user has already applied,
so they answer "what can I narrow to *from here*", not "what exists in the
catalogue".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.api.schemas import PartQueryRequest
from app.db.session import get_db
from app.models.catalog import Part, PartCategory
from app.models.enums import ValueType
from app.models.parameter import ParameterChoice, ParameterValue, ParameterValueChoice
from app.services import parameter_fields as fields
from app.services.search import query_builder

router = APIRouter(prefix="/api/parameter-templates", tags=["facets"])


class ChoiceFacet(BaseModel):
    key: str
    label: str
    #: How many parts in the *currently filtered* set carry this choice. Zero is
    #: a legitimate and useful answer — it tells the user not to click.
    count: int


class NumericRange(BaseModel):
    """Bounds across the filtered set, for a slider or a pair of inputs.

    Taken from `value_min`/`value_max` rather than `value_nominal`, because a
    range-valued part ("20-30 µF") has no nominal and would otherwise be
    invisible to the bounds — the same reason search matches on intervals.
    """

    min: float
    max: float
    unit_symbol: str | None = None


class TemplateFacets(BaseModel):
    name: str
    display_name: str
    value_type: str
    base_unit: str | None
    #: `higher_ok`, `lower_ok`, `range_overlap` or `exact`. Exposed because it is
    #: what a "find me a substitute" toggle means, and the UI should be able to
    #: explain *why* a 50 V part satisfies a 25 V requirement.
    substitution_direction: str
    sort_order: int
    #: Parts in the filtered set that have any value for this template. A
    #: template nothing uses is worth de-emphasising rather than hiding, since
    #: it may simply not be filled in yet.
    populated_count: int
    choices: list[ChoiceFacet] = Field(default_factory=list)
    numeric_range: NumericRange | None = None


class FacetsResponse(BaseModel):
    #: Total parts matching the filters the facets were computed against, so the
    #: UI can show "142 parts" beside the filter panel.
    total: int
    templates: list[TemplateFacets]


class FacetsRequest(PartQueryRequest):
    """The filters already applied, so counts describe what narrowing is left.

    Inherits every narrowing field rather than listing its own. The version that
    listed its own omitted `mode` and `part_kind`, so in substitute mode the
    counts silently described the search-mode set — see `PartQueryRequest`.
    """


def _matching_part_ids(db: Session, request: FacetsRequest) -> Select[tuple[int]]:
    """The filtered part set, as a subquery rather than a materialised list.

    Kept as SQL so the three aggregations below stay single round trips instead
    of shipping every matching id back and forth.
    """
    inner = query_builder.build(db, request.to_query(limit=1), for_count=True).subquery()
    return select(inner.c.id)


@router.post("", response_model=FacetsResponse)
def parameter_facets(request: FacetsRequest, db: Session = Depends(get_db)) -> FacetsResponse:
    """Every filterable attribute, with counts against the current filter set."""
    if request.category is not None:
        exists = db.execute(
            select(PartCategory.id).where(PartCategory.slug == request.category)
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no category with slug {request.category!r}"
            )

    try:
        part_ids = _matching_part_ids(db, request)
        total = int(db.execute(select(func.count()).select_from(part_ids.subquery())).scalar_one())
    except query_builder.UnknownTemplate as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except query_builder.FilterError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"template": error.template, "reason": error.reason, "message": str(error)},
        ) from error

    # Category *and its ancestors*, plus every template that names no category at
    # all. Both halves matter and only one of them used to be here: the exact
    # `applies_to_category == request.category` test hid a field authored on
    # "Capacitors" from "Capacitors > Ceramic", which is the node parts are
    # actually filed under — so a user's own new field silently failed to appear.
    # The "no category means everywhere" half is deliberate and kept: `package`
    # and `mounting_type` are things every part has.
    templates = fields.templates_for_category(db, request.category)

    populated: dict[int, int] = {
        row[0]: row[1]
        for row in db.execute(
            select(ParameterValue.template_id, func.count())
            .where(ParameterValue.part_id.in_(part_ids))
            .group_by(ParameterValue.template_id)
        ).all()
    }

    # Counted over `parameter_value_choice`, not `parameter_value.choice_id`: a
    # multi-valued field keeps its options there and leaves `choice_id` null, so
    # counting the column would report every one of its options as zero — a facet
    # that looks like "you own none of these" while the parts are right there.
    # Joining here is safe in a way it is not in the search query: this is an
    # aggregate over values, and one row per (value, option) is exactly what is
    # being counted.
    choice_counts = {
        (row[0], row[1]): row[2]
        for row in db.execute(
            select(ParameterValue.template_id, ParameterValueChoice.choice_id, func.count())
            .join(ParameterValueChoice, ParameterValueChoice.value_id == ParameterValue.id)
            .where(ParameterValue.part_id.in_(part_ids))
            .group_by(ParameterValue.template_id, ParameterValueChoice.choice_id)
        ).all()
    }

    bounds = {
        row[0]: (row[1], row[2])
        for row in db.execute(
            select(
                ParameterValue.template_id,
                func.min(ParameterValue.value_min),
                func.max(ParameterValue.value_max),
            )
            .where(
                ParameterValue.part_id.in_(part_ids),
                ParameterValue.value_min.isnot(None),
            )
            .group_by(ParameterValue.template_id)
        ).all()
    }

    all_choices: dict[int, list[ParameterChoice]] = {}
    for choice in db.execute(
        select(ParameterChoice).order_by(ParameterChoice.sort_order, ParameterChoice.key)
    ).scalars():
        all_choices.setdefault(choice.template_id, []).append(choice)

    out: list[TemplateFacets] = []
    for template in templates:
        facet = TemplateFacets(
            name=template.name,
            display_name=template.display_name,
            value_type=template.value_type,
            base_unit=template.base_unit,
            substitution_direction=template.substitution_direction,
            sort_order=template.sort_order,
            populated_count=populated.get(template.id, 0),
        )

        if template.value_type == ValueType.ENUM:
            facet.choices = [
                ChoiceFacet(
                    key=choice.key,
                    label=choice.label,
                    count=choice_counts.get((template.id, choice.id), 0),
                )
                for choice in all_choices.get(template.id, [])
            ]
        elif template.value_type == ValueType.NUMERIC:
            low, high = bounds.get(template.id, (None, None))
            if low is not None and high is not None:
                facet.numeric_range = NumericRange(
                    min=low, max=high, unit_symbol=_unit_symbol(db, template.id)
                )

        out.append(facet)

    return FacetsResponse(total=total, templates=out)


def _unit_symbol(db: Session, template_id: int) -> str | None:
    """The display symbol any stored value used, so a slider can be labelled.

    Read off a stored value rather than derived from `base_unit`, so it matches
    exactly what the part detail screen shows — 'Ω' rather than 'ohm'.
    """
    return db.execute(
        select(ParameterValue.display_unit_symbol)
        .where(
            ParameterValue.template_id == template_id,
            ParameterValue.display_unit_symbol.isnot(None),
        )
        .limit(1)
    ).scalar_one_or_none()


class CategoryNode(BaseModel):
    #: Reported so the authoring routes are addressable from the rail — they take
    #: an id in the path, and a client that only had slugs could not reach them.
    id: int
    slug: str
    name: str
    parent_slug: str | None
    depth: int
    #: Parts in this category *and its descendants*, which is what the search
    #: endpoint counts too — clicking "Passives" must not report fewer parts
    #: than it then returns.
    part_count: int


categories_router = APIRouter(prefix="/api/part-categories", tags=["facets"])


@categories_router.get("", response_model=list[CategoryNode])
def list_part_categories(db: Session = Depends(get_db)) -> list[CategoryNode]:
    """The category tree, for the browse-by-type rail.

    Counts include descendants, computed with one prefix match per category on
    the cached `id_path` — the same mechanism search uses, so the number beside
    a category always agrees with what selecting it returns.
    """
    categories = list(db.execute(select(PartCategory).order_by(PartCategory.id_path)).scalars())
    by_id = {category.id: category for category in categories}

    counts: dict[int, int] = {}
    for category in categories:
        subtree = (
            select(PartCategory.id)
            .where(PartCategory.id_path.like(f"{category.id_path}%"))
            .scalar_subquery()
        )
        counts[category.id] = int(
            db.execute(
                select(func.count()).select_from(Part).where(Part.category_id.in_(subtree))
            ).scalar_one()
        )

    return [
        CategoryNode(
            id=category.id,
            slug=category.slug,
            name=category.name,
            parent_slug=(
                by_id[category.parent_id].slug
                if category.parent_id and category.parent_id in by_id
                else None
            ),
            depth=category.depth,
            part_count=counts[category.id],
        )
        for category in categories
    ]
