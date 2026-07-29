"""`/api/projects` and `/api/builds` — the whole workflow through the wire.

Each service stage underneath this already has its own deep test suite
(`test_projects_schema.py`, `test_reservations.py`, `test_bom_import.py`); this
file is about the *routes*: request/response shapes, error mapping, the
model-name collision guard, and the numeric-bounds sweep every route module
gets before it ships.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import AllocationState
from tests.factories import (
    make_allocation,
    make_bom_line,
    make_build,
    make_location,
    make_lot,
    make_part,
    make_project,
)

#: Comfortably past SQLite's signed 64-bit maximum — the exact reproduction
#: case `test_numeric_bounds.py` pins for `/api/stock`.
ABSURD = 10**30

_BOM_CSV = "Reference,Value,Footprint,Qty,MPN\nR1,10k,R_0603,1,\nU1,LM358N,SOIC-8,1,LM358N\n"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_create_and_read_project(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "Blinky", "revision": "v1"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project"]["name"] == "Blinky"
    assert body["project"]["revision"] == "v1"
    assert body["project"]["status"] == "planning"
    assert body["project"]["builds"] == []
    assert body["replayed"] is False

    project_id = body["project"]["id"]
    read = client.get(f"/api/projects/{project_id}")
    assert read.status_code == 200
    assert read.json()["name"] == "Blinky"


def test_two_revisions_of_one_name_both_land(client: TestClient) -> None:
    """`name` is deliberately not unique — a uniqueness failure here is the
    exact friction the schema's docstring says the design spends everything
    to avoid."""
    first = client.post("/api/projects", json={"name": "Blinky", "revision": "v1"})
    second = client.post("/api/projects", json={"name": "Blinky", "revision": "v2"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["project"]["id"] != second.json()["project"]["id"]


def test_create_project_is_idempotent_on_client_op_id(client: TestClient) -> None:
    body = {"name": "Blinky", "client_op_id": "op-1"}
    first = client.post("/api/projects", json=body)
    second = client.post("/api/projects", json=body)
    assert first.json()["project"]["id"] == second.json()["project"]["id"]
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True

    listing = client.get("/api/projects").json()
    assert listing["total"] == 1


def test_patch_project_sets_only_the_fields_given(client: TestClient) -> None:
    created = client.post("/api/projects", json={"name": "Blinky", "notes": "first"}).json()
    project_id = created["project"]["id"]

    patched = client.patch(f"/api/projects/{project_id}", json={"status": "active"})
    assert patched.status_code == 200
    body = patched.json()
    assert body["status"] == "active"
    assert body["notes"] == "first"  # untouched


def test_patch_project_can_clear_a_nullable_field(client: TestClient) -> None:
    created = client.post("/api/projects", json={"name": "Blinky", "notes": "first"}).json()
    project_id = created["project"]["id"]

    patched = client.patch(f"/api/projects/{project_id}", json={"notes": None})
    assert patched.json()["notes"] is None


def test_list_projects_filters_by_status(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "A", "status": "planning"})
    active = client.post("/api/projects", json={"name": "B", "status": "active"}).json()

    listing = client.get("/api/projects", params={"status": "active"}).json()
    assert listing["total"] == 1
    assert listing["projects"][0]["id"] == active["project"]["id"]


def test_read_unknown_project_is_404(client: TestClient) -> None:
    response = client.get("/api/projects/999999")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_project"


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


def _project(client: TestClient, **kwargs: object) -> dict:
    payload = {"name": "Blinky"}
    payload.update(kwargs)
    return client.post("/api/projects", json=payload).json()["project"]


def test_build_numbers_are_sequential_per_project(client: TestClient) -> None:
    project = _project(client, revision="rev-b")
    first = client.post(f"/api/projects/{project['id']}/builds", json={})
    second = client.post(f"/api/projects/{project['id']}/builds", json={})
    assert first.status_code == 201
    assert first.json()["build"]["build_no"] == 1
    assert second.json()["build"]["build_no"] == 2
    # bom_revision is copied from the project at plan time, never accepted
    # from the client.
    assert first.json()["build"]["bom_revision"] == "rev-b"


def test_build_appears_on_its_project(client: TestClient) -> None:
    project = _project(client)
    client.post(f"/api/projects/{project['id']}/builds", json={"label": "run 1"})
    read = client.get(f"/api/projects/{project['id']}").json()
    assert [b["label"] for b in read["builds"]] == ["run 1"]


def test_read_build(client: TestClient) -> None:
    project = _project(client)
    created = client.post(
        f"/api/projects/{project['id']}/builds", json={"assembly_count": 5}
    ).json()["build"]
    read = client.get(f"/api/builds/{created['id']}")
    assert read.status_code == 200
    assert read.json()["assembly_count"] == 5
    assert read.json()["status"] == "planned"


def test_create_build_for_unknown_project_is_404(client: TestClient) -> None:
    response = client.post("/api/projects/999999/builds", json={})
    assert response.status_code == 404


def test_patch_build_to_in_progress_sets_started_at(client: TestClient) -> None:
    project = _project(client)
    build = client.post(f"/api/projects/{project['id']}/builds", json={}).json()["build"]
    patched = client.patch(f"/api/builds/{build['id']}", json={"status": "in_progress"})
    assert patched.status_code == 200
    assert patched.json()["started_at"] is not None
    assert patched.json()["completed_at"] is None


def test_closing_a_build_releases_its_open_reservations(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "R1 part")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=10_000)
    db.commit()

    reserve_response = client.post(
        f"/api/builds/{build_row.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": 4_000},
    )
    assert reserve_response.status_code == 200, reserve_response.text
    assert reserve_response.json()["lot"]["qty_reserved_milli"] == 4_000

    closed = client.patch(f"/api/builds/{build_row.id}", json={"status": "completed"})
    assert closed.status_code == 200
    assert closed.json()["completed_at"] is not None

    after = client.get(f"/api/stock/lots/{lot.id}")
    assert after.json()["qty_reserved_milli"] == 0


def test_closing_a_build_twice_is_a_harmless_no_op(client: TestClient, db: Session) -> None:
    """Closing is idempotent by construction — the whole reason it needs no
    `client_op_id`."""
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    db.commit()

    first = client.patch(f"/api/builds/{build_row.id}", json={"status": "completed"})
    completed_at = first.json()["completed_at"]
    second = client.patch(f"/api/builds/{build_row.id}", json={"status": "completed"})
    assert second.json()["completed_at"] == completed_at


# ---------------------------------------------------------------------------
# BOM import and lines
# ---------------------------------------------------------------------------


def test_bom_import_lands_lines_and_matches_by_mpn(client: TestClient, db: Session) -> None:
    make_part(db, "LM358 opamp", mpn="LM358N")
    db.commit()

    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/bom/import",
        json={"content": _BOM_CSV, "source_ref": "kicad-export.csv"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matched_count"] == 1
    assert body["unmatched_count"] == 1
    assert len(body["lines"]) == 2

    project_after = client.get(f"/api/projects/{project['id']}").json()
    assert project_after["source_ref"] == "kicad-export.csv"


def test_bom_import_is_idempotent_on_client_op_id(client: TestClient) -> None:
    project = _project(client)
    body = {"content": _BOM_CSV, "client_op_id": "import-1"}
    first = client.post(f"/api/projects/{project['id']}/bom/import", json=body)
    second = client.post(f"/api/projects/{project['id']}/bom/import", json=body)
    assert first.json()["lines"] != []
    listing = client.get(f"/api/projects/{project['id']}/bom").json()
    # Two lines from one import, not four from a silently repeated one.
    assert listing["total"] == 2
    assert second.json()["replayed"] is True


def test_list_bom_lines_unmatched_only(client: TestClient, db: Session) -> None:
    make_part(db, "LM358 opamp", mpn="LM358N")
    db.commit()

    project = _project(client)
    client.post(f"/api/projects/{project['id']}/bom/import", json={"content": _BOM_CSV})

    all_lines = client.get(f"/api/projects/{project['id']}/bom").json()
    unmatched = client.get(
        f"/api/projects/{project['id']}/bom", params={"unmatched_only": True}
    ).json()
    assert all_lines["total"] == 2
    assert unmatched["total"] == 1
    assert unmatched["lines"][0]["part_id"] is None


def test_manually_matching_a_line_confirms_it_by_default(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    line = make_bom_line(db, project_row, designators="R1")
    part = make_part(db, "10k resistor")
    db.commit()

    response = client.put(
        f"/api/projects/{project_row.id}/bom",
        json={"edits": [{"id": line.id, "part_id": part.id}]},
    )
    assert response.status_code == 200, response.text
    edited = response.json()["lines"][0]
    assert edited["part_id"] == part.id
    assert edited["is_match_confirmed"] is True


def test_clearing_part_id_forces_is_match_confirmed_false(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    part = make_part(db, "10k resistor")
    line = make_bom_line(db, project_row, part_id=part.id, is_match_confirmed=True)
    db.commit()

    response = client.put(
        f"/api/projects/{project_row.id}/bom",
        json={"edits": [{"id": line.id, "part_id": None}]},
    )
    edited = response.json()["lines"][0]
    assert edited["part_id"] is None
    assert edited["is_match_confirmed"] is False


def test_confirming_a_match_with_no_part_at_all_is_rejected(
    client: TestClient, db: Session
) -> None:
    project_row = make_project(db)
    line = make_bom_line(db, project_row)
    db.commit()

    response = client.put(
        f"/api/projects/{project_row.id}/bom",
        json={"edits": [{"id": line.id, "is_match_confirmed": True}]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "no_part_to_confirm"


def test_editing_a_line_from_another_project_is_rejected(client: TestClient, db: Session) -> None:
    project_a = make_project(db, name="A")
    project_b = make_project(db, name="B")
    line = make_bom_line(db, project_b)
    db.commit()

    response = client.put(
        f"/api/projects/{project_a.id}/bom",
        json={"edits": [{"id": line.id, "note": "hi"}]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "line_not_in_project"


# ---------------------------------------------------------------------------
# Shortages, allocate, release
# ---------------------------------------------------------------------------


def test_shortage_report_distinguishes_short_from_unidentified(
    client: TestClient, db: Session
) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    make_bom_line(db, project_row, qty_per_assembly_milli=10_000, part_id=part.id)
    make_bom_line(db, project_row, qty_per_assembly_milli=1_000)  # unmatched
    db.commit()

    response = client.get(f"/api/builds/{build_row.id}/shortages")
    assert response.status_code == 200
    body = response.json()
    assert body["is_buildable"] is False
    kinds = {line["kind"] for line in body["lines"]}
    assert "short" in kinds
    assert "unidentified" in kinds


def test_allocate_refuses_to_overcommit_by_default(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    db.commit()

    response = client.post(
        f"/api/builds/{build_row.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": 5_000},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "insufficient_available"


def test_allocate_allow_overcommit_is_explicit(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    db.commit()

    response = client.post(
        f"/api/builds/{build_row.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": 5_000, "allow_overcommit": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["lot"]["qty_reserved_milli"] == 5_000


def test_allocate_rejects_a_part_id_mismatched_with_the_lot(
    client: TestClient, db: Session
) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    other_part = make_part(db, "capacitor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    db.commit()

    response = client.post(
        f"/api/builds/{build_row.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": 100, "part_id": other_part.id},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "part_lot_mismatch"


def test_release_one_allocation(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    db.commit()

    allocation = client.post(
        f"/api/builds/{build_row.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": 500},
    ).json()["allocation"]

    response = client.post(
        f"/api/builds/{build_row.id}/release",
        json={"allocation_id": allocation["id"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["released_count"] == 1
    assert body["allocation"]["state"] == "released"
    assert body["lot"]["qty_reserved_milli"] == 0


def test_release_whole_build_frees_every_open_hold(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot_a = make_lot(db, part, location, qty_milli=1_000)
    lot_b = make_lot(db, part, make_location(db, "second bin"), qty_milli=1_000)
    db.commit()

    client.post(f"/api/builds/{build_row.id}/allocate", json={"lot_id": lot_a.id, "qty_milli": 200})
    client.post(f"/api/builds/{build_row.id}/allocate", json={"lot_id": lot_b.id, "qty_milli": 300})

    response = client.post(f"/api/builds/{build_row.id}/release", json={})
    assert response.status_code == 200
    assert response.json()["released_count"] == 2

    assert client.get(f"/api/stock/lots/{lot_a.id}").json()["qty_reserved_milli"] == 0
    assert client.get(f"/api/stock/lots/{lot_b.id}").json()["qty_reserved_milli"] == 0


def test_releasing_a_consumed_allocation_is_a_409_not_a_500(
    client: TestClient, db: Session
) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    allocation = make_allocation(db, build_row, part, 500, state=AllocationState.CONSUMED, lot=lot)
    db.commit()

    response = client.post(
        f"/api/builds/{build_row.id}/release", json={"allocation_id": allocation.id}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "already_consumed"


def test_release_of_allocation_on_another_build_is_404(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    build_a = make_build(db, project_row)
    build_b = make_build(db, project_row, build_no=2)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    allocation = make_allocation(db, build_a, part, 100, state=AllocationState.RESERVED, lot=lot)
    lot.qty_reserved_milli_cached = 100
    db.commit()

    response = client.post(
        f"/api/builds/{build_b.id}/release", json={"allocation_id": allocation.id}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Model-name collisions, and the route list itself
# ---------------------------------------------------------------------------


def test_no_schema_names_collided(client: TestClient) -> None:
    """Same guard as `test_health.test_no_two_modules_share_a_response_model_
    name`, run here too so a broken build of *this* module fails locally
    without needing to know that test exists."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    qualified = sorted(name for name in schemas if "__" in name)
    assert qualified == []


