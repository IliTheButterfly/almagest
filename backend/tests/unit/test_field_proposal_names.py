"""The string arithmetic the duplicate check is built on. No session, no migrations.

`nearest_existing_template` needs real templates and lives in
`tests/integration/test_field_proposals.py`. What is here is the four pure
functions underneath it, because a bug in any of them fails silently: it does not
raise, it just stops finding the duplicate, and a duplicate `parameter_template`
never surfaces as an error anywhere — parametric search simply reports parts as
having no value for a field whose value is filed under the other name.
"""

from __future__ import annotations

import pytest

from app.services.field_proposals import (
    SYNONYM_GROUPS,
    TOKEN_OVERLAP_THRESHOLD,
    _tokens,
    are_synonyms,
    normalise_field_name,
    units_compatible,
)


@pytest.mark.parametrize(
    "spelling",
    ["voltage_rating", "Voltage Rating", "VOLTAGE-RATING", " voltage.rating ", "VoltageRating"],
)
def test_every_spelling_of_one_name_normalises_together(spelling: str) -> None:
    """The dedup key throws away case, punctuation and separators.

    All five of these are things a datasheet, a form field and a model each
    produce for one concept, and the whole point of the key is that they land on
    one proposal row rather than five.
    """
    assert normalise_field_name(spelling) == "voltagerating"


def test_separators_are_removed_rather_than_collapsed() -> None:
    """`Rds(on)` and `RdsOn` are the same name.

    The spellings that actually collide disagree about *whether* there is a
    separator at all, so a rule that turned each run of punctuation into one
    character would put these in different buckets and the proposal table would
    hold both.
    """
    assert normalise_field_name("Rds(on)") == normalise_field_name("RdsOn") == "rdson"


def test_digits_survive_normalisation() -> None:
    """`voltage_2` and `voltage_3` are different fields.

    A normaliser that dropped the number would merge them, and merging two real
    fields is the same class of harm as failing to spot a duplicate.
    """
    assert normalise_field_name("voltage_2") != normalise_field_name("voltage_3")


def test_a_name_of_only_punctuation_normalises_to_nothing() -> None:
    """Which `record_proposal` and `nearest_existing_template` both refuse.

    An empty key would be UNIQUE, so the first punctuation-only name would claim
    it and every subsequent one would silently merge into that row.
    """
    assert normalise_field_name("---()---") == ""


def test_camel_case_and_snake_case_produce_the_same_tokens() -> None:
    assert _tokens("RdsOn") == _tokens("rds_on") == frozenset({"rds", "on"})


def test_a_trailing_plural_is_stemmed_but_nothing_else_is() -> None:
    """`resistances` matches `resistance`; `rating` is not stemmed to `rate`.

    The asymmetry is deliberate. Dropping a plural merges two spellings of one
    word, and stemming further would merge a voltage *rating* with a slew *rate* —
    two fields that are not the same and whose merge nobody would notice.
    """
    assert _tokens("resistances") == _tokens("resistance")
    assert _tokens("rating") != _tokens("rate")


def test_two_names_sharing_one_word_of_two_are_below_the_bar() -> None:
    """`rise_time` and `fall_time` share `time` and must not match on tokens.

    One third, against a two-thirds threshold. This is the case that says the
    threshold is doing work rather than being decoration: with any bar at or below
    a half, every `*_time` field in the install would be reported as a near-match
    for every other and the warning would stop being read.
    """
    left, right = _tokens("rise_time"), _tokens("fall_time")
    shared = left & right
    assert len(shared) / min(len(left), len(right)) < TOKEN_OVERLAP_THRESHOLD


def test_the_curated_synonyms_connect_a_symbol_to_a_word() -> None:
    """`Rds(on)` and `on_resistance` are one concept, and no normaliser gets there.

    This is what `SYNONYM_GROUPS` exists for: the duplicate that actually happens
    is a datasheet's symbol against an inventory's word, and the two share no
    stem. Asserted in both directions, because the lookup is built by iterating
    the groups and a one-way build would work for whichever spelling happened to
    be written first.
    """
    assert are_synonyms("Rds(on)", "on_resistance")
    assert are_synonyms("on_resistance", "RDS(ON)")
    assert are_synonyms("ESR", "equivalent_series_resistance")


def test_unrelated_names_are_not_synonyms() -> None:
    assert not are_synonyms("capacitance", "on_resistance")


def test_every_synonym_group_has_at_least_two_members() -> None:
    """A group of one connects nothing and reads as though it did."""
    for group in SYNONYM_GROUPS:
        assert len({normalise_field_name(member) for member in group}) >= 2, group


def test_no_name_appears_in_two_synonym_groups() -> None:
    """Otherwise the second group silently wins and the first stops working.

    The lookup is a flat dict keyed on the normalised name, so a name in two
    groups is not an error — it is a group that quietly stopped connecting
    anything, which is exactly the failure mode this table must not have.
    """
    seen: dict[str, int] = {}
    for index, group in enumerate(SYNONYM_GROUPS):
        for member in group:
            key = normalise_field_name(member)
            assert key not in seen, f"{member!r} is in groups {seen[key]} and {index}"
            seen[key] = index


def test_an_unknown_unit_is_compatible_with_everything() -> None:
    """Because `cross_check` records proposals with no suggested unit at all.

    Treating "unknown" as "different" would suppress the near-matches the check
    exists to surface. The cost of a false near-match is one extra line for a
    person to read; the cost of a missed one is the duplicate.
    """
    assert units_compatible(None, "ohm")
    assert units_compatible("ohm", None)
    assert units_compatible(None, None)


def test_a_stated_mismatch_is_not_compatible() -> None:
    """Ohms against farads is two fields, whatever their names share."""
    assert not units_compatible("ohm", "farad")


def test_units_compare_canonically() -> None:
    assert units_compatible("OHM", "ohm")
