"""Editing a part's field values by hand.

Iliana: "how can I edit the part category values?"

The answer was: you could not. A value only ever appeared because a *source*
proposed one — an MPN decoder, a datasheet extraction, a BOM import — and a human
accepted or corrected it in the review queue. For a part nobody's decoder
recognised there was no door at all, which made every field authored on a category
decorative, and made a multi-valued list field unreachable by hand.

Two of the four value types had no writer anywhere in the codebase either:
**nothing had ever written `value_text` or `value_bool`**, so a text or yes/no
field was declarable and permanently empty.

So the tests are: each of the four types round-trips through the API and then
*finds the part by a search on it*, because "the value stored" is not the
deliverable — "you can filter by it" is. Plus the refusals, each of which is a
mistake the search path would otherwise meet later:

* a value the grammar cannot read, and one it can read but calls absurd (`1M`
  under capacitance);
* a one-sided limit, which no interval-overlap query can match;
* a field the part's category does not offer;
* the wrong shape for the type — a bool sent as text.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.parameter import ParameterValue
from app.scripts.seed_demo import seed_all


def _session() -> Session:
    return get_session_factory()()


def _seed() -> None:
    session = _session()
    try:
        seed_all(session)
        session.commit()
    finally:
        session.close()


def _category(client: TestClient, slug: str) -> int:
    listed = client.get("/api/part-categories").json()
    return next(row["id"] for row in listed if row["slug"] == slug)


def _part(client: TestClient, name: str, category_id: int | None = None) -> int:
    body: dict[str, Any] = {"name": name, "part_kind": "component"}
    if category_id is not None:
        body["category_id"] = category_id
    response = client.post("/api/parts", json=body)
    assert response.status_code == 201, response.text
    return int(response.json()["part"]["id"])


def _field(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/parameter-fields", json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()["field"]
    return payload


def _search(client: TestClient, template: str, value: str) -> list[int]:
    response = client.post(
        "/api/search/parts", json={"filters": [{"template": template, "value": value}]}
    )
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["results"]]


# ---------------------------------------------------------------------------
# The four types, each stored and then searched for
# ---------------------------------------------------------------------------


def test_a_numeric_value_can_be_typed_and_then_filtered_by(client: TestClient) -> None:
    _seed()
    part_id = _part(client, "Some 22uF cap", _category(client, "capacitor"))

    written = client.put(f"/api/parts/{part_id}/parameters/capacitance", json={"value": "22uF"})
    assert written.status_code == 200, written.text
    parameter = written.json()["parameter"]
    # Both bounds populated is the invariant that makes it findable at all.
    assert parameter["value_min"] == parameter["value_max"]
    assert parameter["display"] == "22 μF"
    assert parameter["provenance"] == "manual"

    # The demo data already has 22uF capacitors, so this asserts the new part is
    # *among* the matches — the point is that a hand-typed value is searchable.
    assert part_id in _search(client, "capacitance", "20-30uF")


def test_a_text_value_round_trips(client: TestClient) -> None:
    """`value_text` had never been written by anything before this."""
    _seed()
    category_id = _category(client, "ic")
    _field(
        client,
        name="marking",
        display_name="Top marking",
        value_type="text",
        substitution_direction="exact",
        applies_to_category="ic",
    )
    part_id = _part(client, "Unknown SOT-23", category_id)

    written = client.put(f"/api/parts/{part_id}/parameters/marking", json={"value": "1AM"})
    assert written.status_code == 200, written.text
    assert written.json()["parameter"]["value_text"] == "1AM"

    read = client.get(f"/api/parts/{part_id}/parameters").json()
    marking = next(row for row in read["parameters"] if row["name"] == "marking")
    assert marking["value_text"] == "1AM"
    assert marking["raw_input"] == "1AM"


def test_a_yes_no_value_round_trips(client: TestClient) -> None:
    """`value_bool` had never been written by anything either."""
    _seed()
    category_id = _category(client, "ic")
    _field(
        client,
        name="automotive_grade",
        display_name="Automotive grade",
        value_type="bool",
        substitution_direction="exact",
        applies_to_category="ic",
    )
    part_id = _part(client, "AEC-Q100 part", category_id)

    written = client.put(
        f"/api/parts/{part_id}/parameters/automotive_grade", json={"checked": True}
    )
    assert written.status_code == 200, written.text
    assert written.json()["parameter"]["value_bool"] is True
    # 'yes' rather than 'True': the lossless record of what a human said.
    assert written.json()["parameter"]["raw_input"] == "yes"


def test_a_single_option_can_be_picked_and_filtered_by(client: TestClient) -> None:
    _seed()
    part_id = _part(client, "A C0G cap", _category(client, "capacitor"))

    written = client.put(f"/api/parts/{part_id}/parameters/dielectric", json={"choices": ["c0g"]})
    assert written.status_code == 200, written.text
    # Resolution is case-insensitive; what comes back is the option's own spelling.
    assert [choice["key"].lower() for choice in written.json()["parameter"]["choices"]] == ["c0g"]

    assert _search(client, "dielectric", "c0g") == [part_id]


def test_several_options_can_be_picked_on_a_multi_valued_field(client: TestClient) -> None:
    """What made the multi-choice field reachable by hand at all."""
    _seed()
    category_id = _category(client, "ic")
    _field(
        client,
        name="interface",
        display_name="Interface",
        value_type="enum",
        substitution_direction="exact",
        applies_to_category="ic",
        allow_multiple=True,
        choices=[
            {"key": "i2c", "label": "I2C"},
            {"key": "spi", "label": "SPI"},
            {"key": "uart", "label": "UART"},
        ],
    )
    part_id = _part(client, "MCP23017", category_id)

    written = client.put(
        f"/api/parts/{part_id}/parameters/interface", json={"choices": ["i2c", "spi"]}
    )
    assert written.status_code == 200, written.text
    assert {choice["key"] for choice in written.json()["parameter"]["choices"]} == {"i2c", "spi"}

    assert _search(client, "interface", "i2c") == [part_id]
    assert _search(client, "interface", "spi") == [part_id]
    assert _search(client, "interface", "uart") == []


# ---------------------------------------------------------------------------
# Reading the editor
# ---------------------------------------------------------------------------


def test_the_editor_lists_fields_with_and_without_values(client: TestClient) -> None:
    """Empty fields are returned too — a field you cannot see is one you will not
    fill in."""
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))
    client.put(f"/api/parts/{part_id}/parameters/capacitance", json={"value": "22uF"})

    read = client.get(f"/api/parts/{part_id}/parameters").json()
    assert read["filed"] is True
    assert read["category"] == "capacitor"
    by_name = {row["name"]: row for row in read["parameters"]}
    assert by_name["capacitance"]["raw_input"] == "22uF"
    assert by_name["voltage_rating"]["raw_input"] is None
    # And it says which fields come from further up, because that is why a
    # capacitor is offered `package`.
    assert by_name["capacitance"]["inherited"] is False
    assert by_name["package"]["inherited"] is True
    # A list field carries its options, so the editor needs no second request.
    assert [option["key"] for option in by_name["dielectric"]["options"]]


def test_an_unfiled_part_says_so_and_gets_only_the_global_fields(client: TestClient) -> None:
    _seed()
    part_id = _part(client, "Filed nowhere")

    read = client.get(f"/api/parts/{part_id}/parameters").json()
    assert read["filed"] is False
    assert read["category"] is None
    names = {row["name"] for row in read["parameters"]}
    # `package` names no category, so every part has one. `capacitance` belongs to
    # capacitors, and offering it here would file a value against a field no filter
    # panel for this part will ever show.
    assert "package" in names
    assert "capacitance" not in names


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


def test_an_implausible_value_is_refused_with_the_parsers_own_reason(client: TestClient) -> None:
    """`1M` under capacitance is syntactically fine and physically absurd — the
    single most valuable refusal in the system, and it has to reach the box."""
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))

    refused = client.put(f"/api/parts/{part_id}/parameters/capacitance", json={"value": "1M"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["reason"] == "implausible"
    assert refused.json()["detail"]["template"] == "capacitance"


def test_gibberish_is_refused_as_a_syntax_error(client: TestClient) -> None:
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))
    refused = client.put(f"/api/parts/{part_id}/parameters/capacitance", json={"value": "banana"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["reason"] in {"syntax", "unknown_unit"}


def test_a_one_sided_limit_is_refused(client: TestClient) -> None:
    """Search is an interval-overlap test, so a half-bounded row matches nothing."""
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))
    refused = client.put(f"/api/parts/{part_id}/parameters/capacitance", json={"value": ">=10uF"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["reason"] == "unbounded_value"


def test_a_field_the_category_does_not_offer_is_refused(client: TestClient) -> None:
    """Otherwise a value could be filed against a field no filter panel for this
    part will ever show."""
    _seed()
    part_id = _part(client, "A resistor", _category(client, "resistor"))
    refused = client.put(f"/api/parts/{part_id}/parameters/capacitance", json={"value": "22uF"})
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"]["reason"] == "field_not_offered"


def test_the_wrong_shape_for_the_type_is_refused(client: TestClient) -> None:
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))
    refused = client.put(f"/api/parts/{part_id}/parameters/dielectric", json={"value": "c0g"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["reason"] == "wrong_value_shape"


def test_two_options_on_a_single_valued_field_are_refused(client: TestClient) -> None:
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))
    refused = client.put(
        f"/api/parts/{part_id}/parameters/dielectric", json={"choices": ["c0g", "x7r"]}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "too_many_choices"


def test_an_unknown_option_is_refused_and_lists_the_known_ones(client: TestClient) -> None:
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))
    refused = client.put(
        f"/api/parts/{part_id}/parameters/dielectric", json={"choices": ["unobtainium"]}
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["reason"] == "unknown_choice"
    assert "c0g" in refused.json()["detail"]["message"].lower()


# ---------------------------------------------------------------------------
# Clearing one
# ---------------------------------------------------------------------------


def test_clearing_a_value_removes_the_row_rather_than_blanking_it(client: TestClient) -> None:
    """A row with every value column null is a part claiming an attribute it has no
    answer for: counted as populated, matching nothing, indistinguishable from a
    bug."""
    _seed()
    part_id = _part(client, "Some cap", _category(client, "capacitor"))
    client.put(f"/api/parts/{part_id}/parameters/capacitance", json={"value": "22uF"})

    cleared = client.delete(f"/api/parts/{part_id}/parameters/capacitance")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["removed"] is True

    session = _session()
    try:
        assert (
            session.execute(
                select(ParameterValue).where(ParameterValue.part_id == part_id)
            ).scalar_one_or_none()
            is None
        )
    finally:
        session.close()

    assert part_id not in _search(client, "capacitance", "20-30uF")
    # And clearing again is honest about having removed nothing.
    again = client.delete(f"/api/parts/{part_id}/parameters/capacitance")
    assert again.json()["removed"] is False


def test_clearing_a_multi_valued_field_takes_its_options_with_it(client: TestClient) -> None:
    _seed()
    category_id = _category(client, "ic")
    _field(
        client,
        name="interface",
        display_name="Interface",
        value_type="enum",
        substitution_direction="exact",
        applies_to_category="ic",
        allow_multiple=True,
        choices=[{"key": "i2c", "label": "I2C"}, {"key": "spi", "label": "SPI"}],
    )
    part_id = _part(client, "MCP23017", category_id)
    client.put(f"/api/parts/{part_id}/parameters/interface", json={"choices": ["i2c", "spi"]})

    cleared = client.delete(f"/api/parts/{part_id}/parameters/interface")
    assert cleared.status_code == 200, cleared.text
    assert _search(client, "interface", "i2c") == []
    # The options themselves survive — deleting this part's answers is not deleting
    # the field's vocabulary.
    listed = client.get("/api/parameter-fields?category=ic").json()
    interface = next(row for row in listed if row["name"] == "interface")
    assert {choice["key"] for choice in interface["choices"]} == {"i2c", "spi"}
