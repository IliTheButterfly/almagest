"""The two things about requirement parsing that need a real database.

Everything else is a unit test (`tests/unit/test_requirements.py`), which is only
legitimate because of the first test here: the vocabulary that suite parses
against is built from the seed script's constants, and this proves
`load_vocabulary` reading a real install produces the same thing. Without it the
unit suite could pass against a snapshot no install has.

The second is the end of the sentence the whole feature is: a description parsed
into filters must **run**, unchanged, through the existing parametric executor. A
`Requirement` whose filters the executor refuses would be a translation into a
vocabulary nobody accepts, and no unit test can catch that.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.scripts.seed_demo import seed_catalogue
from app.services.requirements.parser import DeterministicRequirementParser
from app.services.requirements.vocabulary import load_vocabulary
from app.services.search.query_builder import SearchQuery, execute
from tests.factories import seed_vocabulary


@pytest.fixture
def catalogue(db: Session) -> Session:
    seed_catalogue(db)
    db.commit()
    return db


def test_the_loaded_vocabulary_matches_the_one_the_unit_tests_use(catalogue: Session) -> None:
    """The drift guard that licenses a database-free unit suite.

    Compared by name rather than by tuple order: the order templates come back in
    is cosmetic (it decides the order fields appear in a model's schema), while a
    missing choice or a lost alias is a token that resolves in one place and not
    the other.
    """
    loaded = load_vocabulary(catalogue)
    expected = seed_vocabulary()

    assert {template.name for template in loaded.templates} == {
        template.name for template in expected.templates
    }
    by_name = {template.name: template for template in loaded.templates}
    for template in expected.templates:
        assert by_name[template.name] == template, template.name
    assert set(loaded.categories) == set(expected.categories)
    # And the phrase index, which is what a description is actually matched
    # against, agrees spelling for spelling.
    for phrase in ("0603", "1608", "x7r", "through hole", "surface mount", "soic-8", "mlcc"):
        assert loaded.choices_for(phrase) == expected.choices_for(phrase), phrase
    for phrase in ("resistor", "capacitors", "ic"):
        assert loaded.categories_for(phrase) == expected.categories_for(phrase), phrase


def test_a_description_runs_through_the_executor_unchanged(db: Session) -> None:
    """The worked example, reached from prose instead of from hand-built filters.

    `seed_catalogue` plants three 22 µF capacitors differing by one facet each, and
    `tests/integration/test_search.py` asserts that "through-hole 20-30 µF
    ceramic" finds exactly the first. Here the same query arrives as a sentence,
    and **the executor is untouched** — the requirement parser's whole job is to
    produce the filters that were already accepted.
    """
    seed_catalogue(db)
    db.commit()
    vocabulary = load_vocabulary(db)

    requirement = DeterministicRequirementParser(vocabulary).parse(
        "3x 20-30uF through-hole ceramic capacitor"
    )

    assert requirement.quantity == 3
    assert requirement.category_slug == "capacitor"
    assert requirement.is_complete
    assert requirement.confidence == 1.0

    found = execute(
        db,
        SearchQuery(
            filters=requirement.to_filters(),
            category_slug=requirement.category_slug,
        ),
    )
    assert [part.mpn for part in found] == ["DEMO-CAP-THT-22U"]


def test_a_requirement_the_parser_refused_asks_the_executor_for_nothing(db: Session) -> None:
    """A refusal really does drop the predicate rather than weakening it.

    `1M` under capacitance is megafarads. The line still searches — a category is
    a perfectly good query — but it must not quietly become "any capacitor", *and*
    it must not become a 1 MΩ capacitor either. The refusal travels with it so the
    caller can show why the result is broad.
    """
    seed_catalogue(db)
    db.commit()
    vocabulary = load_vocabulary(db)

    requirement = DeterministicRequirementParser(vocabulary).parse("1M ceramic capacitor")

    assert [item.reason for item in requirement.rejections] == ["implausible"]
    assert {item.template for item in requirement.filters} == {"capacitor_technology"}

    found = execute(
        db,
        SearchQuery(filters=requirement.to_filters(), category_slug=requirement.category_slug),
    )
    assert {part.mpn for part in found} == {"DEMO-CAP-THT-22U", "DEMO-CAP-SMD-22U"}
