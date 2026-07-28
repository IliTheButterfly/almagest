"""The parameter write path.

The load-bearing assertion here is that a **scalar** gets `value_min` and
`value_max` populated. Parametric search is an interval-overlap test, so a row
carrying only `value_nominal` is invisible to every range query — a 22 µF
capacitor would simply not appear in a search for 20–30 µF, silently.
"""

from __future__ import annotations

import pytest
from elec_value_parser import ImplausibleValueError, ValueParseError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import Provenance
from app.models.parameter import ParameterTemplate, ParameterValue
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services import parameters
from app.services.parameters import ChoiceNotFound, LowerPrecedence
from app.services.search.value_parser import TemplateNotNumeric, supported_quantity
from tests.factories import make_part


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_categories(db)
    seed_parameter_templates(db)
    return db


def template(db: Session, name: str) -> ParameterTemplate:
    return db.execute(select(ParameterTemplate).where(ParameterTemplate.name == name)).scalar_one()


def test_a_scalar_gets_a_degenerate_interval(seeded: Session) -> None:
    part = make_part(seeded)
    row = parameters.set_numeric(seeded, part, template(seeded, "capacitance"), "22uF")

    assert row.value_nominal == pytest.approx(22e-6)
    assert row.value_min == pytest.approx(22e-6)
    assert row.value_max == pytest.approx(22e-6)


def test_a_scalar_with_tolerance_gets_its_band(seeded: Session) -> None:
    part = make_part(seeded)
    row = parameters.set_numeric(seeded, part, template(seeded, "resistance"), "10k ±1%")

    assert row.value_nominal == pytest.approx(10_000)
    assert row.value_min == pytest.approx(9_900)
    assert row.value_max == pytest.approx(10_100)
    assert row.tolerance_pct == pytest.approx(1.0)


def test_a_range_leaves_nominal_unset(seeded: Session) -> None:
    part = make_part(seeded)
    row = parameters.set_numeric(seeded, part, template(seeded, "capacitance"), "20-30uF")

    assert row.value_nominal is None
    assert row.value_min == pytest.approx(20e-6)
    assert row.value_max == pytest.approx(30e-6)


def test_shorthand_is_understood(seeded: Session) -> None:
    part = make_part(seeded)
    row = parameters.set_numeric(seeded, part, template(seeded, "resistance"), "4k7")
    assert row.value_nominal == 4700.0
    assert row.raw_input == "4k7"


def test_display_components_are_stored(seeded: Session) -> None:
    """So '4700 Ω' renders as '4.7 kΩ' without recomputing, and without storing
    a formatted string that cannot be re-unitised."""
    part = make_part(seeded)
    row = parameters.set_numeric(seeded, part, template(seeded, "resistance"), "4700")

    assert row.display_mantissa == pytest.approx(4.7)
    assert row.display_si_prefix == "k"
    assert row.display_unit_symbol == "Ω"


def test_the_template_supplies_the_quantity(seeded: Session) -> None:
    """`1M` is a fine resistance and an impossible capacitance. The template is
    what tells the parser which one it is looking at."""
    part = make_part(seeded)
    row = parameters.set_numeric(seeded, part, template(seeded, "resistance"), "1M")
    assert row.value_nominal == pytest.approx(1e6)

    with pytest.raises(ImplausibleValueError):
        parameters.set_numeric(seeded, part, template(seeded, "capacitance"), "1M")


def test_a_template_window_narrows_the_librarys_own(seeded: Session) -> None:
    """Two independent guards, failing differently. The library's window is
    per-quantity and universal; a template's is per-field and may be tighter."""
    part = make_part(seeded)
    decoupling = template(seeded, "capacitance")
    decoupling.plausible_max = 100e-6
    seeded.flush()

    parameters.set_numeric(seeded, part, decoupling, "22uF")

    with pytest.raises(ValueParseError) as excinfo:
        parameters.set_numeric(seeded, make_part(seeded, name="big"), decoupling, "0.5F")
    assert excinfo.value.reason == "implausible"


def test_a_bad_value_writes_nothing(seeded: Session) -> None:
    """A parse failure is a review-queue item, not a partially-written row."""
    part = make_part(seeded)
    with pytest.raises(ValueParseError):
        parameters.set_numeric(seeded, part, template(seeded, "capacitance"), "banana")

    assert (
        seeded.execute(
            select(ParameterValue).where(ParameterValue.part_id == part.id)
        ).scalar_one_or_none()
        is None
    )


def test_enum_choices_and_dual_notation(seeded: Session) -> None:
    """`0603` and `1608` are the same package under two conventions. Both must
    resolve to one row, so the user is never asked which one a source used."""
    part = make_part(seeded)
    package = template(seeded, "package")

    imperial = parameters.set_choice(seeded, part, package, "0603")
    metric = parameters.set_choice(seeded, part, package, "1608")
    assert imperial.choice_id == metric.choice_id


def test_choice_aliases_are_case_insensitive(seeded: Session) -> None:
    part = make_part(seeded)
    mounting = template(seeded, "mounting_type")
    assert (
        parameters.set_choice(seeded, part, mounting, "through-hole").choice_id
        == parameters.set_choice(seeded, part, mounting, "THT").choice_id
    )


def test_unknown_choice_is_refused_with_the_options(seeded: Session) -> None:
    part = make_part(seeded)
    with pytest.raises(ChoiceNotFound, match="THT"):
        parameters.set_choice(seeded, part, template(seeded, "mounting_type"), "levitating")


def test_numeric_parsing_is_refused_for_an_enum_template(seeded: Session) -> None:
    part = make_part(seeded)
    with pytest.raises(TemplateNotNumeric):
        parameters.set_numeric(seeded, part, template(seeded, "mounting_type"), "4k7")


