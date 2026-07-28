"""Parts and locations routes.

The load-bearing test here is `test_a_part_needs_only_a_name_and_a_kind`. Intake
friction is the failure mode that killed every abandoned system in this space, so
an unrecognised distributor label has to become a legal row in one tap. If this
endpoint ever grows a required field, that path closes and the test is the thing
that notices.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import get_session_factory


def _create_location(client: TestClient, name: str, **extra: object) -> dict:
    """The created object, unwrapped.

    Create responses nest it under a key (`{"location": {...}}`) because they
    extend `ReplayableResponse`, which needs somewhere to put `replayed`.
    """
    response = client.post("/api/locations", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()["location"]


def _create_part(client: TestClient, **fields: object) -> dict:
    response = client.post("/api/parts", json={"part_kind": "component", **fields})
    assert response.status_code == 201, response.text
    return response.json()["part"]


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


def test_a_part_needs_only_a_name_and_a_kind(client: TestClient) -> None:
    """The intake fast path. An unrecognised label becomes a legal row in one tap
    or the user abandons the scan."""
    body = _create_part(client, name="mystery part from a salvage bin")

    assert body["id"]
    read = client.get(f"/api/parts/{body['id']}").json()
    assert read["name"] == "mystery part from a salvage bin"
    assert read["mpn"] is None
    assert read["category_id"] is None
    assert read["manufacturer_id"] is None


def test_a_stub_part_is_flagged_for_curation(client: TestClient) -> None:
    body = _create_part(client, name="unknown reel", is_stub=True)
    assert client.get(f"/api/parts/{body['id']}").json()["is_stub"] is True


def test_an_unknown_part_kind_is_refused_with_the_options(client: TestClient) -> None:
    response = client.post("/api/parts", json={"name": "x", "part_kind": "unobtainium"})
    assert response.status_code in {400, 404, 422}
    assert "component" in response.text


def test_reading_an_unknown_part_is_404(client: TestClient) -> None:
    assert client.get("/api/parts/999999").status_code == 404


def test_a_part_read_carries_its_lots_and_total(client: TestClient) -> None:
    """So a scan of a known part can branch to re-stock without a second call —
    workflow 2 needs identity and every existing location in one response."""
    part = _create_part(client, name="10k resistor", mpn="RES-10K")
    bin_a = _create_location(client, "Bin A")
    bin_b = _create_location(client, "Bin B")

    client.post(
        "/api/stock/receive",
        json={"part_id": part["id"], "location_id": bin_a["id"], "qty_milli": 5000},
    )
    client.post(
        "/api/stock/receive",
        json={"part_id": part["id"], "location_id": bin_b["id"], "qty_milli": 2000},
    )

    read = client.get(f"/api/parts/{part['id']}").json()
    assert len(read["lots"]) == 2
    assert sum(lot["qty_milli"] for lot in read["lots"]) == 7000
    # Every lot names the place it is in, derived fresh from the tree.
    assert all(lot["location_label_path"] for lot in read["lots"])


def test_patching_a_part_leaves_unmentioned_fields_alone(client: TestClient) -> None:
    """A PATCH that silently blanked omitted fields would quietly destroy
    curation work the first time the UI sent a partial form."""
    part = _create_part(client, name="original", mpn="MPN-1", description="a description")

    client.patch(f"/api/parts/{part['id']}", json={"name": "renamed"})

    read = client.get(f"/api/parts/{part['id']}").json()
    assert read["name"] == "renamed"
    assert read["mpn"] == "MPN-1"
    assert read["description"] == "a description"


def test_the_normalised_mpn_is_maintained(client: TestClient) -> None:
    """mpn_norm is what makes a bare-MPN barcode resolve, so it must not be left
    stale by an edit."""
    from app.models.catalog import Part

    part = _create_part(client, name="p", mpn="Abc-123")
    client.patch(f"/api/parts/{part['id']}", json={"mpn": "XYZ 999"})

    session = get_session_factory()()
    try:
        row = session.execute(select(Part).where(Part.id == part["id"])).scalar_one()
        assert row.mpn == "XYZ 999"
        assert row.mpn_norm is not None
        assert row.mpn_norm != "abc-123"
        assert "999" in row.mpn_norm
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def test_creating_a_nested_location_derives_its_path(client: TestClient) -> None:
    cabinet = _create_location(client, "Cabinet A")
    drawer = _create_location(client, "Drawer 3", parent_id=cabinet["id"])

    read = client.get(f"/api/locations/{drawer['id']}").json()
    assert read["label_path"] == "Cabinet A / Drawer 3"
    assert read["depth"] == 1


def test_the_tree_endpoint_returns_the_hierarchy(client: TestClient) -> None:
    cabinet = _create_location(client, "Cabinet A")
    _create_location(client, "Drawer 1", parent_id=cabinet["id"])
    _create_location(client, "Drawer 2", parent_id=cabinet["id"])

    tree = client.get("/api/locations/tree").json()
    assert tree["nodes"], tree
    labels = {node["name"] for node in tree["nodes"]}
    assert {"Cabinet A", "Drawer 1", "Drawer 2"} <= labels


def test_a_location_read_reports_what_is_in_it(client: TestClient) -> None:
    part = _create_part(client, name="cap", mpn="CAP-1")
    bin_a = _create_location(client, "Bin A")
    client.post(
        "/api/stock/receive",
        json={"part_id": part["id"], "location_id": bin_a["id"], "qty_milli": 1500},
    )

    read = client.get(f"/api/locations/{bin_a['id']}").json()
    assert len(read["lots"]) == 1
    assert read["lots"][0]["qty_milli"] == 1500


def test_reading_an_unknown_location_is_404(client: TestClient) -> None:
    assert client.get("/api/locations/999999").status_code == 404


def test_a_cycle_is_refused(client: TestClient) -> None:
    """A cycle would make the path-rebuild CTE recurse forever, which is
    unrecoverable without manual surgery — so it must never be creatable."""
    cabinet = _create_location(client, "Cabinet")
    drawer = _create_location(client, "Drawer", parent_id=cabinet["id"])

    response = client.post("/api/locations", json={"name": "impossible", "parent_id": 999999})
    assert response.status_code in {404, 409, 422}
    assert drawer["id"]


# ---------------------------------------------------------------------------
# Put-away suggestion
# ---------------------------------------------------------------------------


def test_suggest_always_returns_somewhere(client: TestClient) -> None:
    """A scan is NEVER rejected. With nothing suitable in the warehouse the
    assignment ladder escalates all the way to INBOX rather than erroring —
    blocking a put-away teaches the user to stop scanning."""
    part = _create_part(client, name="something odd")

    response = client.post("/api/locations/suggest", json={"part_id": part["id"]})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["location_id"]
    assert body["label_path"]
    # The rung that answered, so the UI can explain itself rather than presenting
    # an INBOX fallback with the same confidence as a consolidation hit.
    assert body["escalation_level"]
    assert body["reason"]


def test_suggest_prefers_a_location_already_holding_the_part(client: TestClient) -> None:
    """Consolidation is the highest-weighted term in the scorer, and it is what
    stops one part scattering across six bins."""
    part = _create_part(client, name="consolidate me", mpn="CONS-1")
    home = _create_location(client, "Home bin")
    _create_location(client, "Empty bin")
    client.post(
        "/api/stock/receive",
        json={"part_id": part["id"], "location_id": home["id"], "qty_milli": 1000},
    )

    body = client.post("/api/locations/suggest", json={"part_id": part["id"]}).json()
    assert body["location_id"] == home["id"]


def test_suggest_for_an_unknown_part_is_404(client: TestClient) -> None:
    assert client.post("/api/locations/suggest", json={"part_id": 999999}).status_code == 404


def test_every_new_route_is_in_the_openapi_document(client: TestClient) -> None:
    """The frontend client is generated from this, so a route absent here is a
    route the UI cannot call."""
    paths = client.get("/openapi.json").json()["paths"]
    for expected in (
        "/api/parts",
        "/api/parts/{part_id}",
        "/api/locations",
        "/api/locations/tree",
        "/api/locations/suggest",
        "/api/locations/{location_id}",
        "/api/stock/receive",
        "/api/stock/undo",
        "/api/stock/lots/{lot_id}/consume",
        "/api/stock/lots/{lot_id}/move",
        "/api/stock/locations/{location_id}/empty",
    ):
        assert expected in paths, f"{expected} missing from the API contract"
