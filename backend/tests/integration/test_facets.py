"""Facets — the data a DigiKey-style filter panel is built from.

The assertion that matters throughout is that **a facet count agrees with what
selecting that facet actually returns.** A panel whose numbers disagree with its
own results is worse than no panel: it teaches the user that the counts are
decorative, and then they stop reading them.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.scripts.seed_demo import seed_all


def _seed() -> None:
    session: Session = get_session_factory()()
    try:
        seed_all(session)
        session.commit()
    finally:
        session.close()


def _facets(client: TestClient, **body: object) -> dict:
    response = client.post("/api/parameter-templates", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _template(payload: dict, name: str) -> dict:
    found = [t for t in payload["templates"] if t["name"] == name]
    assert found, f"{name} not in {[t['name'] for t in payload['templates']]}"
    return found[0]


def _count_via_search(client: TestClient, template: str, value: str, **extra: object) -> int:
    response = client.post(
        "/api/search/parts",
        json={"filters": [{"template": template, "value": value}], **extra},
    )
    assert response.status_code == 200, response.text
    return int(response.json()["total"])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_templates_are_enumerable_at_all(client: TestClient) -> None:
    """The gap this closes: search accepted a template *name* but nothing told a
    client which names existed, so the filter UI had to hardcode a list."""
    _seed()
    payload = _facets(client)

    names = {t["name"] for t in payload["templates"]}
    assert {"capacitance", "resistance", "mounting_type", "package"} <= names


def test_a_template_reports_its_substitution_direction(client: TestClient) -> None:
    """So the UI can explain *why* a 50 V part satisfies a 25 V requirement
    rather than presenting substitution as magic."""
    _seed()
    payload = _facets(client)
    assert _template(payload, "voltage_rating")["substitution_direction"] == "higher_ok"
    assert _template(payload, "capacitance")["substitution_direction"] == "range_overlap"


def test_enum_templates_carry_their_choices(client: TestClient) -> None:
    _seed()
    mounting = _template(_facets(client), "mounting_type")
    keys = {c["key"] for c in mounting["choices"]}
    assert keys == {"THT", "SMD"}


def test_numeric_templates_carry_bounds_for_a_slider(client: TestClient) -> None:
    _seed()
    capacitance = _template(_facets(client), "capacitance")

    assert capacitance["numeric_range"] is not None
    assert capacitance["numeric_range"]["min"] <= 22e-6 <= capacitance["numeric_range"]["max"]
    # Labelled with the symbol the part detail screen shows, not 'farad'.
    assert capacitance["numeric_range"]["unit_symbol"] == "F"


def test_a_numeric_template_nobody_has_filled_in_has_no_range(client: TestClient) -> None:
    """Absent rather than a bogus 0-0, which a slider would render as a dead control."""
    _seed()
    assert _template(_facets(client), "inductance")["numeric_range"] is None


# ---------------------------------------------------------------------------
# Counts must agree with the results
# ---------------------------------------------------------------------------


def test_a_choice_count_equals_what_selecting_it_returns(client: TestClient) -> None:
    """The load-bearing property. A panel whose numbers disagree with its own
    results teaches the user that the counts are decorative."""
    _seed()
    payload = _facets(client)

    for template in payload["templates"]:
        for choice in template["choices"]:
            if choice["count"] == 0:
                continue
            assert choice["count"] == _count_via_search(client, template["name"], choice["key"]), (
                f"{template['name']}={choice['key']}"
            )


def test_zero_is_reported_rather_than_hidden(client: TestClient) -> None:
    """Zero is the most useful count in a personal inventory — it tells the user
    not to click. Hiding it would make an empty result feel like a bug."""
    _seed()
    technology = _template(_facets(client), "capacitor_technology")

    counts = {c["key"]: c["count"] for c in technology["choices"]}
    assert counts["ceramic"] == 2
    assert counts["tantalum"] == 0
    assert "tantalum" in counts  # present, not omitted


def test_counts_narrow_as_filters_are_applied(client: TestClient) -> None:
    """Counts answer "what can I narrow to *from here*", not "what exists in the
    catalogue" — otherwise every click is a guess."""
    _seed()
    unfiltered = _template(_facets(client), "capacitor_technology")
    assert {c["key"]: c["count"] for c in unfiltered["choices"]}["electrolytic"] == 1

    # Restrict to ceramics; electrolytic must now read zero.
    filtered = _template(
        _facets(client, filters=[{"template": "capacitor_technology", "value": "ceramic"}]),
        "capacitor_technology",
    )
    assert {c["key"]: c["count"] for c in filtered["choices"]}["electrolytic"] == 0