def test_one_row_per_part_and_template(seeded: Session) -> None:
    """`UNIQUE(part_id, template_id)` is what guarantees each join in a
    multi-predicate search contributes at most one row."""
    part = make_part(seeded)
    capacitance = template(seeded, "capacitance")

    first = parameters.set_numeric(seeded, part, capacitance, "22uF")
    second = parameters.set_numeric(seeded, part, capacitance, "47uF")

    assert first.id == second.id
    assert second.value_nominal == pytest.approx(47e-6)

    rows = seeded.execute(select(ParameterValue).where(ParameterValue.part_id == part.id)).scalars()
    assert len(list(rows)) == 1


def test_the_unique_constraint_is_real(seeded: Session) -> None:
    part = make_part(seeded)
    capacitance = template(seeded, "capacitance")
    parameters.set_numeric(seeded, part, capacitance, "22uF")

    seeded.add(ParameterValue(part_id=part.id, template_id=capacitance.id, raw_input="duplicate"))
    with pytest.raises(IntegrityError):
        seeded.flush()
    seeded.rollback()


def test_a_weaker_source_cannot_overwrite_a_stronger_one(seeded: Session) -> None:
    """manual > datasheet_table > mpn_decoder > distributor_freetext >
    llm_inferred. Nothing overrides a human."""
    part = make_part(seeded)
    capacitance = template(seeded, "capacitance")
    parameters.set_numeric(seeded, part, capacitance, "22uF", provenance=Provenance.MANUAL)

    with pytest.raises(LowerPrecedence):
        parameters.set_numeric(
            seeded, part, capacitance, "47uF", provenance=Provenance.LLM_INFERRED
        )

    seeded.refresh(part)
    row = seeded.execute(
        select(ParameterValue).where(ParameterValue.part_id == part.id)
    ).scalar_one()
    assert row.value_nominal == pytest.approx(22e-6)


def test_a_stronger_source_does_overwrite(seeded: Session) -> None:
    part = make_part(seeded)
    capacitance = template(seeded, "capacitance")
    parameters.set_numeric(
        seeded, part, capacitance, "22uF", provenance=Provenance.DISTRIBUTOR_FREETEXT
    )
    row = parameters.set_numeric(seeded, part, capacitance, "47uF", provenance=Provenance.MANUAL)
    assert row.value_nominal == pytest.approx(47e-6)
    assert row.provenance == Provenance.MANUAL


def test_every_seeded_numeric_template_has_a_parseable_unit(seeded: Session) -> None:
    """Catches a template configured with a unit the parser has never heard of,
    at seed time rather than on the first value someone enters."""
    from app.models.enums import ValueType

    numeric = seeded.execute(
        select(ParameterTemplate).where(ParameterTemplate.value_type == ValueType.NUMERIC)
    ).scalars()
    for row in numeric:
        assert supported_quantity(row.base_unit), f"{row.name} has base_unit {row.base_unit!r}"


def test_seeding_is_idempotent(db: Session) -> None:
    seed_categories(db)
    first = seed_parameter_templates(db)
    second = seed_parameter_templates(db)

    assert first != (0, 0)
    assert second == (0, 0)


def test_seed_all_produces_the_worked_search_example(db: Session) -> None:
    """Three capacitors differing in exactly one facet each.

    This is the fixture the design doc's worked query depends on: a search for
    "through-hole 20-30 uF ceramic" must return the first and only the first.
    The query itself lands with the search executor; this pins the data it will
    be asserted against.
    """
    from app.scripts.seed_demo import seed_all

    report = seed_all(db)
    db.commit()

    assert report.templates > 0
    assert report.parts == 5

    caps = {
        row.mpn: row
        for row in db.execute(select(Part).where(Part.mpn.like("DEMO-CAP-%"))).scalars()
    }
    assert len(caps) == 3

    def facets(mpn: str) -> dict[str, object]:
        part = caps[mpn]
        out: dict[str, object] = {}
        for value in db.execute(
            select(ParameterValue).where(ParameterValue.part_id == part.id)
        ).scalars():
            name = db.get(ParameterTemplate, value.template_id).name  # type: ignore[union-attr]
            out[name] = value.choice_id if value.choice_id else value.value_nominal
        return out

    tht_ceramic = facets("DEMO-CAP-THT-22U")
    smd_ceramic = facets("DEMO-CAP-SMD-22U")
    tht_electrolytic = facets("DEMO-CAP-THT-22U-ELEC")

    # All three are 22 uF, so capacitance alone cannot separate them.
    assert (
        tht_ceramic["capacitance"] == smd_ceramic["capacitance"] == tht_electrolytic["capacitance"]
    )
    # They differ in exactly the facet the query filters on.
    assert tht_ceramic["mounting_type"] != smd_ceramic["mounting_type"]
    assert tht_ceramic["capacitor_technology"] != tht_electrolytic["capacitor_technology"]

    # And every one of them is findable by a 20-30 uF interval-overlap test.
    capacitance_values = db.execute(
        select(ParameterValue)
        .join(ParameterTemplate)
        .where(ParameterTemplate.name == "capacitance")
    ).scalars()
    for row in capacitance_values:
        assert row.value_min is not None and row.value_max is not None
        assert row.value_min <= 30e-6 and row.value_max >= 20e-6


def test_seed_all_is_idempotent(db: Session) -> None:
    from app.scripts.seed_demo import seed_all

    seed_all(db)
    db.commit()
    again = seed_all(db)
    db.commit()

    assert (again.categories, again.templates, again.choices, again.parts) == (0, 0, 0, 0)
