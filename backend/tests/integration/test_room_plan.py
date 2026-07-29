"""Drawn rooms and placed containers — ADR 0009.

Four properties, in the order they matter:

1. **A placement is a coordinate, not a slot.** Children get x/y in millimetres,
   several at a time in one request, and an unplaced child stays unplaced rather
   than landing at the origin.
2. **A reparent invalidates a placement**, because a coordinate is meaningless in
   another room — and it does so through the `plan_parent_id == parent_id` read
   guard, so it holds for a write path that never heard of ADR 0009.
3. **A drawn wall is not a location.** Shapes round-trip through their own table
   and the tree does not grow a single row.
4. Coordinates are authoring data and never reach a printed or tag payload.

Every test runs against real Alembic migrations (`tests/conftest.py`).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.enums import PlanShapeKind
from app.models.storage import Location, LocationPlanShape, LocationPlanShapePoint
from app.services import room_plan
from app.services.tree import location_tree
from tests.factories import make_container_type, make_location


def _room(db: Session, name: str = "Workshop") -> Location:
    room = make_location(db, name=name)
    location_tree(db).rebuild_paths()
    return room


def _child(db: Session, parent: Location, name: str, **kwargs: object) -> Location:
    child = make_location(db, name=name, parent_id=parent.id, **kwargs)
    location_tree(db).rebuild_paths()
    return child


def _outline(*points: tuple[int, int]) -> dict[str, object]:
    """An L-shaped room is the default on purpose: the whole argument for a
    polyline over a width/depth pair is that rooms have alcoves."""
    return {
        "kind": PlanShapeKind.OUTLINE,
        "is_closed": True,
        "points": [{"x_mm": x, "y_mm": y} for x, y in points],
    }


# ---------------------------------------------------------------------------
# 1. Placing children — in one request
# ---------------------------------------------------------------------------


def test_a_batch_of_placements_is_one_request(client: TestClient, db: Session) -> None:
    """Dragging three cabinets and then saving is one write.

    Per-child routes would make a three-box rearrangement three requests that can
    partially fail, leaving the room in a state nobody authored.
    """
    room = _room(db)
    cabinets = [_child(db, room, f"Cabinet {index}") for index in range(3)]
    db.commit()

    response = client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={
            "placements": [
                {"location_id": cabinets[0].id, "x_mm": 0, "y_mm": 0},
                {"location_id": cabinets[1].id, "x_mm": 1200, "y_mm": 0, "rotation_deg": 90},
                {
                    "location_id": cabinets[2].id,
                    "x_mm": -300,
                    "y_mm": 2400,
                    "width_mm": 600,
                    "depth_mm": 400,
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unplaced_location_ids"] == []
    by_id = {item["location_id"]: item for item in body["placements"]}
    assert by_id[cabinets[1].id]["rotation_deg"] == 90
    assert by_id[cabinets[2].id]["x_mm"] == -300
    assert (by_id[cabinets[2].id]["width_mm"], by_id[cabinets[2].id]["depth_mm"]) == (600, 400)
    # Every placement records the parent it was authored against.
    assert {item["parent_id"] for item in body["placements"]} == {room.id}


def test_a_negative_coordinate_is_accepted(client: TestClient, db: Session) -> None:
    """The origin is wherever the person drawing put it.

    Forcing coordinates positive would make the first wall somebody drew the
    corner of the room, which is not a promise a drawing tool can keep.
    """
    room = _room(db)
    bench = _child(db, room, "Bench cabinet")
    db.commit()

    response = client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={"placements": [{"location_id": bench.id, "x_mm": -5000, "y_mm": -250}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["placements"][0]["x_mm"] == -5000


def test_a_footprint_falls_back_to_the_container_type(client: TestClient, db: Session) -> None:
    """Null width/depth means "use the type's", which is the common case."""
    room = _room(db)
    cabinet_type = make_container_type(
        db, slug="raaco-150-30", front_width_mm=306.0, inner_length_mm=150.0
    )
    cabinet = _child(db, room, "Raaco", container_type_id=cabinet_type.id)
    db.commit()

    client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={"placements": [{"location_id": cabinet.id, "x_mm": 10, "y_mm": 20}]},
    ).raise_for_status()
    placement = client.get(f"/api/locations/{room.id}/plan").json()["placements"][0]
    assert (placement["width_mm"], placement["depth_mm"]) == (306, 150)


def test_an_unplaced_child_is_reported_as_unplaced(client: TestClient, db: Session) -> None:
    """The property this whole design exists to keep.

    A container added to a room but never dragged anywhere is a real state, and
    the alternative — defaulting to (0, 0) — would put every box in the same
    corner and look authored. It must therefore be *reportable*, both in the
    room's plan and on the container's own detail read.
    """
    room = _room(db)
    placed = _child(db, room, "Placed")
    never_touched = _child(db, room, "Never touched")
    db.commit()

    client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={"placements": [{"location_id": placed.id, "x_mm": 100, "y_mm": 100}]},
    ).raise_for_status()

    plan = client.get(f"/api/locations/{room.id}/plan").json()
    assert [item["location_id"] for item in plan["placements"]] == [placed.id]
    assert plan["unplaced_location_ids"] == [never_touched.id]
    assert client.get(f"/api/locations/{never_touched.id}").json()["placement"] is None
    assert client.get(f"/api/locations/{placed.id}").json()["placement"]["x_mm"] == 100


def test_a_child_can_be_returned_to_the_unplaced_tray(client: TestClient, db: Session) -> None:
    """ "Not placed" has no coordinate that expresses it, so it is its own field.

    Sending (0, 0) to mean "un-place this" would put the box in a corner instead,
    which is why `unplace_location_ids` is separate from `placements`.
    """
    room = _room(db)
    cabinet = _child(db, room, "Cabinet")
    db.commit()
    client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={"placements": [{"location_id": cabinet.id, "x_mm": 500, "y_mm": 500}]},
    ).raise_for_status()

    response = client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={"placements": [], "unplace_location_ids": [cabinet.id]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["unplaced_location_ids"] == [cabinet.id]
    assert response.json()["placements"] == []


def test_placing_something_that_is_not_a_child_is_refused(client: TestClient, db: Session) -> None:
    """A coordinate authored against one room is meaningless in another, so this
    is a 422 rather than a placement that the read guard would ignore anyway —
    silently accepting it would let a client believe a drag was saved."""
    room = _room(db)
    other = _room(db, "Garage")
    stranger = _child(db, other, "Shelf in the garage")
    db.commit()

    response = client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={"placements": [{"location_id": stranger.id, "x_mm": 0, "y_mm": 0}]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "not_a_child"


def test_a_self_contradicting_batch_is_refused(client: TestClient, db: Session) -> None:
    """Placed and unplaced in the same request: guessing which half was meant is
    how a drag gets silently discarded."""
    room = _room(db)
    cabinet = _child(db, room, "Cabinet")
    db.commit()

    response = client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={
            "placements": [{"location_id": cabinet.id, "x_mm": 0, "y_mm": 0}],
            "unplace_location_ids": [cabinet.id],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "placed_and_unplaced"


def test_the_same_child_placed_twice_in_one_batch_is_refused(
    client: TestClient, db: Session
) -> None:
    """Last-write-wins would make the result depend on array order in a request
    that is otherwise a set."""
    room = _room(db)
    cabinet = _child(db, room, "Cabinet")
    db.commit()

    response = client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={
            "placements": [
                {"location_id": cabinet.id, "x_mm": 0, "y_mm": 0},
                {"location_id": cabinet.id, "x_mm": 900, "y_mm": 0},
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "duplicate_placement"


def test_a_rotation_outside_a_circle_is_a_422(client: TestClient, db: Session) -> None:
    room = _room(db)
    cabinet = _child(db, room, "Cabinet")
    db.commit()
    response = client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={
            "placements": [{"location_id": cabinet.id, "x_mm": 0, "y_mm": 0, "rotation_deg": 400}]
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 2. A reparent invalidates the placement
# ---------------------------------------------------------------------------


def test_moving_a_container_to_another_room_invalidates_its_placement(
    client: TestClient, db: Session
) -> None:
    """A coordinate is meaningless in another room.

    Moved through `TreeRepository.move`, which is the only reparent path in the
    codebase — so this is the invalidation as an actual user would trigger it.
    """
    workshop = _room(db, "Workshop")
    garage = _room(db, "Garage")
    cabinet = _child(db, workshop, "Cabinet on castors")
    db.commit()

    client.put(
        f"/api/locations/{workshop.id}/plan/placements",
        json={"placements": [{"location_id": cabinet.id, "x_mm": 800, "y_mm": 1600}]},
    ).raise_for_status()

    db.expire_all()
    location_tree(db).move(db.get(Location, cabinet.id), garage.id)  # type: ignore[arg-type]
    db.commit()

    assert client.get(f"/api/locations/{workshop.id}/plan").json()["placements"] == []
    garage_plan = client.get(f"/api/locations/{garage.id}/plan").json()
    assert garage_plan["placements"] == []
    assert garage_plan["unplaced_location_ids"] == [cabinet.id]
    assert client.get(f"/api/locations/{cabinet.id}").json()["placement"] is None


def test_the_invalidation_survives_a_reparent_that_never_heard_of_it(
    client: TestClient, db: Session
) -> None:
    """The reason `plan_parent_id` is stored at all.

    A bulk import, a future move endpoint or a hand-written `UPDATE` can change
    `parent_id` without going anywhere near `room_plan.forget_placement` — this
    reparents with raw SQL, deliberately leaving the placement columns intact, and
    the coordinate must *still* be invisible. Clearing on write alone would leave
    the cabinet standing at 800/1600 in a room it has never been in.
    """
    workshop = _room(db, "Workshop")
    garage = _room(db, "Garage")
    cabinet = _child(db, workshop, "Cabinet on castors")
    db.commit()
    client.put(
        f"/api/locations/{workshop.id}/plan/placements",
        json={"placements": [{"location_id": cabinet.id, "x_mm": 800, "y_mm": 1600}]},
    ).raise_for_status()

    db.execute(
        text("UPDATE locations SET parent_id = :new WHERE id = :id"),
        {"new": garage.id, "id": cabinet.id},
    )
    db.commit()
    db.expire_all()

    row = db.get(Location, cabinet.id)
    assert row is not None
    # The columns are untouched, and the placement is nonetheless gone.
    assert (row.plan_x_mm, row.plan_parent_id) == (800, workshop.id)
    assert room_plan.placement_of(db, row) is None
    assert client.get(f"/api/locations/{garage.id}/plan").json()["placements"] == []


def test_moving_back_does_not_resurrect_the_old_coordinate(client: TestClient, db: Session) -> None:
    """Round-tripping a cabinet out of a room and back leaves it unplaced.

    The alternative — remembering per-parent coordinates so a return restores the
    old spot — is a `location_placements(child, parent)` table, and ADR 0009
    rejects it: the coordinate is stale the moment the furniture moves anyway,
    and a resurrected one is a lie that looks authored.
    """
    workshop = _room(db, "Workshop")
    garage = _room(db, "Garage")
    cabinet = _child(db, workshop, "Cabinet on castors")
    db.commit()
    client.put(
        f"/api/locations/{workshop.id}/plan/placements",
        json={"placements": [{"location_id": cabinet.id, "x_mm": 800, "y_mm": 1600}]},
    ).raise_for_status()
    db.expire_all()

    tree = location_tree(db)
    moved = db.get(Location, cabinet.id)
    assert moved is not None
    tree.move(moved, garage.id)
    tree.move(moved, workshop.id)
    db.commit()

    plan = client.get(f"/api/locations/{workshop.id}/plan").json()
    assert plan["placements"] == []
    assert plan["unplaced_location_ids"] == [cabinet.id]


# ---------------------------------------------------------------------------
# 3. Drawn shapes round-trip, and are not locations
# ---------------------------------------------------------------------------


def test_a_drawn_room_round_trips(client: TestClient, db: Session) -> None:
    """An L-shaped outline, a stud wall and a door, saved and read back.

    The alcove is the point: a width/depth pair could not express this room, and
    the alcove is usually exactly where the bench goes.
    """
    room = _room(db)
    db.commit()

    response = client.put(
        f"/api/locations/{room.id}/plan/shapes",
        json={
            "shapes": [
                _outline((0, 0), (4000, 0), (4000, 2500), (2500, 2500), (2500, 3500), (0, 3500)),
                {
                    "kind": PlanShapeKind.WALL,
                    "label": "stud wall",
                    "thickness_mm": 100,
                    "points": [{"x_mm": 2500, "y_mm": 2500}, {"x_mm": 4000, "y_mm": 2500}],
                },
                {
                    "kind": PlanShapeKind.DOOR,
                    "points": [{"x_mm": 0, "y_mm": 800}, {"x_mm": 0, "y_mm": 1700}],
                },
            ]
        },
    )
    assert response.status_code == 200, response.text

    plan = client.get(f"/api/locations/{room.id}/plan").json()
    assert [shape["kind"] for shape in plan["shapes"]] == ["outline", "wall", "door"]
    outline = plan["shapes"][0]
    assert outline["is_closed"] is True
    assert len(outline["points"]) == 6
    # Vertex order is stored, not inferred: the alcove is the 4th and 5th points
    # and a scrambled polyline is a different room.
    assert outline["points"][3] == {"x_mm": 2500, "y_mm": 2500}
    assert outline["points"][4] == {"x_mm": 2500, "y_mm": 3500}
    assert plan["shapes"][1]["thickness_mm"] == 100
    assert plan["shapes"][2]["thickness_mm"] is None
    assert plan["extent"] == {
        "min_x_mm": 0,
        "min_y_mm": 0,
        "max_x_mm": 4000,
        "max_y_mm": 3500,
    }


def test_a_drawn_wall_is_not_a_location(client: TestClient, db: Session) -> None:
    """The reason `location_plan_shapes` is its own table.

    A wall holds no stock, gets no `short_id`, resolves from no scan and must
    never appear in the tree. Had it been a `locations` row with a kind on it, the
    tree would contain furniture nobody can put anything in — and auto-assignment
    would have to learn to skip it.
    """
    room = _room(db)
    db.commit()
    before = db.execute(select(func.count()).select_from(Location)).scalar_one()

    client.put(
        f"/api/locations/{room.id}/plan/shapes",
        json={
            "shapes": [
                _outline((0, 0), (2000, 0), (2000, 2000), (0, 2000)),
                {
                    "kind": PlanShapeKind.FIXTURE,
                    "label": "sink",
                    "is_closed": True,
                    "points": [
                        {"x_mm": 0, "y_mm": 1500},
                        {"x_mm": 600, "y_mm": 1500},
                        {"x_mm": 600, "y_mm": 2000},
                        {"x_mm": 0, "y_mm": 2000},
                    ],
                },
            ]
        },
    ).raise_for_status()

    assert db.execute(select(func.count()).select_from(Location)).scalar_one() == before
    nodes = client.get("/api/locations/tree").json()["nodes"]
    assert len(nodes) == before
    assert not any("sink" in node["name"].lower() for node in nodes)
    # And no shape ever acquires a printed identity.
    assert (
        db.execute(
            text("SELECT COUNT(*) FROM object_ids WHERE entity_type LIKE '%shape%'")
        ).scalar_one()
        == 0
    )


def test_saving_a_plan_replaces_the_whole_drawing(client: TestClient, db: Session) -> None:
    """Whole-plan replacement, not per-shape CRUD.

    A drawing session ends with "this is the room now". The consequence worth
    asserting is that nothing is left behind — including the *points* of the
    shapes that went away, which would otherwise be orphan rows nothing reads and
    nothing cleans up.
    """
    room = _room(db)
    db.commit()
    client.put(
        f"/api/locations/{room.id}/plan/shapes",
        json={
            "shapes": [
                _outline((0, 0), (3000, 0), (3000, 3000), (0, 3000)),
                {
                    "kind": PlanShapeKind.WALL,
                    "points": [{"x_mm": 0, "y_mm": 1500}, {"x_mm": 3000, "y_mm": 1500}],
                },
            ]
        },
    ).raise_for_status()

    client.put(
        f"/api/locations/{room.id}/plan/shapes",
        json={"shapes": [_outline((0, 0), (3000, 0), (3000, 3000), (0, 3000))]},
    ).raise_for_status()

    plan = client.get(f"/api/locations/{room.id}/plan").json()
    assert [shape["kind"] for shape in plan["shapes"]] == ["outline"]
    assert db.execute(select(func.count()).select_from(LocationPlanShape)).scalar_one() == 1
    assert db.execute(select(func.count()).select_from(LocationPlanShapePoint)).scalar_one() == 4


def test_an_empty_shape_list_erases_the_drawing(client: TestClient, db: Session) -> None:
    """Sending nothing is a real edit, not an omission — the same convention as
    clearing a `child_view` override with an explicit null."""
    room = _room(db)
    db.commit()
    client.put(
        f"/api/locations/{room.id}/plan/shapes",
        json={"shapes": [_outline((0, 0), (1000, 0), (1000, 1000))]},
    ).raise_for_status()

    response = client.put(f"/api/locations/{room.id}/plan/shapes", json={"shapes": []})
    assert response.status_code == 200, response.text
    assert response.json()["shapes"] == []
    assert response.json()["extent"] is None
    assert db.execute(select(func.count()).select_from(LocationPlanShapePoint)).scalar_one() == 0


def test_a_one_point_shape_is_a_422(client: TestClient, db: Session) -> None:
    """Two vertices is the minimum that is a line at all."""
    room = _room(db)
    db.commit()
    response = client.put(
        f"/api/locations/{room.id}/plan/shapes",
        json={"shapes": [{"kind": "wall", "points": [{"x_mm": 0, "y_mm": 0}]}]},
    )
    assert response.status_code == 422


def test_an_undrawn_room_is_not_a_404(client: TestClient, db: Session) -> None:
    """The editor has to be the thing you draw the first wall in, so a room with
    nothing drawn and nothing placed answers with empty lists and a null
    extent — never an error, and never a default canvas that is not there."""
    room = _room(db)
    db.commit()
    plan = client.get(f"/api/locations/{room.id}/plan").json()
    assert plan == {
        "location_id": room.id,
        "shapes": [],
        "placements": [],
        "unplaced_location_ids": [],
        "extent": None,
    }


def test_the_extent_covers_placed_footprints_too(client: TestClient, db: Session) -> None:
    """A cabinet against the right-hand wall must not be half outside the canvas
    the client sizes from its own footprint."""
    room = _room(db)
    cabinet = _child(db, room, "Cabinet")
    db.commit()
    client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={
            "placements": [
                {
                    "location_id": cabinet.id,
                    "x_mm": 1000,
                    "y_mm": 0,
                    "width_mm": 600,
                    "depth_mm": 400,
                }
            ]
        },
    ).raise_for_status()
    # The bounding box is of what is *there*, not of the origin — an empty room
    # has no extent at all, so there is no origin to anchor to.
    assert client.get(f"/api/locations/{room.id}/plan").json()["extent"] == {
        "min_x_mm": 1000,
        "min_y_mm": 0,
        "max_x_mm": 1600,
        "max_y_mm": 400,
    }


# ---------------------------------------------------------------------------
# 4. Coordinates are authoring data
# ---------------------------------------------------------------------------


def test_a_coordinate_never_reaches_a_printed_or_tag_payload(
    client: TestClient, db: Session
) -> None:
    """The same rule that keeps hierarchy off a tag, for the same reason.

    A short_id is minted for a placed cabinet and its payload must carry the
    opaque code and nothing else — a coordinate written to a tag would be a lie
    the moment the cabinet is wheeled two metres to the left, and unlike the
    database, the tag cannot be corrected without holding it.
    """
    room = _room(db)
    cabinet = _child(db, room, "Cabinet on castors")
    db.commit()
    client.put(
        f"/api/locations/{room.id}/plan/placements",
        json={"placements": [{"location_id": cabinet.id, "x_mm": 1234, "y_mm": 5678}]},
    ).raise_for_status()

    minted = client.post(f"/api/locations/{cabinet.id}/short-id", json={}).json()
    assert "1234" not in minted["short_id"]
    resolved = client.get(f"/api/resolve/{minted['short_id']}").json()
    payload = str(resolved)
    assert "1234" not in payload and "5678" not in payload
