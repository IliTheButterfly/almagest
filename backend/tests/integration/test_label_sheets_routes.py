"""`/api/labels/sheets`, driven through HTTP.

`tests/integration/test_label_sheets_service.py` covers the rules themselves
against `app.services.labels` directly; what matters here is that the route
wires a request onto them correctly, that a sheet job survives a round trip
through `GET`, and — the one claim worth proving through the *public* API and
nothing lower-level — that a reprint always shows whatever a slot is called
**now**, never what it was called when a client last read it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create_location(client: TestClient, name: str, **extra: object) -> dict:
    response = client.post("/api/locations", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()["location"]


def _cabinet_type(
    client: TestClient,
    slug: str,
    *,
    rows: int = 2,
    cols: int = 2,
    front_width_mm: float | None = 46.0,
    front_height_mm: float | None = 22.0,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
        "slug": slug,
        "display_name": slug,
        "child_layout": "grid",
        "grid_rows": rows,
        "grid_cols": cols,
        "slot_label_scheme": "row_alpha_col_num",
        "front_width_mm": front_width_mm,
        "front_height_mm": front_height_mm,
        **extra,
    }
    response = client.post("/api/container-types", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["container_type"]


def _cabinet(client: TestClient, container_type_id: int, *, parent_id: int | None = None) -> dict:
    if parent_id is None:
        parent_id = _create_location(client, "Room")["id"]
    response = client.post(
        f"/api/locations/{parent_id}/instantiate",
        json={"container_type_id": container_type_id, "count": 1, "naming_pattern": "Cabinet"},
    )
    assert response.status_code == 201, response.text
    return response.json()["locations"][0]


def _layout_slots(client: TestClient, cabinet_id: int) -> list[dict]:
    return client.get(f"/api/locations/{cabinet_id}/layout").json()["slots"]


def _print_sheet(client: TestClient, **body: object) -> dict:
    response = client.post("/api/labels/sheets", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Reading order and the shape of a job
# ---------------------------------------------------------------------------


def test_a_sheet_has_one_card_per_slot_in_reading_order(client: TestClient) -> None:
    container_type = _cabinet_type(client, "routes-drawer-cabinet", rows=2, cols=2)
    cabinet = _cabinet(client, container_type["id"])

    body = _print_sheet(client, template="drawer_card", root_location_id=cabinet["id"])
    job = body["job"]

    assert job["item_count"] == 4
    assert [(c["row"], c["col"]) for c in job["cards"]] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert [c["slot_label"] for c in job["cards"]] == ["A1", "A2", "B1", "B2"]
    assert job["template"] == "drawer_card"
    assert job["backend"] == "pdf_sheet"
    assert job["card_width_mm"] == 40.0
    assert job["card_height_mm"] == 18.0


def test_get_reads_back_exactly_what_the_post_returned(client: TestClient) -> None:
    container_type = _cabinet_type(client, "routes-get-cabinet", rows=1, cols=2)
    cabinet = _cabinet(client, container_type["id"])

    created = _print_sheet(client, template="drawer_card", root_location_id=cabinet["id"])
    job_id = created["job"]["id"]

    fetched = client.get(f"/api/labels/sheets/{job_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == created["job"]


def test_unknown_job_id_is_404(client: TestClient) -> None:
    response = client.get("/api/labels/sheets/999999")
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["reason"] == "unknown_job"


def test_unknown_root_location_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/labels/sheets", json={"template": "drawer_card", "root_location_id": 999999}
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["reason"] == "unknown_location"


def test_a_container_type_with_no_front_dimensions_is_a_422(client: TestClient) -> None:
    container_type = _cabinet_type(
        client, "routes-no-front", rows=1, cols=1, front_width_mm=None, front_height_mm=None
    )
    cabinet = _cabinet(client, container_type["id"])

    response = client.post(
        "/api/labels/sheets", json={"template": "drawer_card", "root_location_id": cabinet["id"]}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "missing_front_dimensions"


# ---------------------------------------------------------------------------
# slot_ids: a partial reprint lands at the right cell
# ---------------------------------------------------------------------------


def test_slot_ids_filter_positions_one_card_at_its_real_cell(client: TestClient) -> None:
    container_type = _cabinet_type(client, "routes-slot-filter", rows=2, cols=2)
    cabinet = _cabinet(client, container_type["id"])
    b1 = next(s for s in _layout_slots(client, cabinet["id"]) if s["slot_label"] == "B1")

    body = _print_sheet(
        client,
        template="drawer_card",
        root_location_id=cabinet["id"],
        slot_ids=[b1["location_id"]],
        backend="file",
    )
    job = body["job"]

    assert job["item_count"] == 1
    card = job["cards"][0]
    assert card["location_id"] == b1["location_id"]
    assert (card["row"], card["col"]) == (1, 0)


def test_slot_ids_for_a_cabinet_card_is_a_422(client: TestClient) -> None:
    container_type = _cabinet_type(client, "routes-cabinet-card-filter", rows=1, cols=1)
    cabinet = _cabinet(client, container_type["id"])
    slot = _layout_slots(client, cabinet["id"])[0]

    response = client.post(
        "/api/labels/sheets",
        json={
            "template": "cabinet_card",
            "root_location_id": cabinet["id"],
            "slot_ids": [slot["location_id"]],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "slot_ids_not_applicable"


# ---------------------------------------------------------------------------
# QR inclusion follows card geometry through the whole route
# ---------------------------------------------------------------------------


def test_qr_included_flag_follows_card_geometry_through_the_route(client: TestClient) -> None:
    #: 40x12 mm after the lip margin — PLAN.md's own "no room for a QR" case.
    tiny = _cabinet_type(
        client, "routes-tiny-slot", rows=1, cols=1, front_width_mm=46.0, front_height_mm=16.0
    )
    tiny_cabinet = _cabinet(client, tiny["id"])
    tiny_job = _print_sheet(client, template="drawer_card", root_location_id=tiny_cabinet["id"])
    assert tiny_job["job"]["cards"][0]["qr_included"] is False

    roomy = _cabinet_type(
        client, "routes-roomy-slot", rows=1, cols=1, front_width_mm=87.0, front_height_mm=24.0
    )
    roomy_cabinet = _cabinet(client, roomy["id"])
    roomy_job = _print_sheet(client, template="drawer_card", root_location_id=roomy_cabinet["id"])
    assert roomy_job["job"]["cards"][0]["qr_included"] is True


# ---------------------------------------------------------------------------
# Cabinet cards
# ---------------------------------------------------------------------------


def test_cabinet_card_prints_exactly_one_card_for_the_root(client: TestClient) -> None:
    container_type = _cabinet_type(client, "routes-cabinet-card", rows=2, cols=2)
    cabinet = _cabinet(client, container_type["id"])

    body = _print_sheet(client, template="cabinet_card", root_location_id=cabinet["id"])
    job = body["job"]

    assert job["item_count"] == 1
    assert job["cards"][0]["location_id"] == cabinet["id"]
    assert job["cards"][0]["name"] == cabinet["name"]


# ---------------------------------------------------------------------------
# The server never trusts stale data: a relabel between two prints shows up
# ---------------------------------------------------------------------------


def test_a_reprint_shows_the_current_slot_label_not_the_original(client: TestClient) -> None:
    """The only field on `LabelSheetRequest` is `root_location_id`/`slot_ids`
    — there is nowhere on the wire for a stale name to travel through, and
    this is the proof: relabel a slot via the *public* API, reprint just that
    slot, and the new card shows the new name.
    """
    container_type = _cabinet_type(client, "routes-relabel", rows=1, cols=1)
    cabinet = _cabinet(client, container_type["id"])
    slot = _layout_slots(client, cabinet["id"])[0]
    assert slot["slot_label"] == "A1"

    first = _print_sheet(client, template="drawer_card", root_location_id=cabinet["id"])
    assert first["job"]["cards"][0]["slot_label"] == "A1"

    relabel = client.post(
        f"/api/locations/{cabinet['id']}/reapply-layout",
        json={
            "slots": [
                {
                    "row_idx": slot["row_idx"],
                    "col_idx": slot["col_idx"],
                    "row_span": slot["row_span"],
                    "col_span": slot["col_span"],
                    "slot_label": "Resistors 220R",
                }
            ]
        },
    )
    assert relabel.status_code == 200, relabel.text

    second = _print_sheet(client, template="drawer_card", root_location_id=cabinet["id"])
    assert second["job"]["cards"][0]["slot_label"] == "Resistors 220R"
    assert second["job"]["cards"][0]["location_id"] == first["job"]["cards"][0]["location_id"]


# ---------------------------------------------------------------------------
# Records: locations.last_printed_at, and idempotent replay
# ---------------------------------------------------------------------------


def test_printing_sets_last_printed_at(client: TestClient) -> None:
    container_type = _cabinet_type(client, "routes-last-printed", rows=1, cols=1)
    cabinet = _cabinet(client, container_type["id"])
    before = client.get(f"/api/locations/{cabinet['id']}").json()
    assert before["last_printed_at"] is None

    _print_sheet(client, template="cabinet_card", root_location_id=cabinet["id"])

    after = client.get(f"/api/locations/{cabinet['id']}").json()
    assert after["last_printed_at"] is not None


def test_replaying_the_same_client_op_id_does_not_render_a_second_job(client: TestClient) -> None:
    container_type = _cabinet_type(client, "routes-idempotent", rows=1, cols=1)
    cabinet = _cabinet(client, container_type["id"])
    body = {
        "template": "cabinet_card",
        "root_location_id": cabinet["id"],
        "client_op_id": "print-once-1",
    }

    first = _print_sheet(client, **body)
    second = _print_sheet(client, **body)

    assert first["job"]["id"] == second["job"]["id"]
    assert first["replayed"] is False
    assert second["replayed"] is True
