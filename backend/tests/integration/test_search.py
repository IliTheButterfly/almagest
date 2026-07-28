"""Parametric search.

The centrepiece is `test_the_worked_example_from_the_design_doc`: seed a THT
22 µF ceramic, an SMD 22 µF ceramic and a THT 22 µF electrolytic, then confirm
"through-hole 20–30 µF ceramic" returns **exactly one**. Three parts that agree
on the filtered numeric and differ by one facet each is the smallest fixture
that can catch a predicate being dropped, inverted, or fanned out.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.parameter import ParameterTemplate
from app.scripts.seed_demo import seed_catalogue
from app.services.search.query_builder import (
    Filter,
    FilterError,
    SearchQuery,
    UnknownTemplate,
    count,
    execute,
)
from tests.factories import make_location, make_lot, make_part


@pytest.fixture
def catalogue(db: Session) -> Session:
    seed_catalogue(db)
    db.commit()
    return db


def template(db: Session, name: str) -> ParameterTemplate:
    return db.execute(select(ParameterTemplate).where(ParameterTemplate.name == name)).scalar_one()


def names(parts: list[Part]) -> set[str]:
    return {part.mpn or part.name for part in parts}


# ---------------------------------------------------------------------------
# The worked example
# ---------------------------------------------------------------------------


def test_the_worked_example_from_the_design_doc(catalogue: Session) -> None:
    """ "Through-hole 20-30 µF ceramic capacitor" must return exactly one."""
    found = execute(
        catalogue,
        SearchQuery(
            category_slug="capacitor",
            filters=(
                Filter("mounting_type", "THT"),
                Filter("capacitance", "20-30uF"),
                Filter("capacitor_technology", "ceramic"),
            ),
        ),
    )
    assert names(found) == {"DEMO-CAP-THT-22U"}


def test_dropping_the_technology_facet_returns_both_through_hole_parts(
    catalogue: Session,
) -> None:
    """Guards the previous test against passing for the wrong reason — if the
    ceramic predicate were silently ignored, this would return the same set."""
    found = execute(
        catalogue,
        SearchQuery(
            category_slug="capacitor",
            filters=(Filter("mounting_type", "THT"), Filter("capacitance", "20-30uF")),
        ),
    )
    assert names(found) == {"DEMO-CAP-THT-22U", "DEMO-CAP-THT-22U-ELEC"}


def test_multiple_predicates_never_fan_out(catalogue: Session) -> None:
    """`UNIQUE(part_id, template_id)` is what guarantees this. Without it each
    JOIN could multiply rows and a part would appear once per matching value."""
    found = execute(
        catalogue,
        SearchQuery(
            filters=(
                Filter("capacitance", "20-30uF"),
                Filter("mounting_type", "THT"),
                Filter("capacitor_technology", "ceramic"),
                Filter("voltage_rating", "1-100V"),
            )
        ),
    )
    assert len(found) == len({part.id for part in found}) == 1


# ---------------------------------------------------------------------------
# Numeric matching
# ---------------------------------------------------------------------------


def test_a_scalar_part_matches_a_range_query(catalogue: Session) -> None:
    """The reason every numeric row carries min/max even when it is a scalar."""
    assert names(execute(catalogue, SearchQuery(filters=(Filter("capacitance", "20-30uF"),))))


def test_a_range_that_excludes_the_value_finds_nothing(catalogue: Session) -> None:
    found = execute(catalogue, SearchQuery(filters=(Filter("capacitance", "100-200uF"),)))
    assert found == []


def test_shorthand_works_in_a_query(catalogue: Session) -> None:
    assert names(execute(catalogue, SearchQuery(filters=(Filter("resistance", "4k7"),)))) == {
        "DEMO-RES-4K7"
    }


def test_a_comparison_query(catalogue: Session) -> None:
    found = execute(catalogue, SearchQuery(filters=(Filter("voltage_rating", ">=50V"),)))
    assert names(found) == {"DEMO-CAP-THT-22U-ELEC"}


def test_tolerance_band_is_matched_not_just_the_nominal(catalogue: Session) -> None:
    """The 10k ±1% part spans 9900-10100, so a query at 10050 must find it even
    though no stored nominal equals that."""
    found = execute(catalogue, SearchQuery(filters=(Filter("resistance", "10050"),)))
    assert names(found) == {"DEMO-RES-10K"}


def test_the_query_value_is_parsed_with_the_templates_quantity(catalogue: Session) -> None:
    """A query is validated exactly as a stored value is — `1M` is a fine
    resistance query and an impossible capacitance one."""
    execute(catalogue, SearchQuery(filters=(Filter("resistance", "1M"),)))

    with pytest.raises(FilterError) as excinfo:
        execute(catalogue, SearchQuery(filters=(Filter("capacitance", "1M"),)))
    assert excinfo.value.reason == "implausible"


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------


def test_dual_notation_in_a_query(catalogue: Session) -> None:
    """Searching `1608` must find a part filed as `0603`."""
    imperial = execute(catalogue, SearchQuery(filters=(Filter("package", "0603"),)))
    metric = execute(catalogue, SearchQuery(filters=(Filter("package", "1608"),)))
    assert names(imperial) == names(metric) == {"DEMO-RES-10K"}


def test_comma_separated_choices_are_ored(catalogue: Session) -> None:
    """One facet with two acceptable answers, not two contradictory filters."""
    found = execute(
        catalogue,
        SearchQuery(filters=(Filter("capacitor_technology", "ceramic,electrolytic"),)),
    )
    assert len(found) == 3


def test_an_unknown_choice_is_reported_rather_than_ignored(catalogue: Session) -> None:
    with pytest.raises(FilterError) as excinfo:
        execute(catalogue, SearchQuery(filters=(Filter("mounting_type", "levitating"),)))
    assert excinfo.value.reason == "unknown_choice"


def test_an_unknown_template_is_an_error(catalogue: Session) -> None:
    with pytest.raises(UnknownTemplate):
        execute(catalogue, SearchQuery(filters=(Filter("shoe_size", "44"),)))


# ---------------------------------------------------------------------------
# Substitution — the same executor, different operators
# ---------------------------------------------------------------------------


def test_substitution_accepts_a_higher_rating(catalogue: Session) -> None:
    """`higher_ok`: a 50 V part satisfies a 25 V requirement."""
    found = execute(
        catalogue,
        SearchQuery(filters=(Filter("voltage_rating", "25V"),), mode="substitute"),
    )
    assert "DEMO-CAP-THT-22U-ELEC" in names(found)  # 50 V
    assert "DEMO-CAP-SMD-22U" not in names(found)  # 16 V


def test_substitution_is_directional(catalogue: Session) -> None:
    """The reverse must not hold — a 16 V part cannot stand in for 50 V. This
    is exactly the failure an LLM-based substitution search would produce, and
    the reason this stays a deterministic SQL predicate."""
    found = execute(
        catalogue,
        SearchQuery(filters=(Filter("voltage_rating", "50V"),), mode="substitute"),
    )
    assert names(found) == {"DEMO-CAP-THT-22U-ELEC"}


def test_search_and_substitute_differ_only_in_the_operator(catalogue: Session) -> None:
    requirement = (Filter("voltage_rating", "25V"),)
    as_search = names(execute(catalogue, SearchQuery(filters=requirement, mode="search")))
    as_substitute = names(execute(catalogue, SearchQuery(filters=requirement, mode="substitute")))
    # An exact-match search finds the 25 V part; substitution finds what would
    # *do instead*, which is a strictly different question.
    assert as_search != as_substitute


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def test_a_category_search_includes_descendants(catalogue: Session) -> None:
    """Searching "Passives" must find resistors — one indexed prefix match on
    id_path, no recursion."""
    passives = execute(catalogue, SearchQuery(category_slug="passive"))
    assert len(passives) == 5
    assert "DEMO-RES-4K7" in names(passives)


def test_an_unknown_category_matches_nothing(catalogue: Session) -> None:
    assert execute(catalogue, SearchQuery(category_slug="does-not-exist")) == []


def test_free_text_matches_identity_fields(catalogue: Session) -> None:
    assert names(execute(catalogue, SearchQuery(text="electrolytic"))) == {"DEMO-CAP-THT-22U-ELEC"}


def test_in_stock_only(catalogue: Session) -> None:
    part = catalogue.execute(select(Part).where(Part.mpn == "DEMO-RES-4K7")).scalar_one()
    make_lot(catalogue, part, make_location(catalogue), qty_milli=5000)
    catalogue.commit()

    found = execute(catalogue, SearchQuery(in_stock_only=True))
    assert names(found) == {"DEMO-RES-4K7"}


def test_a_part_with_several_lots_appears_once(catalogue: Session) -> None:
    """An EXISTS rather than a JOIN, so stock in three bins does not triple the
    part in the results."""
    part = catalogue.execute(select(Part).where(Part.mpn == "DEMO-RES-4K7")).scalar_one()
    for index in range(3):
        make_lot(catalogue, part, make_location(catalogue, name=f"Bin {index}"), qty_milli=10)
    catalogue.commit()

    assert len(execute(catalogue, SearchQuery(in_stock_only=True))) == 1


def test_stubs_can_be_excluded(catalogue: Session) -> None:
    make_part(catalogue, name="unidentified salvage", is_stub=True)
    catalogue.commit()

    assert len(execute(catalogue, SearchQuery(include_stubs=True))) == 6
    assert len(execute(catalogue, SearchQuery(include_stubs=False))) == 5


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_ordering_is_deterministic(catalogue: Session) -> None:
    """Otherwise pagination silently drops and repeats rows between pages."""
    first = [part.id for part in execute(catalogue, SearchQuery())]
    for _ in range(5):
        assert [part.id for part in execute(catalogue, SearchQuery())] == first


def test_pagination(catalogue: Session) -> None:
    everything = [part.id for part in execute(catalogue, SearchQuery(limit=100))]
    page_one = [part.id for part in execute(catalogue, SearchQuery(limit=2, offset=0))]
    page_two = [part.id for part in execute(catalogue, SearchQuery(limit=2, offset=2))]

    assert page_one == everything[:2]
    assert page_two == everything[2:4]
    assert not set(page_one) & set(page_two)


def test_count_ignores_pagination(catalogue: Session) -> None:
    query = SearchQuery(filters=(Filter("capacitance", "20-30uF"),), limit=1)
    assert len(execute(catalogue, query)) == 1
    assert count(catalogue, query) == 3


def test_an_empty_query_returns_everything(catalogue: Session) -> None:
    assert len(execute(catalogue, SearchQuery())) == 5
