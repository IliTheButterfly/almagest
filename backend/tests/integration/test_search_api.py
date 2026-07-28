"""The search endpoints.

The GET alias exists so a query can be pasted as a URL. That is only safe
because it builds the identical `SearchQuery` as the POST body — which is
asserted here directly rather than assumed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.scripts.seed_demo import seed_catalogue


def _seed(client: TestClient) -> None:
    from app.db.session import get_session_factory

    session: Session = get_session_factory()()
    try:
        seed_catalogue(session)
        session.commit()
    finally:
        session.close()


def test_post_search_worked_example(client: TestClient) -> None:
    _seed(client)
    response = client.post(
        "/api/search/parts",
        json={
            "category": "capacitor",
            "filters": [
                {"template": "mounting_type", "value": "THT"},
                {"template": "capacitance", "value": "20-30uF"},
                {"template": "capacitor_technology", "value": "ceramic"},
            ],
        },
    )
    assert response.status_code == 200

    body = response.json()
    assert body["total"] == 1
    assert body["results"][0]["mpn"] == "DEMO-CAP-THT-22U"


def test_get_and_post_agree(client: TestClient) -> None:
    """A pasted URL and an API call must mean the same thing."""
    _seed(client)
    posted = client.post(
        "/api/search/parts",
        json={
            "category": "capacitor",
            "filters": [
                {"template": "mounting_type", "value": "THT"},
                {"template": "capacitance", "value": "20-30uF"},
            ],
        },
    ).json()

    fetched = client.get(
        "/api/search/parts",
        params=[
            ("category", "capacitor"),
            ("f", "mounting_type:THT"),
            ("f", "capacitance:20-30uF"),
        ],
    ).json()

    assert posted == fetched


def test_total_ignores_pagination(client: TestClient) -> None:
    _seed(client)
    body = client.post("/api/search/parts", json={"limit": 2}).json()
    assert body["total"] == 5
    assert len(body["results"]) == 2


def test_an_uninterpretable_value_is_422_with_a_reason(client: TestClient) -> None:
    """Well-formed request, uninterpretable value. The reason code is what lets
    the UI say something better than "invalid input"."""
    _seed(client)
    response = client.post(
        "/api/search/parts",
        json={"filters": [{"template": "capacitance", "value": "1M"}]},
    )
    assert response.status_code == 422

    detail = response.json()["detail"]
    assert detail["template"] == "capacitance"
    assert detail["reason"] == "implausible"


def test_an_unknown_template_is_400(client: TestClient) -> None:
    _seed(client)
    response = client.post(
        "/api/search/parts", json={"filters": [{"template": "shoe_size", "value": "44"}]}
    )
    assert response.status_code == 400


def test_a_malformed_querystring_filter_is_400(client: TestClient) -> None:
    _seed(client)
    assert client.get("/api/search/parts", params={"f": "capacitance"}).status_code == 400


def test_substitute_mode_over_the_api(client: TestClient) -> None:
    _seed(client)
    body = client.get(
        "/api/search/parts", params=[("f", "voltage_rating:25V"), ("mode", "substitute")]
    ).json()

    mpns = {row["mpn"] for row in body["results"]}
    assert "DEMO-CAP-THT-22U-ELEC" in mpns  # 50 V satisfies a 25 V requirement
    assert "DEMO-CAP-SMD-22U" not in mpns  # 16 V does not


def test_search_routes_are_in_the_openapi_document(client: TestClient) -> None:
    """The frontend client is generated from this."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/search/parts" in paths
    assert {"get", "post"} <= set(paths["/api/search/parts"])
