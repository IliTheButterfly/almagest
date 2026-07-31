"""List fields that hold more than one option at once.

Iliana: "can you add an option for the list fields to allow multiple choices at
once".

The interesting part is not that a part can hold two options — it is that adding
multiplicity **must not break the invariant the whole search design rests on**.
`UNIQUE(part_id, template_id)` is what lets a multi-predicate parametric query be
plain `JOIN`s that each contribute at most one row; a second value row per option
would turn every such query into a cross product, silently, and worse the more
filters are applied. So the tests here are in two groups:

* the feature — a part holds two options, and a filter on either finds it once;
* the invariant — a *five*-predicate search across a multi-valued field still
  returns each part exactly once, which is the thing that would break if the
  options had been joined rather than matched with `EXISTS`.

Plus the refusals: several options on a field that holds one, and turning
multiplicity off under parts that are using it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.catalog import Part
from app.models.parameter import ParameterTemplate, ParameterValue, ParameterValueChoice
from app.scripts.seed_demo import seed_all
from app.services import parameters


def _session() -> Session:
    return get_session_factory()()


def _seed() -> None:
    session = _session()
    try:
        seed_all(session)
        session.commit()
    finally:
        session.close()


def _field(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/parameter-fields", json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()["field"]
    return payload


def _multi_field(client: TestClient, name: str = "interface") -> dict[str, Any]:
    return _field(
        client,
        name=name,
        display_name="Interface",
        value_type="enum",
        substitution_direction="exact",
        allow_multiple=True,
        choices=[
            {"key": "i2c", "label": "I²C"},
            {"key": "spi", "label": "SPI"},
            {"key": "uart", "label": "UART"},
        ],
    )


def _part(client: TestClient, name: str) -> int:
    response = client.post("/api/parts", json={"name": name, "part_kind": "component"})
    assert response.status_code == 201, response.text
    return int(response.json()["part"]["id"])


def _set_choices(part_id: int, template_name: str, keys: list[str]) -> None:
    session = _session()
    try:
        part = session.get(Part, part_id)
        assert part is not None
        template = session.execute(
            select(ParameterTemplate).where(ParameterTemplate.name == template_name)
        ).scalar_one()
        parameters.set_choices(session, part, template, keys)
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The feature
# ---------------------------------------------------------------------------


def test_a_part_holds_several_options_and_either_one_finds_it(client: TestClient) -> None:
    _seed()
    _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi"])

    for wanted in ("i2c", "spi"):
        found = client.post(
            "/api/search/parts",
            json={"filters": [{"template": "interface", "value": wanted}]},
        )
        assert found.status_code == 200, found.text
        assert [row["id"] for row in found.json()["results"]] == [part_id], wanted

    # And an option it does not hold does not find it.
    missed = client.post(
        "/api/search/parts", json={"filters": [{"template": "interface", "value": "uart"}]}
    )
    assert missed.json()["results"] == []


def test_two_options_ticked_at_once_match_a_part_holding_either_once(client: TestClient) -> None:
    """The OR semantics of a facet are unchanged — and the part appears **once**,
    which is the part that would regress if options were joined rather than
    matched."""
    _seed()
    _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi"])

    found = client.post(
        "/api/search/parts", json={"filters": [{"template": "interface", "value": "i2c,spi"}]}
    )
    assert [row["id"] for row in found.json()["results"]] == [part_id]


def test_the_unique_invariant_survives_a_multi_predicate_search(client: TestClient) -> None:
    """The reason multiplicity lives in a child table at all.

    Five predicates, one of them on a field where the part holds three options. If
    the options were joined instead of matched with `EXISTS`, this part would come
    back three times — and every additional filter would multiply it again.
    """
    _seed()
    _multi_field(client)
    _field(
        client,
        name="footprint_family",
        display_name="Footprint family",
        value_type="enum",
        substitution_direction="exact",
        allow_multiple=True,
        choices=[
            {"key": "dip", "label": "DIP"},
            {"key": "soic", "label": "SOIC"},
            {"key": "tssop", "label": "TSSOP"},
        ],
    )
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi", "uart"])
    _set_choices(part_id, "footprint_family", ["dip", "soic"])

    found = client.post(
        "/api/search/parts",
        json={
            "filters": [
                {"template": "interface", "value": "i2c"},
                {"template": "interface", "value": "spi"},
                {"template": "footprint_family", "value": "dip"},
                {"template": "footprint_family", "value": "soic"},
                {"template": "interface", "value": "i2c,uart"},
            ]
        },
    )
    assert found.status_code == 200, found.text
    ids = [row["id"] for row in found.json()["results"]]
    assert ids == [part_id], f"fan-out: {ids}"
    assert found.json()["total"] == 1


def test_the_set_is_replaced_not_appended(client: TestClient) -> None:
    """ "Now only SMD" has to be sayable, so the writer takes the whole set."""
    _seed()
    _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi"])
    _set_choices(part_id, "interface", ["uart"])

    session = _session()
    try:
        row = session.execute(
            select(ParameterValue).where(ParameterValue.part_id == part_id)
        ).scalar_one()
        held = {choice.key for choice in parameters.choices_held(session, row)}
        assert held == {"uart"}
        # Back to a single option, so the single-valued mirror is meaningful again.
        assert row.choice_id is not None
    finally:
        session.close()


def test_choice_id_is_null_while_several_are_held(client: TestClient) -> None:
    """So nothing can read one option out of three and believe it is the answer."""
    _seed()
    _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi"])

    session = _session()
    try:
        row = session.execute(
            select(ParameterValue).where(ParameterValue.part_id == part_id)
        ).scalar_one()
        assert row.choice_id is None
        assert len(parameters.choices_held(session, row)) == 2
    finally:
        session.close()


def test_a_single_valued_field_still_mirrors_into_choice_id(client: TestClient) -> None:
    """Every existing consumer reads `choice_id`, so single-valued writes — which is
    what every MPN decoder and the enrichment promoter do — must not change shape."""
    _seed()
    part_id = _part(client, "Some ceramic cap")
    _set_choices(part_id, "dielectric", ["c0g"])

    session = _session()
    try:
        row = session.execute(
            select(ParameterValue).where(ParameterValue.part_id == part_id)
        ).scalar_one()
        assert row.choice_id is not None
        # And the child table is written too, which is what search reads.
        assert (
            session.execute(
                select(ParameterValueChoice).where(ParameterValueChoice.value_id == row.id)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def test_facet_counts_see_a_multi_valued_field(client: TestClient) -> None:
    """Counted from the child table: counting `choice_id` would report every option
    of a multi-valued field as zero — "you own none of these" with the parts in
    plain sight."""
    _seed()
    _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi"])

    facets = client.post("/api/parameter-templates", json={})
    assert facets.status_code == 200, facets.text
    interface = next(row for row in facets.json()["templates"] if row["name"] == "interface")
    counts = {choice["key"]: choice["count"] for choice in interface["choices"]}
    assert counts["i2c"] == 1
    assert counts["spi"] == 1
    assert counts["uart"] == 0


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


def test_several_options_on_a_single_valued_field_are_refused(client: TestClient) -> None:
    """Rather than keeping the first, which would look exactly like success."""
    _seed()
    _field(
        client,
        name="interface",
        display_name="Interface",
        value_type="enum",
        substitution_direction="exact",
        choices=[{"key": "i2c", "label": "I²C"}, {"key": "spi", "label": "SPI"}],
    )
    part_id = _part(client, "MCP23017")
    with pytest.raises(parameters.TooManyChoices):
        _set_choices(part_id, "interface", ["i2c", "spi"])


def test_multiple_cannot_be_turned_on_for_a_number(client: TestClient) -> None:
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "esr",
            "display_name": "ESR",
            "value_type": "numeric",
            "base_unit": "ohm",
            "substitution_direction": "lower_ok",
            "allow_multiple": True,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "multiple_on_non_enum"


def test_turning_it_on_later_is_allowed(client: TestClient) -> None:
    """Safe by construction: every stored value holds one option, and one option is
    a valid set of one."""
    _seed()
    field = _field(
        client,
        name="interface",
        display_name="Interface",
        value_type="enum",
        substitution_direction="exact",
        choices=[{"key": "i2c", "label": "I²C"}, {"key": "spi", "label": "SPI"}],
    )
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c"])

    edited = client.patch(f"/api/parameter-fields/{field['id']}", json={"allow_multiple": True})
    assert edited.status_code == 200, edited.text
    assert edited.json()["field"]["allow_multiple"] is True

    _set_choices(part_id, "interface", ["i2c", "spi"])
    found = client.post(
        "/api/search/parts", json={"filters": [{"template": "interface", "value": "spi"}]}
    )
    assert [row["id"] for row in found.json()["results"]] == [part_id]


def test_turning_it_off_under_parts_using_it_is_refused(client: TestClient) -> None:
    """Which of a part's three options survives is not the API's decision to make."""
    _seed()
    field = _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi"])

    refused = client.patch(f"/api/parameter-fields/{field['id']}", json={"allow_multiple": False})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "multiple_in_use"
    assert "1 part" in refused.json()["detail"]["message"]


def test_turning_it_off_is_fine_when_nobody_holds_several(client: TestClient) -> None:
    _seed()
    field = _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c"])

    edited = client.patch(f"/api/parameter-fields/{field['id']}", json={"allow_multiple": False})
    assert edited.status_code == 200, edited.text
    assert edited.json()["field"]["allow_multiple"] is False


def test_an_option_a_part_holds_cannot_be_deleted(client: TestClient) -> None:
    """The `RESTRICT` guard has to hold through the child table too, or the database
    refuses it as an `IntegrityError` — a 500 with no number in it."""
    _seed()
    field = _multi_field(client)
    part_id = _part(client, "MCP23017")
    _set_choices(part_id, "interface", ["i2c", "spi"])

    listed = client.get(f"/api/parameter-fields/{field['id']}").json()
    spi = next(choice for choice in listed["choices"] if choice["key"] == "spi")
    refused = client.delete(f"/api/parameter-fields/{field['id']}/choices/{spi['id']}")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "choice_in_use"
