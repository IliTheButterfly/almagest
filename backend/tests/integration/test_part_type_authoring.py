"""Authoring part types, categories and the fields you filter on.

Iliana: "we currently have no way to create new part types. The part type creator
should allow you to add fields and units/list for filtering."

The word to hold onto is **filtering**. "The field exists" is not the deliverable —
a `parameter_template` row that no value can be entered against, or that a search
silently never matches, is worse than no field at all, because it looks like it
works. So the two headline tests here author a field over the API and then *search
for a part by it*, end to end through the same executor the filter panel uses.

The rest are the ways that goes wrong, each of which is silent by default:

* a `base_unit` the parser does not know (`ohms`, `µF`) — accepted, every value
  then refused, field permanently empty;
* a `value_type` changed after values exist — the stored rows stay in columns the
  executor no longer reads;
* a field authored on "Capacitors" not being offered on "Capacitors > Ceramic",
  which is the node parts are actually filed under;
* a choice deleted out from under the parts filed under it — a bare
  `IntegrityError`, i.e. a 500 with no number in it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.catalog import Part
from app.models.parameter import ParameterTemplate
from app.scripts.seed_demo import seed_all
from app.services import parameters

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session() -> Session:
    return get_session_factory()()


def _seed() -> None:
    session = _session()
    try:
        seed_all(session)
        session.commit()
    finally:
        session.close()


def _set_numeric(part_id: int, template_name: str, raw: str) -> None:
    """Store a value the only sanctioned way — `app.services.parameters`.

    Nothing in the authoring diff writes `parameter_value`, so a test that wants a
    value in place uses the same funnel every other writer does. That funnel is what
    guarantees `value_min`/`value_max`, which is the whole reason the search below
    can find anything.
    """
    session = _session()
    try:
        part = session.get(Part, part_id)
        assert part is not None
        template = session.execute(
            select(ParameterTemplate).where(ParameterTemplate.name == template_name)
        ).scalar_one()
        parameters.set_numeric(session, part, template, raw)
        session.commit()
    finally:
        session.close()


def _set_choice(part_id: int, template_name: str, key: str) -> None:
    session = _session()
    try:
        part = session.get(Part, part_id)
        assert part is not None
        template = session.execute(
            select(ParameterTemplate).where(ParameterTemplate.name == template_name)
        ).scalar_one()
        parameters.set_choice(session, part, template, key)
        session.commit()
    finally:
        session.close()


def _category(client: TestClient, name: str, slug: str, parent_id: int | None = None) -> int:
    response = client.post(
        "/api/part-categories",
        json={"name": name, "slug": slug, "parent_id": parent_id},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["part_category"]["id"])


def _field(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/parameter-fields", json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()["field"]
    return payload


def _part(client: TestClient, name: str, category_id: int | None = None) -> int:
    response = client.post(
        "/api/parts", json={"name": name, "category_id": category_id, "part_kind": "component"}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["part"]["id"])


def _search(client: TestClient, template: str, value: str, **extra: Any) -> list[str]:
    response = client.post(
        "/api/search/parts",
        json={"filters": [{"template": template, "value": value}], **extra},
    )
    assert response.status_code == 200, response.text
    return [row["name"] for row in response.json()["results"]]


def _field_names(client: TestClient, category: str | None = None) -> list[str]:
    params = {"category": category} if category else {}
    response = client.get("/api/parameter-fields", params=params)
    assert response.status_code == 200, response.text
    return [row["name"] for row in response.json()]


def _facet_names(client: TestClient, category: str | None = None) -> set[str]:
    body = {"category": category} if category else {}
    response = client.post("/api/parameter-templates", json=body)
    assert response.status_code == 200, response.text
    return {row["name"] for row in response.json()["templates"]}


# ---------------------------------------------------------------------------
# The deliverable: author a field, then filter by it
# ---------------------------------------------------------------------------


def test_a_numeric_field_authored_over_the_api_can_be_searched_on(client: TestClient) -> None:
    """The headline. Nothing existed to create a filterable field; now a POST does,
    and the field it creates is a real predicate in the parametric executor."""
    _seed()
    category = _category(client, "Ceramic caps", "ceramic-cap")

    created = _field(
        client,
        name="esr",
        display_name="ESR",
        value_type="numeric",
        base_unit="ohm",
        substitution_direction="lower_ok",
        applies_to_category="ceramic-cap",
        sort_order=70,
    )
    assert created["base_unit"] == "ohm"

    low = _part(client, "Low-ESR cap", category)
    high = _part(client, "Lossy cap", category)
    _set_numeric(low, "esr", "0.05")
    _set_numeric(high, "esr", "2.5")

    # A range query, which only works because the value writer populated both
    # bounds and the field's quantity parses '0R1' at all.
    assert _search(client, "esr", "0.01-0.1") == ["Low-ESR cap"]
    # And the substitution direction is not decoration: `lower_ok` means a part
    # whose whole interval sits below the requirement satisfies it.
    assert _search(client, "esr", "1", mode="substitute") == ["Low-ESR cap"]


def test_a_list_field_is_authored_with_its_options_and_filters_on_them(
    client: TestClient,
) -> None:
    """Authoring a list field is **one** action. Field-then-options would leave a
    window where an enum template exists with no choices — a filter that offers
    nothing and matches nothing while looking like it works."""
    _seed()
    category = _category(client, "Connectors", "connector")

    created = _field(
        client,
        name="gender",
        display_name="Gender",
        value_type="enum",
        substitution_direction="exact",
        applies_to_category="connector",
        choices=[
            {"key": "male", "label": "Male (plug)", "aliases": ["plug", "pin"]},
            {"key": "female", "label": "Female (socket)", "aliases": ["socket", "jack"]},
        ],
    )
    assert [choice["key"] for choice in created["choices"]] == ["male", "female"]

    plug = _part(client, "2.54mm header", category)
    socket = _part(client, "2.54mm socket strip", category)
    _set_choice(plug, "gender", "male")
    _set_choice(socket, "gender", "female")

    assert _search(client, "gender", "female") == ["2.54mm socket strip"]
    # An alias resolves to the same option, so a source that said "socket" and one
    # that said "female" are never two different answers.
    assert _search(client, "gender", "socket") == ["2.54mm socket strip"]


# ---------------------------------------------------------------------------
# Defect 1: a field on a parent category is inherited by its children
# ---------------------------------------------------------------------------


def test_a_field_authored_on_a_parent_category_is_offered_on_the_child(
    client: TestClient,
) -> None:
    """**Fails against the old behaviour.** `applies_to_category` was an exact
    string match, so a field authored on "Capacitors" was invisible under
    "Capacitors > Ceramic" — the node parts are actually filed under. The user sees
    a filter panel missing the field they just made, with nothing to explain it.
    """
    _seed()
    parent = {row["slug"]: row["id"] for row in client.get("/api/part-categories").json()}[
        "capacitor"
    ]
    _category(client, "Ceramic", "ceramic", parent_id=parent)

    _field(
        client,
        name="esr",
        display_name="ESR",
        value_type="numeric",
        base_unit="ohm",
        substitution_direction="lower_ok",
        applies_to_category="capacitor",
    )

    assert "esr" in _field_names(client, "ceramic")
    # And through the facet panel, which is the door the filter UI actually uses.
    assert "esr" in _facet_names(client, "ceramic")
    # Inherited from the parent's own field too, not just the one just added.
    assert "capacitance" in _facet_names(client, "ceramic")


def test_an_inherited_field_says_so(client: TestClient) -> None:
    """Editing it affects every sibling category, so the editor has to be able to
    tell "authored here" from "comes from Passives"."""
    _seed()
    parent = {row["slug"]: row["id"] for row in client.get("/api/part-categories").json()}[
        "capacitor"
    ]
    _category(client, "Ceramic", "ceramic", parent_id=parent)
    _field(
        client,
        name="esr",
        display_name="ESR",
        value_type="numeric",
        base_unit="ohm",
        substitution_direction="lower_ok",
        applies_to_category="ceramic",
    )

    by_name = {
        row["name"]: row for row in client.get("/api/parameter-fields?category=ceramic").json()
    }
    assert by_name["esr"]["inherited"] is False
    assert by_name["capacitance"]["inherited"] is True
    assert by_name["package"]["inherited"] is True  # global, so inherited everywhere


def test_inheritance_does_not_leak_sideways(client: TestClient) -> None:
    """Ancestors only. A sibling's field must stay a sibling's — otherwise the fix
    for the exact-match defect would just be "show everything"."""
    _seed()
    assert "resistance" not in _facet_names(client, "capacitor")
    assert "capacitance" not in _facet_names(client, "resistor")
    # A field authored on the shared ancestor reaches both.
    _field(
        client,
        name="tempco",
        display_name="Temperature coefficient",
        value_type="numeric",
        base_unit="temperature",
        substitution_direction="range_overlap",
        applies_to_category="passive",
    )
    assert "tempco" in _facet_names(client, "capacitor")
    assert "tempco" in _facet_names(client, "resistor")


# ---------------------------------------------------------------------------
# base_unit is validated at authoring time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["ohms", "µF", "F", "banana", "Ω"])
def test_an_unparseable_base_unit_is_refused_immediately(client: TestClient, bad: str) -> None:
    """The single most expensive way this feature could fail. A field whose unit the
    parser does not recognise is creatable, appears in the filter panel, and refuses
    every value anyone enters — so the check has to happen here, not on first use.
    """
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "esr",
            "display_name": "ESR",
            "value_type": "numeric",
            "base_unit": bad,
            "substitution_direction": "lower_ok",
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "unknown_base_unit"
    # Readable, and it names what it could not parse plus what it would accept.
    assert bad in detail["message"]
    assert "ohm" in detail["message"]


def test_a_recognised_spelling_is_stored_canonically(client: TestClient) -> None:
    """'OHM' and the quantity alias 'resistance' are the same thing as 'ohm'. Two
    rows spelling one quantity differently would parse identically and read
    differently in every facet panel and every export."""
    _seed()
    created = _field(
        client,
        name="esr",
        display_name="ESR",
        value_type="numeric",
        base_unit="OHM",
        substitution_direction="lower_ok",
    )
    assert created["base_unit"] == "ohm"


def test_a_numeric_field_with_no_unit_is_refused(client: TestClient) -> None:
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "esr",
            "display_name": "ESR",
            "value_type": "numeric",
            "substitution_direction": "lower_ok",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "missing_base_unit"


def test_a_unit_on_a_list_field_is_refused(client: TestClient) -> None:
    """Not pedantry: a unit on an enum is a sign the author picked the wrong value
    type, and accepting it would leave a template the numeric parser half-believes."""
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "gender",
            "display_name": "Gender",
            "value_type": "enum",
            "base_unit": "ohm",
            "substitution_direction": "exact",
            "choices": [{"key": "male", "label": "Male"}],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unit_on_non_numeric"


def test_the_pickable_units_are_served_rather_than_guessed(client: TestClient) -> None:
    """So the UI offers a select instead of the free-text box that produces 'ohms'."""
    response = client.get("/api/parameter-fields/base-units")
    assert response.status_code == 200, response.text
    by_name = {row["name"]: row["symbol"] for row in response.json()}
    assert by_name["ohm"] == "Ω"
    assert by_name["farad"] == "F"


# ---------------------------------------------------------------------------
# substitution_direction is required
# ---------------------------------------------------------------------------


def test_substitution_direction_is_not_optional(client: TestClient) -> None:
    """It is what makes substitution search correct by construction. Silently
    defaulting a voltage rating to `exact` would mean a 50 V part no longer
    satisfies a 25 V requirement, and nothing would say so."""
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "esr",
            "display_name": "ESR",
            "value_type": "numeric",
            "base_unit": "ohm",
        },
    )
    assert response.status_code == 422
    assert any(
        error["loc"][-1] == "substitution_direction" for error in response.json()["detail"]
    ), response.text


def test_a_list_field_with_no_options_is_refused(client: TestClient) -> None:
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "gender",
            "display_name": "Gender",
            "value_type": "enum",
            "substitution_direction": "exact",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "no_choices"


# ---------------------------------------------------------------------------
# The globally-UNIQUE name, explained rather than dumped
# ---------------------------------------------------------------------------


def test_a_name_collision_is_explained_and_hands_back_the_existing_field(
    client: TestClient,
) -> None:
    """Not a 500 and not an opaque IntegrityError: the collision is nearly always
    "the field you want already exists", so the response has to contain it."""
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "capacitance",
            "display_name": "Capacitance",
            "value_type": "numeric",
            "base_unit": "farad",
            "substitution_direction": "range_overlap",
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "duplicate_name"
    assert detail["existing"]["name"] == "capacitance"
    assert detail["existing"]["base_unit"] == "farad"
    assert "reuse" in detail["message"]


def test_reuse_adopts_the_existing_field_and_adds_only_missing_options(
    client: TestClient,
) -> None:
    """One real-world concept is one field: sharing `voltage_rating` between
    capacitors and inductors is *right*, and it keeps substitution coherent because
    there is one declared direction for the concept."""
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "dielectric",
            "display_name": "Dielectric",
            "value_type": "enum",
            "substitution_direction": "exact",
            "choices": [
                {"key": "X7R", "label": "X7R"},  # already there
                {"key": "X8R", "label": "X8R"},  # new
            ],
            "on_name_conflict": "reuse",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reused"] is True

    keys = [choice["key"] for choice in body["field"]["choices"]]
    assert keys.count("X7R") == 1, "an existing option must not be duplicated"
    assert "X8R" in keys

    # Only one `dielectric` exists — nothing was created alongside it.
    assert _field_names(client).count("dielectric") == 1


def test_reuse_refuses_an_incompatible_existing_field(client: TestClient) -> None:
    """Reusing across a different quantity would file microfarads and millihenries
    in one field, where every stored bound means whichever unit its row was written
    under."""
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "capacitance",
            "display_name": "Capacitance",
            "value_type": "numeric",
            "base_unit": "henry",
            "substitution_direction": "range_overlap",
            "on_name_conflict": "reuse",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "incompatible_existing_field"


def test_namespacing_makes_a_genuinely_separate_field(client: TestClient) -> None:
    """For a collision that is an accident of vocabulary rather than the same
    concept."""
    _seed()
    _category(client, "Enclosures", "enclosure")
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "package",
            "display_name": "Packaging style",
            "value_type": "enum",
            "substitution_direction": "exact",
            "applies_to_category": "enclosure",
            "choices": [{"key": "flatpack", "label": "Flat pack"}],
            "on_name_conflict": "namespace",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["field"]["name"] == "enclosure.package"
    assert response.json()["reused"] is False


def test_namespacing_without_a_category_says_why_it_cannot(client: TestClient) -> None:
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "package",
            "display_name": "Packaging style",
            "value_type": "enum",
            "substitution_direction": "exact",
            "choices": [{"key": "flatpack", "label": "Flat pack"}],
            "on_name_conflict": "namespace",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "namespace_needs_category"


# ---------------------------------------------------------------------------
# Editing a definition parts already depend on
# ---------------------------------------------------------------------------


def _authored_numeric(client: TestClient) -> int:
    created = _field(
        client,
        name="esr",
        display_name="ESR",
        value_type="numeric",
        base_unit="ohm",
        substitution_direction="lower_ok",
    )
    return int(created["id"])


def test_the_value_type_can_change_while_nothing_holds_a_value(client: TestClient) -> None:
    _seed()
    field_id = _authored_numeric(client)
    response = client.patch(
        f"/api/parameter-fields/{field_id}",
        json={"value_type": "enum", "base_unit": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["field"]["value_type"] == "enum"
    assert response.json()["field"]["base_unit"] is None


def test_retyping_away_from_numeric_drops_the_quantity_by_itself(client: TestClient) -> None:
    """A unit left on a non-numeric field is a contradiction the create path
    refuses outright, so a retype must not be able to produce one by omission."""
    _seed()
    field_id = _authored_numeric(client)
    response = client.patch(f"/api/parameter-fields/{field_id}", json={"value_type": "text"})
    assert response.status_code == 200, response.text
    assert response.json()["field"]["base_unit"] is None


def test_the_value_type_cannot_change_once_a_part_holds_a_value(client: TestClient) -> None:
    """A numeric value lives in `value_min`/`value_max`, an enum's in `choice_id`.
    Flipping the type strands every stored row in columns the executor no longer
    reads — they stay in the table, match nothing, and nothing says so."""
    _seed()
    field_id = _authored_numeric(client)
    _set_numeric(_part(client, "Some cap"), "esr", "0.05")

    response = client.patch(
        f"/api/parameter-fields/{field_id}", json={"value_type": "enum", "base_unit": None}
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "value_type_in_use"
    assert "1 part" in detail["message"]


def test_the_quantity_cannot_change_once_a_part_holds_a_value(client: TestClient) -> None:
    """The sharper version of the same failure: the stored bounds were computed
    under the old quantity, so they keep answering range queries in the wrong unit
    while looking authoritative."""
    _seed()
    field_id = _authored_numeric(client)
    _set_numeric(_part(client, "Some cap"), "esr", "0.05")

    response = client.patch(f"/api/parameter-fields/{field_id}", json={"base_unit": "henry"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "base_unit_in_use"


def test_deleting_a_field_parts_use_is_refused_naming_the_count(client: TestClient) -> None:
    """`parameter_value.template_id` is ON DELETE CASCADE, so unguarded this deletes
    every value of the field along with it, silently."""
    _seed()
    field_id = _authored_numeric(client)
    _set_numeric(_part(client, "Some cap"), "esr", "0.05")

    response = client.delete(f"/api/parameter-fields/{field_id}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "field_in_use"
    assert "1 part" in detail["message"]

    response = client.get("/api/parameter-fields")
    assert "esr" in [row["name"] for row in response.json()]


def test_an_unused_field_can_be_deleted(client: TestClient) -> None:
    _seed()
    field_id = _authored_numeric(client)
    assert client.delete(f"/api/parameter-fields/{field_id}").status_code == 200
    assert "esr" not in _field_names(client)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def _authored_enum(client: TestClient) -> int:
    created = _field(
        client,
        name="gender",
        display_name="Gender",
        value_type="enum",
        substitution_direction="exact",
        choices=[{"key": "male", "label": "Male"}, {"key": "female", "label": "Female"}],
    )
    return int(created["id"])


def test_deleting_an_option_parts_are_filed_under_is_refused_with_the_count(
    client: TestClient,
) -> None:
    """`parameter_value.choice_id` is ON DELETE RESTRICT, so the database already
    refuses — as an `IntegrityError`, which reaches the client as a 500 with no
    number in it. "2 parts are filed under 'male'" is the difference."""
    _seed()
    field_id = _authored_enum(client)
    for name in ("Header A", "Header B"):
        _set_choice(_part(client, name), "gender", "male")

    male = next(
        choice
        for choice in client.get(f"/api/parameter-fields/{field_id}").json()["choices"]
        if choice["key"] == "male"
    )
    assert male["use_count"] == 2

    response = client.delete(f"/api/parameter-fields/{field_id}/choices/{male['id']}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "choice_in_use"
    assert "2 parts" in detail["message"]


def test_an_unused_option_can_be_deleted(client: TestClient) -> None:
    _seed()
    field_id = _authored_enum(client)
    choices = client.get(f"/api/parameter-fields/{field_id}").json()["choices"]
    unused = next(choice for choice in choices if choice["key"] == "female")

    assert (
        client.delete(f"/api/parameter-fields/{field_id}/choices/{unused['id']}").status_code == 200
    )
    keys = [
        choice["key"]
        for choice in client.get(f"/api/parameter-fields/{field_id}").json()["choices"]
    ]
    assert keys == ["male"]


def test_an_option_can_be_added_relabelled_reordered_and_aliased(client: TestClient) -> None:
    _seed()
    field_id = _authored_enum(client)

    added = client.post(
        f"/api/parameter-fields/{field_id}/choices",
        json={"key": "hermaphroditic", "label": "Hermaphroditic", "sort_order": 30},
    )
    assert added.status_code == 201, added.text

    choices = {choice["key"]: choice for choice in added.json()["field"]["choices"]}
    edited = client.patch(
        f"/api/parameter-fields/{field_id}/choices/{choices['female']['id']}",
        json={"label": "Female (socket)", "sort_order": 5, "aliases": ["socket", "jack"]},
    )
    assert edited.status_code == 200, edited.text

    after = {choice["key"]: choice for choice in edited.json()["field"]["choices"]}
    assert after["female"]["label"] == "Female (socket)"
    assert after["female"]["aliases"] == ["socket", "jack"]
    # Reordered to the front, which is what sort_order is for.
    assert next(choice["key"] for choice in edited.json()["field"]["choices"]) == "female"

    # And the new alias resolves in search, which is the only reason aliases exist.
    _set_choice(_part(client, "Socket strip"), "gender", "female")
    assert _search(client, "gender", "jack") == ["Socket strip"]


def test_a_duplicate_option_key_is_refused(client: TestClient) -> None:
    _seed()
    field_id = _authored_enum(client)
    response = client.post(
        f"/api/parameter-fields/{field_id}/choices",
        json={"key": "MALE", "label": "Male again"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "duplicate_choice_key"


# ---------------------------------------------------------------------------
# The shared field library
# ---------------------------------------------------------------------------


def _seeded_field_id(client: TestClient, name: str) -> int:
    return next(
        row["id"] for row in client.get("/api/parameter-fields").json() if row["name"] == name
    )


def test_a_shared_field_cannot_be_renamed_or_re_quantified(client: TestClient) -> None:
    """A user renaming `capacitance` breaks every decoder and extractor that names
    it — and unlike a container type, cloning it is the *failure* rather than the
    point: the clone would need a different name, which nothing refers to, while
    both appeared in the panel as two fields meaning one thing."""
    _seed()
    field_id = _seeded_field_id(client, "capacitance")

    renamed = client.patch(f"/api/parameter-fields/{field_id}", json={"name": "cap"})
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()["detail"]["reason"] == "seed_immutable"

    requantified = client.patch(f"/api/parameter-fields/{field_id}", json={"base_unit": "henry"})
    assert requantified.status_code == 409
    assert requantified.json()["detail"]["reason"] == "seed_immutable"

    deleted = client.delete(f"/api/parameter-fields/{field_id}")
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["reason"] == "seed_immutable"


def test_a_shared_field_is_still_relabelled_reordered_and_re_scoped(client: TestClient) -> None:
    """Only the three identity fields are frozen. Everything a user would sensibly
    want to change about a shared field stays changeable, or the guard becomes a
    wall."""
    _seed()
    field_id = _seeded_field_id(client, "capacitance")
    response = client.patch(
        f"/api/parameter-fields/{field_id}",
        json={
            "display_name": "Capacitance (C)",
            "sort_order": 5,
            "plausible_max": 1.0,
            "substitution_direction": "range_overlap",
            "applies_to_category": "passive",
        },
    )
    assert response.status_code == 200, response.text
    field = response.json()["field"]
    assert field["display_name"] == "Capacitance (C)"
    assert field["applies_to_category"] == "passive"
    assert field["plausible_max"] == 1.0


def test_a_user_authored_field_is_never_marked_shared(client: TestClient) -> None:
    _seed()
    assert (
        _field(
            client,
            name="esr",
            display_name="ESR",
            value_type="numeric",
            base_unit="ohm",
            substitution_direction="lower_ok",
        )["is_seed"]
        is False
    )


# ---------------------------------------------------------------------------
# Kinds and categories — the other two halves of "part type"
# ---------------------------------------------------------------------------


def test_a_part_kind_can_be_created_and_then_filtered_on(client: TestClient) -> None:
    """A *kind* is what something fundamentally is, and it is what
    `/api/search/parts?part_kind=` narrows on — so creating one has to actually
    partition the catalogue."""
    _seed()
    response = client.post(
        "/api/part-kinds", json={"slug": "fastener", "display_name": "Fasteners", "sort_order": 40}
    )
    assert response.status_code == 201, response.text
    assert response.json()["part_kind"]["part_count"] == 0

    created = client.post("/api/parts", json={"name": "M3x8 cap screw", "part_kind": "fastener"})
    assert created.status_code == 201, created.text

    found = client.post("/api/search/parts", json={"part_kind": "fastener"}).json()
    assert [row["name"] for row in found["results"]] == ["M3x8 cap screw"]


def test_a_duplicate_part_kind_slug_is_a_409_not_a_500(client: TestClient) -> None:
    _seed()
    response = client.post("/api/part-kinds", json={"slug": "component", "display_name": "Again"})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "duplicate_slug"


def test_a_part_kind_is_renamed_without_its_slug_moving(client: TestClient) -> None:
    """The slug is the search parameter and therefore lives in shared URLs; the
    display name is the one meant for humans."""
    _seed()
    kind_id = next(
        row["id"] for row in client.get("/api/part-kinds").json() if row["slug"] == "component"
    )
    response = client.patch(
        f"/api/part-kinds/{kind_id}", json={"display_name": "Electronic components"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["part_kind"]["display_name"] == "Electronic components"
    assert response.json()["part_kind"]["slug"] == "component"


def test_a_category_is_created_through_the_tree_with_its_paths_cached(
    client: TestClient,
) -> None:
    """Never a hand-written `id_path`: it is what subtree search, the descendant
    counts and field inheritance all read."""
    _seed()
    parent = {row["slug"]: row["id"] for row in client.get("/api/part-categories").json()}[
        "capacitor"
    ]
    response = client.post(
        "/api/part-categories", json={"name": "Ceramic", "slug": "ceramic", "parent_id": parent}
    )
    assert response.status_code == 201, response.text
    body = response.json()["part_category"]
    assert body["depth"] == 2
    assert body["label_path"] == "Passives / Capacitors / Ceramic"


def test_renaming_a_category_refreshes_the_paths_below_it(client: TestClient) -> None:
    _seed()
    by_slug = {row["slug"]: row["id"] for row in client.get("/api/part-categories").json()}
    response = client.patch(f"/api/part-categories/{by_slug['passive']}", json={"name": "Passive"})
    assert response.status_code == 200, response.text

    rail = {row["slug"]: row for row in client.get("/api/part-categories").json()}
    assert rail["capacitor"]["name"] == "Capacitors"
    read = client.get("/api/parameter-fields").status_code
    assert read == 200
    moved = client.post(
        f"/api/part-categories/{by_slug['capacitor']}/move", json={"parent_id": None}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["part_category"]["depth"] == 0
    assert moved.json()["part_category"]["label_path"] == "Capacitors"


def test_moving_a_category_under_its_own_descendant_is_refused(client: TestClient) -> None:
    """A cycle admitted here would make the path-rebuild CTE recurse forever."""
    _seed()
    by_slug = {row["slug"]: row["id"] for row in client.get("/api/part-categories").json()}
    response = client.post(
        f"/api/part-categories/{by_slug['passive']}/move",
        json={"parent_id": by_slug["capacitor"]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "would_create_cycle"


def test_moving_a_category_takes_its_fields_inheritance_with_it(client: TestClient) -> None:
    """Inheritance is computed from the live tree, not cached per template — so a
    reparent has to change which categories offer a field, immediately."""
    _seed()
    by_slug = {row["slug"]: row["id"] for row in client.get("/api/part-categories").json()}
    _field(
        client,
        name="esr",
        display_name="ESR",
        value_type="numeric",
        base_unit="ohm",
        substitution_direction="lower_ok",
        applies_to_category="capacitor",
    )
    _category(client, "Ceramic", "ceramic", parent_id=by_slug["resistor"])
    assert "esr" not in _field_names(client, "ceramic")

    ceramic = {row["slug"]: row["id"] for row in client.get("/api/part-categories").json()}[
        "ceramic"
    ]
    assert (
        client.post(
            f"/api/part-categories/{ceramic}/move", json={"parent_id": by_slug["capacitor"]}
        ).status_code
        == 200
    )
    assert "esr" in _field_names(client, "ceramic")


def test_a_field_may_name_only_a_real_category(client: TestClient) -> None:
    """Otherwise a typo'd slug produces a field that is offered nowhere — the same
    silent-invisibility failure as the unit, one column over."""
    _seed()
    response = client.post(
        "/api/parameter-fields",
        json={
            "name": "esr",
            "display_name": "ESR",
            "value_type": "numeric",
            "base_unit": "ohm",
            "substitution_direction": "lower_ok",
            "applies_to_category": "capacitorz",
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["reason"] == "unknown_category"


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_the_authoring_routes_are_in_the_openapi_document(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/part-kinds",
        "/api/part-categories",
        "/api/parameter-fields",
        "/api/parameter-fields/base-units",
        "/api/parameter-fields/{field_id}",
        "/api/parameter-fields/{field_id}/choices",
        "/api/parameter-fields/{field_id}/choices/{choice_id}",
    ):
        assert path in paths, path
    # The facet reader keeps its path and its operation id, which every generated
    # client already calls — that is why authoring got its own prefix.
    assert paths["/api/parameter-templates"]["post"]["operationId"] == "parameter_facets"


def test_authoring_a_field_is_idempotent_under_a_retried_key(client: TestClient) -> None:
    """A phone on flaky wifi must not end up with two fields, and the name is
    UNIQUE so the second attempt would otherwise 409 on a request that succeeded."""
    _seed()
    body = {
        "name": "esr",
        "display_name": "ESR",
        "value_type": "numeric",
        "base_unit": "ohm",
        "substitution_direction": "lower_ok",
        "client_op_id": "11111111-1111-1111-1111-111111111111",
    }
    first = client.post("/api/parameter-fields", json=body)
    second = client.post("/api/parameter-fields", json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["replayed"] is True
    assert first.json()["field"]["id"] == second.json()["field"]["id"]
    assert _field_names(client).count("esr") == 1
