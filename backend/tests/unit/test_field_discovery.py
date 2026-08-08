"""The field-discovery schema and its parser. No session, no network, no model.

The pure half of ADR 0011's propose path. Every refusal here is checked against the
raw JSON shape a provider emits rather than against pre-built dataclasses, which is
also what makes `FakeFieldDiscoveryProvider` worth having: the fake replays a
fixture through this same `parse_response`, so the fixture exercises the parsing
instead of bypassing it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models.enums import ValueType
from app.services.enrichment.field_discovery import (
    FakeFieldDiscoveryProvider,
    FieldDiscoveryFixtureMiss,
    FieldDiscoveryRequest,
    FieldDiscoveryResponseError,
    parse_response,
    schema_for,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "field_discovery" / "proposals.json"


def a_request(**kwargs: Any) -> FieldDiscoveryRequest:
    defaults: dict[str, Any] = {
        "document_ref": "sha256:doc",
        "document_text": "Static drain-source on-resistance Rds(on) 45 mOhm",
        "categories": ("semiconductors", "passives"),
        "base_units": ("ohm", "volt", "farad"),
        "max_proposals": 3,
    }
    return FieldDiscoveryRequest(**{**defaults, **kwargs})


def one(**overrides: Any) -> dict[str, Any]:
    body = {
        "name": "on_resistance",
        "value_type": "numeric",
        "base_unit": "ohm",
        "category": "semiconductors",
        "quote": "Static drain-source on-resistance Rds(on) ... 45 mOhm",
    }
    body.update(overrides)
    return body


def parse(*proposals: dict[str, Any], request: FieldDiscoveryRequest | None = None) -> Any:
    return parse_response(
        {"proposals": list(proposals)},
        request or a_request(),
        provider="test",
        model="test",
    )


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


def test_the_schema_constrains_the_category_to_slugs_that_exist() -> None:
    """A model may not invent a category, and the decoder is where that is enforced.

    `applies_to_category` is inherited down the taxonomy, so a fabricated slug
    either matches nothing — a field offered nowhere, which looks like a broken
    filter panel — or collides with a real category and offers the field on a
    subtree nobody chose.
    """
    schema = schema_for(a_request())
    category = schema["properties"]["proposals"]["items"]["properties"]["category"]
    assert category["enum"] == ["semiconductors", "passives", None]


def test_the_schema_constrains_the_base_unit_to_quantities_the_parser_knows() -> None:
    """A numeric field with an unparseable unit accepts no value at all."""
    schema = schema_for(a_request())
    unit = schema["properties"]["proposals"]["items"]["properties"]["base_unit"]
    assert unit["enum"] == ["ohm", "volt", "farad", None]


def test_the_quote_is_required_and_non_empty_in_the_schema() -> None:
    """The one field that makes a proposal reviewable, so it is required twice over.

    Mutation-checked: drop `minLength` here or drop `quote` from `required` and
    this fails, as does `test_a_proposal_with_no_quote_is_refused` below.
    """
    items = schema_for(a_request())["properties"]["proposals"]["items"]
    assert "quote" in items["required"]
    assert items["properties"]["quote"]["minLength"] == 1


def test_the_schema_has_no_substitution_direction_field() -> None:
    """It is what makes a 50 V part stand in for a 25 V one and not the reverse.

    Guessing it produces a substitution search that is wrong by construction while
    looking like it works, which is why `POST /api/parameter-fields` requires it
    with no default — from the person filling the form.
    """
    items = schema_for(a_request())["properties"]["proposals"]["items"]
    assert "substitution_direction" not in items["properties"]
    assert items["additionalProperties"] is False


def test_the_schema_bounds_the_answer_to_what_was_asked_for() -> None:
    assert schema_for(a_request(max_proposals=5))["properties"]["proposals"]["maxItems"] == 5


def test_only_numeric_and_enum_are_offered() -> None:
    """`app.services.parameters` has no writer for `text` or `bool`.

    Such a field could be created and never filled in, which is a filter offered
    in the panel that matches nothing.
    """
    items = schema_for(a_request())["properties"]["proposals"]["items"]
    assert items["properties"]["value_type"]["enum"] == ["numeric", "enum"]


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_a_well_formed_proposal_parses() -> None:
    result = parse(one())
    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.name == "on_resistance"
    assert proposal.value_type is ValueType.NUMERIC
    assert proposal.base_unit == "ohm"
    assert proposal.quote.startswith("Static drain-source")


def test_an_empty_answer_is_not_an_error() -> None:
    """ "This sheet needs no new fields" is the correct reading of most sheets."""
    assert parse().proposals == ()


def test_a_proposal_with_no_quote_is_refused() -> None:
    body = one()
    del body["quote"]
    with pytest.raises(FieldDiscoveryResponseError, match="quote"):
        parse(body)


def test_a_proposal_with_a_blank_quote_is_refused() -> None:
    """An empty string defeats the requirement as thoroughly as a missing key."""
    with pytest.raises(FieldDiscoveryResponseError, match="quote"):
        parse(one(quote="   "))


def test_an_invented_category_is_refused() -> None:
    """The check that is not merely a restatement of the schema.

    A server that quietly ignores `response_format` can emit a slug that does not
    exist, and that server is exactly the one nobody validated.
    """
    with pytest.raises(FieldDiscoveryResponseError, match="not an existing part category"):
        parse(one(category="power-mosfets"))


def test_an_unrecognised_base_unit_is_refused() -> None:
    with pytest.raises(FieldDiscoveryResponseError, match="value parser"):
        parse(one(base_unit="milliohms"))


def test_an_unwritable_value_type_is_refused() -> None:
    with pytest.raises(FieldDiscoveryResponseError, match="value_type"):
        parse(one(value_type="text"))


def test_the_same_field_twice_is_a_malformed_response() -> None:
    """Both entries would upsert one proposal row, so the second overwrites the first.

    The response's own disagreement would vanish silently. Same call
    `extract.parse_response` makes for a repeated part number.
    """
    with pytest.raises(FieldDiscoveryResponseError, match="more than once"):
        parse(one(), one(quote="somewhere else entirely"))


def test_more_proposals_than_were_asked_for_is_refused() -> None:
    request = a_request(max_proposals=1)
    with pytest.raises(FieldDiscoveryResponseError, match="at most 1"):
        parse(one(), one(name="gate_charge"), request=request)


def test_an_empty_optional_string_reads_as_absent() -> None:
    """A model filling `note` with "" is saying nothing, and there is nothing to lose.

    `quote` deliberately does not get this treatment — there it is a refusal.
    """
    assert parse(one(note="  ", stated_value=""))
    proposal = parse(one(note="  ", stated_value="")).proposals[0]
    assert proposal.note is None
    assert proposal.stated_value is None


def test_a_request_with_no_text_is_refused_at_construction() -> None:
    """A document with no extracted text belongs to the extraction queue."""
    with pytest.raises(ValueError, match="nothing to read"):
        a_request(document_text="   ")


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


def test_the_fake_replays_the_fixture_through_the_real_parser() -> None:
    """Which is the only reason the fake is worth having.

    A fake returning pre-built objects would exercise none of the refusals above,
    so they would be tested against hand-written inputs and never against the
    shape the fixture — and therefore a model — actually emits.
    """
    provider = FakeFieldDiscoveryProvider(FIXTURE)
    result = provider.propose(
        a_request(
            document_ref="sha256:mosfet-family-sheet",
            categories=("semiconductors",),
            base_units=("ohm", "volt"),
            max_proposals=12,
        )
    )
    assert [row.name for row in result.proposals] == ["on_resistance", "gate_threshold_voltage"]
    assert all(row.quote for row in result.proposals)
    # A replayed response has no latency of its own, and inventing one would put a
    # fabricated number into a benchmark.
    assert result.stats is None


def test_the_fake_can_replay_an_empty_answer() -> None:
    provider = FakeFieldDiscoveryProvider(FIXTURE)
    result = provider.propose(a_request(document_ref="sha256:nothing-new-sheet"))
    assert result.proposals == ()


def test_a_fixture_miss_raises_rather_than_answering_nothing() -> None:
    """Because empty is a *meaningful* answer here.

    A fake that returned it on a missing fixture would let a test assert the
    nothing-to-propose path while actually testing a typo in a `document_ref`.
    """
    provider = FakeFieldDiscoveryProvider(FIXTURE)
    with pytest.raises(FieldDiscoveryFixtureMiss):
        provider.propose(a_request(document_ref="sha256:never-recorded"))


def test_the_fixture_is_rejected_by_a_request_that_forbids_its_category() -> None:
    """The fixture is not a bypass: it goes through the same category check.

    Worth asserting explicitly, because "the fake accepts anything" is how a
    recorded fixture stops being evidence about what a real response must satisfy.
    """
    provider = FakeFieldDiscoveryProvider(FIXTURE)
    with pytest.raises(FieldDiscoveryResponseError, match="not an existing part category"):
        provider.propose(
            a_request(
                document_ref="sha256:mosfet-family-sheet",
                categories=("passives",),
                base_units=("ohm", "volt"),
                max_proposals=12,
            )
        )
