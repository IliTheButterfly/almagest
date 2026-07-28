"""The search endpoints.

The GET alias exists so a query can be pasted as a URL. That is only safe
because it builds the identical `SearchQuery` as the POST body — which is
asserted here directly rather than assumed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.scripts.seed_demo import seed_catalogue
from tests.factories import make_location, make_lot


def _seed(client: TestClient) -> None:
    from app.db.session import get_session_factory

    session: Session = get_session_factory()()
    try:
        seed_catalogue(session)
        session.commit()
    finally:
        session.close()


def _part(client: TestClient, db: Session, mpn: str) -> Part:
    """The seeded part, through the `db` session — which is the same database
    file the `client` fixture writes to, so a lot added here is visible there."""
    _seed(client)
    return db.execute(select(Part).where(Part.mpn == mpn)).scalar_one()


def _row(client: TestClient, mpn: str) -> dict:
    """One result row, found by MPN rather than by position, so these tests do
    not silently depend on the ordering that other tests here assert."""
    results = client.post("/api/search/parts", json={}).json()["results"]
    matching = [row for row in results if row["mpn"] == mpn]
    assert matching, f"{mpn} not in {[r['mpn'] for r in results]}"
    return matching[0]


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


# ---------------------------------------------------------------------------
# Stock on the row: results are ordered stock-first, so they must say how much
# ---------------------------------------------------------------------------


def test_a_result_row_reports_its_stock(client: TestClient, db: Session) -> None:
    """The gap: results are ordered stock-first but `PartSummary` carried no
    quantity, so the sort had no visible explanation."""
    part = _part(client, db, "DEMO-RES-4K7")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=250_000)
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert row["qty_milli"] == 250_000
    assert row["lot_count"] == 1
    assert row["location_count"] == 1


def test_several_lots_sum_without_duplicating_the_row(client: TestClient, db: Session) -> None:
    """Quantity lives on the lot, so the row has to add them up — and the part
    must still appear exactly once, which is why search uses EXISTS not a JOIN."""
    part = _part(client, db, "DEMO-RES-4K7")
    bin_a = make_location(db, name="Bin A")
    make_lot(db, part, bin_a, qty_milli=500_000)
    make_lot(db, part, bin_a, qty_milli=120_000)  # a cut strip beside the reel
    make_lot(db, part, make_location(db, name="Bin B"), qty_milli=30_000)
    db.commit()

    results = client.post("/api/search/parts", json={"text": "DEMO-RES-4K7"}).json()["results"]
    assert len([r for r in results if r["mpn"] == "DEMO-RES-4K7"]) == 1

    row = _row(client, "DEMO-RES-4K7")
    assert row["qty_milli"] == 650_000
    assert row["lot_count"] == 3
    assert row["location_count"] == 2  # two bins, three lots


def test_a_part_with_no_stock_reports_zero_rather_than_null(client: TestClient) -> None:
    """Zero is a fact worth showing — "you have none of this" is most of what a
    personal inventory is for. A null would render as a blank cell that reads as
    "unknown"."""
    _seed(client)
    row = _row(client, "DEMO-RES-4K7")
    assert row["qty_milli"] == 0
    assert row["lot_count"] == 0
    assert row["location_count"] == 0


def test_an_emptied_lot_stops_counting(client: TestClient, db: Session) -> None:
    """Counted on the same `qty > 0` test as `in_stock_only` and the ordering, so
    a row can never read `0 lots` while sorting as though it were stocked."""
    part = _part(client, db, "DEMO-RES-4K7")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=0)
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert (row["qty_milli"], row["lot_count"]) == (0, 0)
    in_stock = client.post("/api/search/parts", json={"in_stock_only": True}).json()["results"]
    assert "DEMO-RES-4K7" not in {r["mpn"] for r in in_stock}


def test_the_row_quantity_agrees_with_the_ordering(client: TestClient, db: Session) -> None:
    """Stock-first ordering and the displayed quantity read the same column, so
    every stocked row must precede every unstocked one."""
    part = _part(client, db, "DEMO-RES-10K")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=1_000)
    db.commit()

    rows = client.post("/api/search/parts", json={}).json()["results"]
    stocked = [index for index, row in enumerate(rows) if row["qty_milli"] > 0]
    unstocked = [index for index, row in enumerate(rows) if row["qty_milli"] == 0]
    assert stocked and unstocked
    assert max(stocked) < min(unstocked)


def test_the_querystring_alias_reports_stock_too(client: TestClient, db: Session) -> None:
    """Both routes run `_run`, and a pasted URL must not answer differently."""
    part = _part(client, db, "DEMO-RES-4K7")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=42_000)
    db.commit()

    rows = client.get("/api/search/parts", params={"text": "DEMO-RES-4K7"}).json()["results"]
    assert [r["qty_milli"] for r in rows if r["mpn"] == "DEMO-RES-4K7"] == [42_000]
