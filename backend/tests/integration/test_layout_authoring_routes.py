"""`/api/container-types` and the three `/api/locations` routes that build on
it: `instantiate`, `reapply-layout`, `layout`.

Driven through HTTP rather than the service layer directly —
`tests/integration/test_layout_authoring_service.py` already covers the rules
themselves; what matters here is that the routes wire requests onto them
correctly and turn the service's exceptions into the right status codes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

#: Comfortably past SQLite's signed 64-bit maximum — see
#: `tests/integration/test_numeric_bounds.py`, which this file's bound checks
#: mirror for the layout-authoring routes specifically.
ABSURD = 10**30


def _create_location(client: TestClient, name: str, **extra: object) -> dict:
    response = client.post("/api/locations", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()["location"]


def _create_type(client: TestClient, slug: str, **extra: object) -> dict:
    response = client.post(
        "/api/container-types", json={"slug": slug, "display_name": slug, **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()["container_type"]


# ---------------------------------------------------------------------------
# CRUD, clone, and the seed library
# ---------------------------------------------------------------------------


def test_create_read_and_list_a_container_type(client: TestClient) -> None:
    created = _create_type(client, "test-cabinet", grid_rows=2, grid_cols=3)
    assert created["grid_rows"] == 2
    assert created["materialize_slots"] is False
    assert created["is_seed"] is False

    read = client.get(f"/api/container-types/{created['id']}").json()
    assert read == created

    listing = client.get("/api/container-types").json()
    assert any(row["slug"] == "test-cabinet" for row in listing)


def test_the_seed_library_is_present_and_read_only_by_slug(client: TestClient) -> None:
    seeds = client.get("/api/container-types", params={"is_seed": True}).json()
    slugs = {row["slug"] for row in seeds}
    assert "gridfinity-baseplate-4x4" in slugs
    assert "akro-mils-10144" in slugs
    assert "raaco-c8-30" in slugs
    assert all(row["is_seed"] for row in seeds)


def test_patching_a_seed_type_clones_it_instead_of_mutating_it(client: TestClient) -> None:
    seeds = client.get("/api/container-types", params={"is_seed": True}).json()
    seed = next(row for row in seeds if row["slug"] == "gridfinity-baseplate-2x2")

    response = client.patch(f"/api/container-types/{seed['id']}", json={"description": "mine now"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["cloned"] is True
    assert body["container_type"]["id"] != seed["id"]
    assert body["container_type"]["description"] == "mine now"
    assert body["container_type"]["is_seed"] is False

    # The seed itself is untouched.
    untouched = client.get(f"/api/container-types/{seed['id']}").json()
    assert untouched["description"] != "mine now"
    assert untouched["is_seed"] is True


def test_patching_a_seed_twice_with_the_same_client_op_id_replays_not_reclones(
    client: TestClient,
) -> None:
    """The whole reason this route needs an idempotency guard unlike a plain
    `PartUpdate`: a retried edit of a seed must not spawn a second clone."""
    seeds = client.get("/api/container-types", params={"is_seed": True}).json()
    seed = next(row for row in seeds if row["slug"] == "gridfinity-baseplate-4x6")

    body = {"description": "retry me", "client_op_id": "seed-patch-retry-1"}
    first = client.patch(f"/api/container-types/{seed['id']}", json=body).json()
    second = client.patch(f"/api/container-types/{seed['id']}", json=body).json()

    assert first["container_type"]["id"] == second["container_type"]["id"]
    assert second["replayed"] is True


def test_cloning_a_type_explicitly_copies_its_materialised_template(client: TestClient) -> None:
    created = _create_type(client, "explicit-clone-source", grid_rows=1, grid_cols=2)
    client.put(
        f"/api/container-types/{created['id']}/slot-template",
        json={
            "slots": [
                {"row_idx": 0, "col_idx": 0, "row_span": 1, "col_span": 2, "slot_label": "Merged"}
            ]
        },
    )

    response = client.post(f"/api/container-types/{created['id']}/clone", json={})
    assert response.status_code == 201, response.text
    clone = response.json()["container_type"]
    assert clone["slug"] == "explicit-clone-source-copy"

    template = client.get(f"/api/container-types/{clone['id']}/slot-template").json()
    assert [s["slot_label"] for s in template["slots"]] == ["Merged"]


# ---------------------------------------------------------------------------
# The slot-template canvas: pure until it isn't
# ---------------------------------------------------------------------------


def test_a_pure_grid_reports_zero_stored_slots_but_a_full_effective_layout(
    client: TestClient,
) -> None:
    created = _create_type(client, "pure-grid-cabinet", grid_rows=2, grid_cols=2)
    template = client.get(f"/api/container-types/{created['id']}/slot-template").json()

    assert template["materialize_slots"] is False
    assert {s["slot_label"] for s in template["slots"]} == {"A1", "A2", "B1", "B2"}


def test_put_with_a_merge_materialises_the_type(client: TestClient) -> None:
    created = _create_type(client, "merge-cabinet", grid_rows=1, grid_cols=2)
    response = client.put(
        f"/api/container-types/{created['id']}/slot-template",
        json={
            "slots": [
                {"row_idx": 0, "col_idx": 0, "row_span": 1, "col_span": 2, "slot_label": "Wide"}
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template"]["materialize_slots"] is True
    assert [s["slot_label"] for s in body["template"]["slots"]] == ["Wide"]


def test_put_with_exactly_the_generated_grid_stays_pure(client: TestClient) -> None:
    created = _create_type(client, "still-pure-cabinet", grid_rows=1, grid_cols=2)
    response = client.put(
        f"/api/container-types/{created['id']}/slot-template",
        json={
            "slots": [
                {"row_idx": 0, "col_idx": 0, "slot_label": "A1"},
                {"row_idx": 0, "col_idx": 1, "slot_label": "A2"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["template"]["materialize_slots"] is False


def test_an_overlapping_slot_template_is_a_422(client: TestClient) -> None:
    created = _create_type(client, "overlap-cabinet", grid_rows=1, grid_cols=2)
    response = client.put(
        f"/api/container-types/{created['id']}/slot-template",
        json={
            "slots": [
                {"row_idx": 0, "col_idx": 0, "row_span": 1, "col_span": 2, "slot_label": "Wide"},
                {"row_idx": 0, "col_idx": 1, "slot_label": "A2"},
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "overlap"


# ---------------------------------------------------------------------------
# Instantiate: independent copies, grid_incompatibility enforced
# ---------------------------------------------------------------------------


def test_instantiate_creates_independent_copies(client: TestClient) -> None:
    cabinet_type = _create_type(client, "instantiate-cabinet", grid_rows=1, grid_cols=2)
    room = _create_location(client, "Instantiate room")

    response = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={"container_type_id": cabinet_type["id"], "count": 2, "naming_pattern": "Cabinet {n}"},
    )
    assert response.status_code == 201, response.text
    locations = response.json()["locations"]
    assert [loc["name"] for loc in locations] == ["Cabinet 1", "Cabinet 2"]

    first_layout = client.get(f"/api/locations/{locations[0]['id']}/layout").json()
    assert {s["slot_label"] for s in first_layout["slots"]} == {"A1", "A2"}

    # Editing the type afterwards must not touch either existing instance.
    client.put(
        f"/api/container-types/{cabinet_type['id']}/slot-template",
        json={
            "slots": [
                {"row_idx": 0, "col_idx": 0, "row_span": 1, "col_span": 2, "slot_label": "Merged"}
            ]
        },
    )
    unaffected = client.get(f"/api/locations/{locations[0]['id']}/layout").json()
    assert {s["slot_label"] for s in unaffected["slots"]} == {"A1", "A2"}


def test_instantiate_at_the_top_of_the_tree_needs_no_parent(client: TestClient) -> None:
    """The first container in an empty install has nowhere to hang off.

    `POST /api/locations/instantiate` is the parentless twin, and the point of it
    is that the *layout* still materialises: a room to draw or a cabinet with
    drawers is exactly what somebody creates first, and before this route the only
    thing possible at the root was a plain container with no slots.
    """
    cabinet_type = _create_type(client, "top-level-cabinet", grid_rows=1, grid_cols=2)

    response = client.post(
        "/api/locations/instantiate",
        json={"container_type_id": cabinet_type["id"], "count": 1, "naming_pattern": "Workshop"},
    )
    assert response.status_code == 201, response.text
    created = response.json()["locations"][0]
    assert created["parent_id"] is None
    assert created["depth"] == 0
    assert created["label_path"] == "Workshop"

    layout = client.get(f"/api/locations/{created['id']}/layout").json()
    assert {s["slot_label"] for s in layout["slots"]} == {"A1", "A2"}

    # And it is a root the tree read agrees is a root.
    tree = client.get("/api/locations/tree").json()
    assert any(node["id"] == created["id"] and node["parent_id"] is None for node in tree["nodes"])


def test_instantiate_at_the_top_replays_on_the_same_client_op_id(client: TestClient) -> None:
    """Same guard as the parented route: it writes a whole subtree per instance,
    so a retried request must not stamp a second cabinet."""
    cabinet_type = _create_type(client, "top-level-idem", grid_rows=1, grid_cols=1)
    body = {
        "container_type_id": cabinet_type["id"],
        "count": 1,
        "naming_pattern": "Bench",
        "client_op_id": "top-level-once",
    }

    first = client.post("/api/locations/instantiate", json=body)
    second = client.post("/api/locations/instantiate", json=body)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["replayed"] is True
    assert first.json()["locations"][0]["id"] == second.json()["locations"][0]["id"]
    roots = [
        node
        for node in client.get("/api/locations/tree").json()["nodes"]
        if node["parent_id"] is None and node["name"] == "Bench"
    ]
    assert len(roots) == 1


def test_instantiate_at_the_top_still_validates_the_type(client: TestClient) -> None:
    response = client.post(
        "/api/locations/instantiate",
        json={"container_type_id": 999_999, "count": 1, "naming_pattern": "Nope"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["reason"] == "unknown_container_type"


def test_instantiate_refuses_a_pitch_mismatch(client: TestClient) -> None:
    plate_type = _create_type(
        client,
        "route-plate",
        grid_rows=4,
        grid_cols=4,
        child_layout="grid",
        grid_pitch_mm=42.0,
        capacity_model="grid_units",
    )
    wrong_bin_type = _create_type(
        client,
        "route-wrong-bin",
        footprint_cols=1,
        footprint_rows=1,
        grid_pitch_mm=50.0,
        capacity_model="volume",
    )
    plate = _create_location(client, "Route plate", container_type_id=plate_type["id"])

    response = client.post(
        f"/api/locations/{plate['id']}/instantiate",
        json={"container_type_id": wrong_bin_type["id"], "count": 1, "naming_pattern": "Bin"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "pitch_mismatch"


def test_creating_a_location_under_an_incompatible_parent_is_409(client: TestClient) -> None:
    """`POST /api/locations` is "any route that parents one container under
    another" — the hard check applies there too, not only at `instantiate`."""
    plate_type = _create_type(
        client,
        "create-route-plate",
        grid_rows=2,
        grid_cols=2,
        child_layout="grid",
        grid_pitch_mm=42.0,
        capacity_model="grid_units",
    )
    too_wide_type = _create_type(
        client, "create-route-too-wide", footprint_cols=5, footprint_rows=1, grid_pitch_mm=42.0
    )
    plate = _create_location(client, "Create-route plate", container_type_id=plate_type["id"])

    response = client.post(
        "/api/locations",
        json={
            "name": "Too wide bin",
            "parent_id": plate["id"],
            "container_type_id": too_wide_type["id"],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "footprint_too_wide"


# ---------------------------------------------------------------------------
# reapply-layout: the change guard, end to end
# ---------------------------------------------------------------------------


def _instantiate_one(client: TestClient, *, rows: int, cols: int, slug: str) -> dict:
    cabinet_type = _create_type(client, slug, grid_rows=rows, grid_cols=cols)
    room = _create_location(client, f"{slug}-room")
    response = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={"container_type_id": cabinet_type["id"], "count": 1, "naming_pattern": "Cabinet"},
    )
    return response.json()["locations"][0]


def test_reapply_layout_safe_relabel(client: TestClient) -> None:
    cabinet = _instantiate_one(client, rows=1, cols=2, slug="reapply-safe")
    current = client.get(f"/api/locations/{cabinet['id']}/layout").json()["slots"]

    desired = [
        {
            "row_idx": s["row_idx"],
            "col_idx": s["col_idx"],
            "row_span": s["row_span"],
            "col_span": s["col_span"],
            "slot_label": "Resistors" if s["slot_label"] == "A1" else s["slot_label"],
        }
        for s in current
    ]
    response = client.post(
        f"/api/locations/{cabinet['id']}/reapply-layout", json={"slots": desired}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == 1
    assert {s["slot_label"] for s in body["layout"]["slots"]} == {"Resistors", "A2"}


def test_reapply_layout_409_lists_every_affected_slot(client: TestClient) -> None:
    cabinet = _instantiate_one(client, rows=1, cols=3, slug="reapply-guarded")
    current = client.get(f"/api/locations/{cabinet['id']}/layout").json()["slots"]
    part = client.post("/api/parts", json={"name": "guard-part", "part_kind": "component"}).json()[
        "part"
    ]

    a2 = next(s for s in current if s["slot_label"] == "A2")
    a3 = next(s for s in current if s["slot_label"] == "A3")
    for slot in (a2, a3):
        client.post(
            "/api/stock/receive",
            json={"part_id": part["id"], "location_id": slot["location_id"], "qty_milli": 1000},
        )

    a1 = next(s for s in current if s["slot_label"] == "A1")
    desired = [{"row_idx": a1["row_idx"], "col_idx": a1["col_idx"], "slot_label": "A1"}]
    response = client.post(
        f"/api/locations/{cabinet['id']}/reapply-layout", json={"slots": desired}
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "slots_hold_content"
    affected_ids = {row["location_id"] for row in detail["affected_slots"]}
    assert affected_ids == {a2["location_id"], a3["location_id"]}
    for row in detail["affected_slots"]:
        assert "has_stock" in row["reasons"]

    # Nothing was touched.
    unchanged = client.get(f"/api/locations/{cabinet['id']}/layout").json()["slots"]
    assert {s["slot_label"] for s in unchanged} == {"A1", "A2", "A3"}


def test_reapply_layout_refuses_reinterpreting_a_slot_identity(client: TestClient) -> None:
    cabinet = _instantiate_one(client, rows=1, cols=2, slug="reapply-refused")
    current = client.get(f"/api/locations/{cabinet['id']}/layout").json()["slots"]
    a2 = next(s for s in current if s["slot_label"] == "A2")

    desired = [{"row_idx": a2["row_idx"], "col_idx": a2["col_idx"], "slot_label": "A1"}]
    response = client.post(
        f"/api/locations/{cabinet['id']}/reapply-layout", json={"slots": desired}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "slot_identity_reinterpreted"


def test_reapply_layout_requires_an_explicit_label_for_every_slot(client: TestClient) -> None:
    cabinet = _instantiate_one(client, rows=1, cols=1, slug="reapply-missing-label")
    response = client.post(
        f"/api/locations/{cabinet['id']}/reapply-layout",
        json={"slots": [{"row_idx": 0, "col_idx": 0}]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "missing_slot_label"


# ---------------------------------------------------------------------------
# GET layout: tag + contents state, not just the grid
# ---------------------------------------------------------------------------


def test_a_relaid_out_slot_gives_up_its_printed_code(client: TestClient) -> None:
    """A card printed for a slot that a re-layout deleted must stop working — and
    above all must not start meaning a *different* drawer.

    `locations.id` is an `INTEGER PRIMARY KEY` with no `AUTOINCREMENT`, so SQLite
    reuses a freed rowid. The re-layout path deleted the location row and left its
    `object_ids` row behind, so the next slot created in that cabinet adopted the
    freed id — and the card in somebody's hand silently began resolving to the
    wrong compartment. `removal._delete` has released these four tables since it
    was written; this path never did.

    Two assertions, and the second is the one that matters: a dangling code that
    resolves to nothing is a nuisance, a code that resolves to somebody else's
    drawer is the failure `removal.py`'s docstring calls worse than no scan at all.
    """
    cabinet = _instantiate_one(client, rows=1, cols=2, slug="relayout-identity")
    slots = client.get(f"/api/locations/{cabinet['id']}/layout").json()["slots"]

    codes: dict[str, str] = {}
    for slot in slots:
        minted = client.post(f"/api/locations/{slot['location_id']}/short-id", json={})
        assert minted.status_code in {200, 201}, minted.text
        codes[slot["slot_label"]] = minted.json()["short_id"]
    doomed = codes["A2"]

    # Replace the pair with a single wide cell, then split it again — the two-tap
    # merge/split a person does in the slot editor.
    merged = client.post(
        f"/api/locations/{cabinet['id']}/reapply-layout",
        json={
            "slots": [
                {"row_idx": 0, "col_idx": 0, "row_span": 1, "col_span": 2, "slot_label": "Wide"}
            ]
        },
    )
    assert merged.status_code == 200, merged.text
    resplit = client.post(
        f"/api/locations/{cabinet['id']}/reapply-layout",
        json={
            "slots": [
                {"row_idx": 0, "col_idx": 0, "row_span": 1, "col_span": 1, "slot_label": "Z1"},
                {"row_idx": 0, "col_idx": 1, "row_span": 1, "col_span": 1, "slot_label": "Z2"},
            ]
        },
    )
    assert resplit.status_code == 200, resplit.text

    resolved = client.get(f"/api/resolve/{doomed}")

    # The card is dead, and says so rather than pointing somewhere.
    assert resolved.status_code in {200, 404}, resolved.text
    if resolved.status_code == 200:
        assert resolved.json()["status"] != "resolved", resolved.json()

    # And no surviving slot has inherited the code.
    live = client.get(f"/api/locations/{cabinet['id']}/layout").json()["slots"]
    assert doomed not in {slot["short_id"] for slot in live}


def test_layout_reports_short_id_tag_and_stock_state(client: TestClient) -> None:
    cabinet_type = _create_type(client, "layout-state-cabinet", grid_rows=1, grid_cols=1)
    room = _create_location(client, "Layout-state room")
    instantiated = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={
            "container_type_id": cabinet_type["id"],
            "count": 1,
            "naming_pattern": "Cabinet",
            "tag_granularity": "slot",
        },
    ).json()["locations"][0]

    layout = client.get(f"/api/locations/{instantiated['id']}/layout").json()
    assert layout["grid_rows"] == 1
    assert layout["grid_cols"] == 1
    [slot] = layout["slots"]
    assert slot["short_id"] is not None
    assert slot["lot_count"] == 0

    part = client.post(
        "/api/parts", json={"name": "layout-state-part", "part_kind": "component"}
    ).json()["part"]
    client.post(
        "/api/stock/receive",
        json={"part_id": part["id"], "location_id": slot["location_id"], "qty_milli": 5000},
    )

    refreshed = client.get(f"/api/locations/{instantiated['id']}/layout").json()
    [slot_after] = refreshed["slots"]
    assert slot_after["lot_count"] == 1
    assert slot_after["qty_milli"] == 5000


# ---------------------------------------------------------------------------
# Numeric bounds — every field here is new, so it needs its own coverage
# alongside tests/integration/test_numeric_bounds.py's existing enumeration.
# ---------------------------------------------------------------------------


def test_an_absurd_container_type_id_is_422_not_500(client: TestClient) -> None:
    assert client.get(f"/api/container-types/{ABSURD}").status_code == 422


def test_an_absurd_grid_dimension_is_422(client: TestClient) -> None:
    response = client.post(
        "/api/container-types",
        json={"slug": "absurd-grid", "display_name": "x", "grid_rows": ABSURD},
    )
    assert response.status_code == 422


def test_an_absurd_slot_position_is_422(client: TestClient) -> None:
    created = _create_type(client, "absurd-slot-position", grid_rows=1, grid_cols=1)
    response = client.put(
        f"/api/container-types/{created['id']}/slot-template",
        json={"slots": [{"row_idx": ABSURD, "col_idx": 0, "slot_label": "X"}]},
    )
    assert response.status_code == 422


def test_an_absurd_instantiate_count_is_422(client: TestClient) -> None:
    cabinet_type = _create_type(client, "absurd-count-cabinet", grid_rows=1, grid_cols=1)
    room = _create_location(client, "Absurd-count room")
    response = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={"container_type_id": cabinet_type["id"], "count": ABSURD, "naming_pattern": "x"},
    )
    assert response.status_code == 422


def test_an_absurd_container_type_id_in_instantiate_is_422(client: TestClient) -> None:
    room = _create_location(client, "Absurd-type-id room")
    response = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={"container_type_id": ABSURD, "count": 1, "naming_pattern": "x"},
    )
    assert response.status_code == 422
