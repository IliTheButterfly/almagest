"""The search endpoints.

The GET alias exists so a query can be pasted as a URL. That is only safe
because it builds the identical `SearchQuery` as the POST body — which is
asserted here directly rather than assumed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.search import LOCATIONS_PER_RESULT
from app.models.catalog import Part
from app.models.storage import Location
from app.scripts.seed_demo import seed_catalogue
from app.services.tree import TreeRepository
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


# ---------------------------------------------------------------------------
# *Which* bins, not merely how many — the row that can be acted on
# ---------------------------------------------------------------------------


def test_a_result_row_names_the_container_it_is_in(client: TestClient, db: Session) -> None:
    """The gap this closes: the row could say "in 2 bins" and never which two, so
    finding out where anything was meant opening the part."""
    part = _part(client, db, "DEMO-RES-4K7")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=250_000)
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert [place["label_path"] for place in row["locations"]] == ["Bin A"]
    assert row["locations"][0]["qty_milli"] == 250_000


def test_the_fullest_container_comes_first(client: TestClient, db: Session) -> None:
    """The row leads with the bin worth walking to, and the PWA draws the walk to
    exactly this one — so an arbitrary order would send somebody to the drawer
    holding three of them instead of the reel."""
    part = _part(client, db, "DEMO-RES-4K7")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=30_000)
    make_lot(db, part, make_location(db, name="Bin B"), qty_milli=500_000)
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert [place["label_path"] for place in row["locations"]] == ["Bin B", "Bin A"]


def test_lots_in_one_container_are_summed_into_a_single_entry(
    client: TestClient, db: Session
) -> None:
    """A reel and a cut strip in the same bin are two lots and one place to walk
    to. Listing the bin twice would read as two drawers."""
    part = _part(client, db, "DEMO-RES-4K7")
    bin_a = make_location(db, name="Bin A")
    make_lot(db, part, bin_a, qty_milli=500_000)
    make_lot(db, part, bin_a, qty_milli=120_000)
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert len(row["locations"]) == 1
    assert row["locations"][0]["qty_milli"] == 620_000
    assert row["lot_count"] == 2  # still two lots, one container


def test_the_named_list_is_capped_but_the_count_is_not(client: TestClient, db: Session) -> None:
    """The row names a few for recognition; `location_count` stays the truth, so
    the UI can say "and 2 more" instead of implying three is all there is."""
    part = _part(client, db, "DEMO-RES-4K7")
    for index in range(5):
        make_lot(db, part, make_location(db, name=f"Bin {index}"), qty_milli=(index + 1) * 1_000)
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert len(row["locations"]) == LOCATIONS_PER_RESULT
    assert row["location_count"] == 5
    # Fullest first, so the cap keeps the bins worth walking to rather than the
    # five that happened to be created first.
    assert [place["label_path"] for place in row["locations"]] == ["Bin 4", "Bin 3", "Bin 2"]


def test_an_emptied_container_is_not_named(client: TestClient, db: Session) -> None:
    """Same `qty > 0` test as the counts and the ordering: a row must never name a
    bin it is not in while reporting itself out of stock."""
    part = _part(client, db, "DEMO-RES-4K7")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=0)
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert row["locations"] == []
    assert row["location_count"] == 0


def test_the_named_container_is_the_full_derived_path(client: TestClient, db: Session) -> None:
    """Not the bare container name: "01" is the same in every cabinet, and the
    row exists to be recognised. `label_path` is the derived cache, so this also
    pins that search reads it rather than re-deriving a path of its own."""
    part = _part(client, db, "DEMO-RES-4K7")
    cabinet = make_location(db, name="Workbench cabinet")
    drawer = make_location(db, name="01", parent_id=cabinet.id)
    make_lot(db, part, drawer, qty_milli=250_000)
    db.flush()
    TreeRepository(db, Location).rebuild_paths()
    db.commit()

    row = _row(client, "DEMO-RES-4K7")
    assert [place["label_path"] for place in row["locations"]] == ["Workbench cabinet / 01"]


def test_a_container_whose_path_cache_is_empty_still_names_itself(
    client: TestClient, db: Session
) -> None:
    """`label_path` is reconstructible from `parent_id`, so a stale cache is never
    data loss — and must not look like it either. A blank chip in a result list
    would read as a container with no name."""
    part = _part(client, db, "DEMO-RES-4K7")
    make_lot(db, part, make_location(db, name="Bin A"), qty_milli=1_000)
    db.commit()  # deliberately no `rebuild_paths`

    row = _row(client, "DEMO-RES-4K7")
    assert row["locations"][0]["label_path"] == "Bin A"