def test_the_total_matches_the_search_total(client: TestClient) -> None:
    _seed()
    payload = _facets(client, category="capacitor")
    search_total = client.post("/api/search/parts", json={"category": "capacitor"}).json()["total"]
    assert payload["total"] == search_total == 3


def test_a_numeric_range_narrows_with_the_filters(client: TestClient) -> None:
    _seed()
    wide = _template(_facets(client), "voltage_rating")["numeric_range"]
    narrow = _template(
        _facets(client, filters=[{"template": "capacitor_technology", "value": "ceramic"}]),
        "voltage_rating",
    )["numeric_range"]

    assert wide is not None and narrow is not None
    assert narrow["max"] <= wide["max"]


# ---------------------------------------------------------------------------
# Scoping and errors
# ---------------------------------------------------------------------------


def test_category_scoping_keeps_globally_applicable_templates(client: TestClient) -> None:
    """`applies_to_category` is advisory. Filtering strictly on it would hide
    `package` and `mounting_type` from every category, which is most of what you
    want to filter on."""
    _seed()
    names = {t["name"] for t in _facets(client, category="capacitor")["templates"]}

    assert "capacitance" in names  # category-specific
    assert "mounting_type" in names  # global
    assert "resistance" not in names  # another category's


def test_an_unknown_category_is_404(client: TestClient) -> None:
    _seed()
    assert (
        client.post("/api/parameter-templates", json={"category": "nonexistent"}).status_code == 404
    )


def test_an_uninterpretable_filter_value_is_422_with_a_reason(client: TestClient) -> None:
    """Same contract as search: the reason code lets the UI say something better
    than "invalid input"."""
    _seed()
    response = client.post(
        "/api/parameter-templates",
        json={"filters": [{"template": "capacitance", "value": "1M"}]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "implausible"


def test_an_unknown_template_in_a_filter_is_400(client: TestClient) -> None:
    _seed()
    assert (
        client.post(
            "/api/parameter-templates",
            json={"filters": [{"template": "shoe_size", "value": "44"}]},
        ).status_code
        == 400
    )


def test_facets_work_on_an_empty_database(client: TestClient) -> None:
    """No seed at all: the panel must render, with every count zero, rather than
    erroring on a fresh install."""
    payload = _facets(client)
    assert payload["total"] == 0
    assert payload["templates"] == []


# ---------------------------------------------------------------------------
# The category rail
# ---------------------------------------------------------------------------


def test_categories_are_listed_as_a_tree(client: TestClient) -> None:
    _seed()
    response = client.get("/api/part-categories")
    assert response.status_code == 200

    by_slug = {c["slug"]: c for c in response.json()}
    assert by_slug["resistor"]["parent_slug"] == "passive"
    assert by_slug["passive"]["parent_slug"] is None
    assert by_slug["resistor"]["depth"] == 1


def test_a_category_count_includes_descendants(client: TestClient) -> None:
    """Clicking "Passives" must not report fewer parts than it then returns —
    search includes descendants, so the count has to as well."""
    _seed()
    by_slug = {c["slug"]: c for c in client.get("/api/part-categories").json()}

    assert by_slug["passive"]["part_count"] == 5
    assert by_slug["capacitor"]["part_count"] == 3
    assert by_slug["resistor"]["part_count"] == 2

    for slug in ("passive", "capacitor", "resistor"):
        searched = client.post("/api/search/parts", json={"category": slug}).json()["total"]
        assert by_slug[slug]["part_count"] == searched, slug


def test_an_empty_category_reports_zero_rather_than_being_omitted(client: TestClient) -> None:
    _seed()
    by_slug = {c["slug"]: c for c in client.get("/api/part-categories").json()}
    assert by_slug["diode"]["part_count"] == 0


def test_the_facet_routes_are_in_the_openapi_document(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/parameter-templates" in paths
    assert "/api/part-categories" in paths