def test_every_new_route_is_in_the_openapi_document(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    paths = document["paths"]
    expected = {
        ("/api/projects", "post"),
        ("/api/projects", "get"),
        ("/api/projects/{project_id}", "get"),
        ("/api/projects/{project_id}", "patch"),
        ("/api/projects/{project_id}/builds", "post"),
        ("/api/builds/{build_id}", "get"),
        ("/api/builds/{build_id}", "patch"),
        ("/api/projects/{project_id}/bom/import", "post"),
        ("/api/projects/{project_id}/bom", "get"),
        ("/api/projects/{project_id}/bom", "put"),
        ("/api/builds/{build_id}/shortages", "get"),
        ("/api/builds/{build_id}/allocate", "post"),
        ("/api/builds/{build_id}/release", "post"),
    }
    for path, method in expected:
        assert path in paths, f"{path} missing from openapi.json"
        assert method in paths[path], f"{method} {path} missing from openapi.json"


# ---------------------------------------------------------------------------
# Hostile numeric input: every field must 422, never 500
# ---------------------------------------------------------------------------


def test_every_numeric_field_rejects_an_absurd_value(client: TestClient, db: Session) -> None:
    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    line = make_bom_line(db, project_row)
    db.commit()

    cases: list[tuple[str, str, dict[str, object]]] = [
        ("post", "/api/projects", {"name": "x", "client_op_id": "x" * 40}),
        ("get", f"/api/projects/{ABSURD}", {}),
        ("patch", f"/api/projects/{ABSURD}", {"notes": "x"}),
        ("post", f"/api/projects/{ABSURD}/builds", {}),
        ("post", f"/api/projects/{project_row.id}/builds", {"assembly_count": ABSURD}),
        ("get", f"/api/builds/{ABSURD}", {}),
        (
            "put",
            f"/api/projects/{project_row.id}/bom",
            {"edits": [{"id": ABSURD}]},
        ),
        (
            "put",
            f"/api/projects/{project_row.id}/bom",
            {"edits": [{"id": line.id, "part_id": ABSURD}]},
        ),
        (
            "put",
            f"/api/projects/{project_row.id}/bom",
            {"edits": [{"id": line.id, "qty_per_assembly_milli": ABSURD}]},
        ),
        ("get", f"/api/builds/{ABSURD}/shortages", {}),
        (
            "post",
            f"/api/builds/{build_row.id}/allocate",
            {"lot_id": ABSURD, "qty_milli": 100},
        ),
        (
            "post",
            f"/api/builds/{build_row.id}/allocate",
            {"lot_id": lot.id, "qty_milli": ABSURD},
        ),
        (
            "post",
            f"/api/builds/{build_row.id}/allocate",
            {"lot_id": lot.id, "qty_milli": 100, "part_id": ABSURD},
        ),
        (
            "post",
            f"/api/builds/{build_row.id}/allocate",
            {"lot_id": lot.id, "qty_milli": 100, "bom_line_id": ABSURD},
        ),
        (
            "post",
            f"/api/builds/{ABSURD}/allocate",
            {"lot_id": lot.id, "qty_milli": 100},
        ),
        (
            "post",
            f"/api/builds/{build_row.id}/release",
            {"allocation_id": ABSURD},
        ),
        ("post", f"/api/builds/{ABSURD}/release", {}),
    ]
    for method, path, body in cases:
        response = client.get(path) if method == "get" else getattr(client, method)(path, json=body)
        assert response.status_code == 422, f"{method} {path} {body} -> {response.status_code}"


def test_nothing_was_written_by_a_rejected_absurd_allocate(client: TestClient, db: Session) -> None:
    from app.models.projects import StockAllocation

    project_row = make_project(db)
    build_row = make_build(db, project_row)
    part = make_part(db, "resistor")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=1_000)
    db.commit()

    client.post(
        f"/api/builds/{build_row.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": ABSURD},
    )

    from sqlalchemy import select

    assert db.execute(select(StockAllocation)).first() is None
