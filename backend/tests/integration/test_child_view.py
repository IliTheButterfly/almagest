"""The per-layer view type — ADR 0006.

Three properties, in the order they matter:

1. **The axis is separate from `child_layout`.** That is the decision under
   test, not a style preference: `app.services.assignment` selects containers by
   `child_layout == 'grid'` in order to materialise a cell for a scan that would
   otherwise have nowhere to land, so a drawing kind smuggled into that enum
   would silently remove a cabinet from auto-assignment.
2. **The three-rung fallback**, and that the derived rung is right for every
   seed type — which is what made the migration a pure column add with no
   backfill.
3. **Nothing is inherited.** A view is a fact about one level's own children.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.enums import CapacityModel, ChildLayout, ChildView
from app.models.storage import ContainerType, Location
from app.services import views
from app.services.tree import location_tree
from tests.factories import make_container_type, make_location

PITCH = 42.0


def _type(db: Session, slug: str, **kwargs: object) -> ContainerType:
    return make_container_type(db, slug=slug, **kwargs)


# ---------------------------------------------------------------------------
# 1. Why this is a third axis and not three more `ChildLayout` members
# ---------------------------------------------------------------------------


def test_child_layout_still_answers_only_the_geometry_question() -> None:
    """The membership of `ChildLayout` is pinned on purpose.

    Growing it is how this feature could have been built, and the next test
    shows what that would have cost. Anyone adding a drawing kind to it has to
    delete this assertion first, which is the point.
    """
    assert {member.value for member in ChildLayout} == {"grid", "list", "none"}


def test_a_view_kind_never_hides_a_container_from_auto_assignment(db: Session) -> None:
    """The mechanical argument for two axes rather than one.

    A cabinet drawn as a face of drawer fronts still *presents* a grid, so
    `assignment`'s "materialise the next empty cell" escalation must still find
    it. Had `cabinet_face` been a `ChildLayout` member instead, this query would
    return nothing and a scan would escalate to the INBOX because somebody
    picked a skin.
    """
    cabinet = _type(
        db,
        "bench-cabinet",
        child_layout=ChildLayout.GRID,
        child_view=ChildView.CABINET_FACE,
        grid_rows=4,
        grid_cols=6,
    )
    make_location(db, name="Workbench cabinet", container_type_id=cabinet.id)
    db.commit()

    # Verbatim the predicate `app.services.assignment._materialize_cell` uses.
    grid_containers = db.execute(
        select(Location.name)
        .join(ContainerType, ContainerType.id == Location.container_type_id)
        .where(ContainerType.child_layout == ChildLayout.GRID)
    ).scalars()
    assert list(grid_containers) == ["Workbench cabinet"]


def test_both_columns_are_plain_varchar(db: Session) -> None:
    """Adding a way to draw a level stays a one-line change in `enums.py`."""
    for table in ("container_types", "locations"):
        declared = {row[1]: row[2] for row in db.execute(text(f"PRAGMA table_info({table})")).all()}
        assert declared["child_view"].upper().startswith("VARCHAR")


def test_a_view_kind_from_a_newer_build_reads_rather_than_raising(db: Session) -> None:
    """The other half of the no-`CHECK` promise: the legal set grows, and an
    older build must draw such a level plainly rather than crash reading it."""
    location = make_location(db, name="drawn by a future release")
    db.commit()
    db.execute(
        text("UPDATE locations SET child_view = 'isometric_exploded' WHERE id = :id"),
        {"id": location.id},
    )
    db.commit()
    db.expire_all()

    fresh = db.get(Location, location.id)
    assert fresh is not None
    assert views.resolve_child_view(fresh, None) == "isometric_exploded"


# ---------------------------------------------------------------------------
# 2. Derivation: what the geometry already says
# ---------------------------------------------------------------------------


def test_a_measured_grid_derives_cells_seen_from_above(db: Session) -> None:
    plate = _type(
        db,
        "plate",
        child_layout=ChildLayout.GRID,
        grid_rows=4,
        grid_cols=4,
        grid_pitch_mm=PITCH,
        capacity_model=CapacityModel.GRID_UNITS,
    )
    assert views.derive_child_view(plate) == ChildView.GRID_CELLS


def test_an_unmeasured_grid_derives_a_cabinet_face(db: Session) -> None:
    """No pitch means the cells are not a known size in millimetres, which is
    what separates an assortment box from a tray."""
    box = _type(db, "assortment", child_layout=ChildLayout.GRID, grid_rows=7, grid_cols=8)
    assert views.derive_child_view(box) == ChildView.CABINET_FACE


def test_a_list_with_a_canvas_derives_a_cabinet_face(db: Session) -> None:
    """The shape both seeded off-the-shelf cabinets are in."""
    raaco = _type(db, "drawer-tower", child_layout=ChildLayout.LIST, grid_rows=30, grid_cols=1)
    assert views.derive_child_view(raaco) == ChildView.CABINET_FACE


def test_a_list_with_no_canvas_derives_rows(db: Session) -> None:
    bag = _type(db, "bag-of-bags", child_layout=ChildLayout.LIST)
    assert views.derive_child_view(bag) == ChildView.LIST


def test_something_that_occupies_a_grid_but_presents_none_derives_rows(db: Session) -> None:
    """A Gridfinity bin: its children are its own dividers, which have an order
    and no geometry worth drawing."""
    bin_type = _type(
        db,
        "bin",
        child_layout=ChildLayout.NONE,
        footprint_cols=2,
        footprint_rows=1,
        footprint_height_u=6,
        grid_pitch_mm=PITCH,
    )
    assert views.derive_child_view(bin_type) == ChildView.LIST


def test_something_that_neither_presents_nor_occupies_a_grid_is_a_floor_plan(db: Session) -> None:
    """A workshop. Its children are placed, not slotted — and a bullet list is
    not a picture of a room."""
    room = _type(db, "workshop", child_layout=ChildLayout.NONE)
    assert views.derive_child_view(room) == ChildView.FLOOR_PLAN


def test_no_container_type_at_all_is_a_floor_plan(db: Session) -> None:
    """Which is what makes the outermost level of the tree fall out of the same
    rule instead of needing a hardcoded default at depth 0."""
    assert views.derive_child_view(None) == ChildView.FLOOR_PLAN
    assert views.resolve_child_view(None, None) == ChildView.FLOOR_PLAN


def test_a_shelf_run_is_authored_and_never_derived(db: Session) -> None:
    """Nothing in the schema distinguishes a shelf from a cabinet, so deriving
    one from a row count would be a guess presented as a drawing. Every other
    member is reachable by derivation; this one is only reachable by saying so.
    """
    derivable = {
        views.derive_child_view(container_type)
        for container_type in (
            None,
            _type(db, "d-plate", child_layout=ChildLayout.GRID, grid_pitch_mm=PITCH),
            _type(db, "d-box", child_layout=ChildLayout.GRID, grid_rows=2, grid_cols=2),
            _type(db, "d-tower", child_layout=ChildLayout.LIST, grid_rows=8, grid_cols=1),
            _type(db, "d-bag", child_layout=ChildLayout.LIST),
            _type(db, "d-bin", child_layout=ChildLayout.NONE, footprint_cols=1),
            _type(db, "d-room", child_layout=ChildLayout.NONE),
        )
    }
    assert ChildView.SHELF_RUN not in derivable
    assert derivable == set(ChildView) - {ChildView.SHELF_RUN}


#: Every seeded type and the drawing its own geometry implies. Spelled out in
#: full rather than sampled: ADR 0006's claim is that the derivation is right for
#: *all* of them, and a sample of five made both the ADR and the migration say
#: "eight seed types" when there are eleven — a number nobody had counted because
#: nothing checked it.
SEED_DRAWINGS = {
    # A declared 42 mm pitch: a tray seen from above.
    "gridfinity-baseplate-2x2": ChildView.GRID_CELLS,
    "gridfinity-baseplate-4x4": ChildView.GRID_CELLS,
    "gridfinity-baseplate-4x6": ChildView.GRID_CELLS,
    # Occupies a footprint, presents no grid: its children are its own dividers.
    "gridfinity-bin-1x1x6": ChildView.LIST,
    "gridfinity-bin-2x1x6": ChildView.LIST,
    "gridfinity-bin-1x1x3": ChildView.LIST,
    "gridfinity-bin-2x2x6": ChildView.LIST,
    "gridfinity-bin-3x2x6": ChildView.LIST,
    # `child_layout='list'` with a canvas: a face of drawer fronts.
    "akro-mils-10144": ChildView.CABINET_FACE,
    "raaco-c8-30": ChildView.CABINET_FACE,
    "raaco-c10-40": ChildView.CABINET_FACE,
}


@pytest.mark.parametrize(("slug", "expected"), sorted(SEED_DRAWINGS.items()))
def test_every_seed_type_derives_the_right_drawing(
    db: Session, slug: str, expected: ChildView
) -> None:
    """Why the migration is a pure column add. Each seed row's geometry already
    says how it should be drawn, so `child_view` lands NULL on all of them and a
    stored copy — free to drift from the geometry it was read off — is avoided.
    """
    seed = db.execute(select(ContainerType).where(ContainerType.slug == slug)).scalar_one()
    assert seed.child_view is None
    assert views.resolve_child_view(None, seed) == expected


def test_the_seed_library_is_exactly_what_that_list_claims(db: Session) -> None:
    """ "Every seed type" has to mean every seed type.

    Without this the test above is a sample wearing the word "every", which is
    how the ADR and the migration both came to describe a library of eleven as
    eight. Adding a seed now fails here until it is listed and its drawing
    asserted, rather than being quietly uncovered.
    """
    seeded = set(
        db.execute(select(ContainerType.slug).where(ContainerType.is_seed.is_(True)))
        .scalars()
        .all()
    )
    assert seeded == set(SEED_DRAWINGS)
    assert len(seeded) == 11


# ---------------------------------------------------------------------------
# 3. The three rungs, and what does not propagate
# ---------------------------------------------------------------------------


def test_the_type_default_beats_the_derivation(db: Session) -> None:
    """ "Every Raaco cabinet draws the same way" is a fact about the type."""
    shelving = _type(
        db,
        "shelving-unit",
        child_layout=ChildLayout.LIST,
        grid_rows=5,
        grid_cols=1,
        child_view=ChildView.SHELF_RUN,
    )
    assert views.derive_child_view(shelving) == ChildView.CABINET_FACE
    assert views.resolve_child_view(None, shelving) == ChildView.SHELF_RUN


def test_the_instance_override_beats_the_type(db: Session) -> None:
    plate = _type(db, "plate", child_layout=ChildLayout.GRID, grid_pitch_mm=PITCH)
    ordinary = make_location(db, name="Plate on the bench", container_type_id=plate.id)
    odd = make_location(
        db,
        name="Plate in a drawer nobody opens",
        container_type_id=plate.id,
        child_view=ChildView.LIST,
    )
    db.commit()

    assert views.resolve_child_view(ordinary, plate) == ChildView.GRID_CELLS
    assert views.resolve_child_view(odd, plate) == ChildView.LIST


def test_a_view_is_not_inherited_down_the_tree(db: Session) -> None:
    """Unlike `esd_safe`, which walks the ancestors because ESD safety really
    does propagate. Choosing a floor plan for a room must not silently redraw
    every drawer inside it.
    """
    room_type = _type(db, "room", child_layout=ChildLayout.NONE)
    cabinet_type = _type(db, "cab", child_layout=ChildLayout.GRID, grid_rows=4, grid_cols=2)

    room = make_location(db, name="Workshop", container_type_id=room_type.id)
    room.child_view = ChildView.FLOOR_PLAN
    cabinet = make_location(
        db, name="Cabinet", container_type_id=cabinet_type.id, parent_id=room.id
    )
    db.flush()
    location_tree(db).rebuild_paths()
    db.commit()

    assert views.resolve_child_view(room, room_type) == ChildView.FLOOR_PLAN
    assert cabinet.child_view is None
    assert views.resolve_child_view(cabinet, cabinet_type) == ChildView.CABINET_FACE


def test_resolving_a_whole_tree_agrees_with_resolving_one_node(db: Session) -> None:
    """The batched helper the tree route uses is the same rule, not a second
    copy of it — a divergence would show as a map drawn differently from the
    container screen it drills into."""
    plate_type = _type(db, "plate", child_layout=ChildLayout.GRID, grid_pitch_mm=PITCH)
    room_type = _type(db, "room", child_layout=ChildLayout.NONE)

    room = make_location(db, name="Shop", container_type_id=room_type.id)
    plate = make_location(db, name="Plate", container_type_id=plate_type.id, parent_id=room.id)
    override = make_location(
        db, name="Odd plate", container_type_id=plate_type.id, child_view=ChildView.LIST
    )
    loose = make_location(db, name="Untyped corner")
    db.flush()
    location_tree(db).rebuild_paths()
    db.commit()

    rows = [room, plate, override, loose]
    batched = views.resolve_child_views(db, rows)
    assert batched == {
        room.id: ChildView.FLOOR_PLAN,
        plate.id: ChildView.GRID_CELLS,
        override.id: ChildView.LIST,
        loose.id: ChildView.FLOOR_PLAN,
    }


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def test_the_tree_reports_the_resolved_view_for_every_node(client: TestClient) -> None:
    created = client.post(
        "/api/container-types",
        json={
            "slug": "api-plate",
            "display_name": "Plate",
            "child_layout": "grid",
            "grid_pitch_mm": PITCH,
            "grid_rows": 4,
            "grid_cols": 4,
        },
    )
    assert created.status_code == 201, created.text
    plate_type_id = created.json()["container_type"]["id"]

    room = client.post("/api/locations", json={"name": "Workshop"})
    assert room.status_code == 201, room.text
    room_id = room.json()["location"]["id"]
    plate = client.post(
        "/api/locations",
        json={"name": "Plate", "parent_id": room_id, "container_type_id": plate_type_id},
    )
    assert plate.status_code == 201, plate.text
    plate_id = plate.json()["location"]["id"]

    nodes = {node["id"]: node for node in client.get("/api/locations/tree").json()["nodes"]}
    assert nodes[room_id]["effective_child_view"] == "floor_plan"
    assert nodes[plate_id]["effective_child_view"] == "grid_cells"


def test_a_type_reports_the_pin_and_the_resolved_value_separately(client: TestClient) -> None:
    """An editor cannot offer "stop pinning this" without being able to tell a
    pin from a derivation that happens to agree with it."""
    created = client.post(
        "/api/container-types",
        json={
            "slug": "api-tower",
            "display_name": "Tower",
            "child_layout": "list",
            "grid_rows": 30,
            "grid_cols": 1,
        },
    )
    assert created.status_code == 201, created.text
    type_id = created.json()["container_type"]["id"]
    body = created.json()["container_type"]
    assert body["child_view"] is None
    assert body["effective_child_view"] == "cabinet_face"

    pinned = client.patch(
        f"/api/container-types/{type_id}",
        json={"child_view": "shelf_run", "client_op_id": "pin-1"},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["container_type"]["child_view"] == "shelf_run"
    assert pinned.json()["container_type"]["effective_child_view"] == "shelf_run"

    # An explicit null is an edit, not an omission: it hands the drawing back.
    cleared = client.patch(
        f"/api/container-types/{type_id}",
        json={"child_view": None, "client_op_id": "clear-1"},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["container_type"]["child_view"] is None
    assert cleared.json()["container_type"]["effective_child_view"] == "cabinet_face"


def test_an_instance_can_pin_and_unpin_its_own_view(client: TestClient) -> None:
    created = client.post(
        "/api/container-types",
        json={
            "slug": "api-plate-2",
            "display_name": "Plate",
            "child_layout": "grid",
            "grid_pitch_mm": PITCH,
        },
    )
    assert created.status_code == 201, created.text
    type_id = created.json()["container_type"]["id"]
    location = client.post("/api/locations", json={"name": "Plate", "container_type_id": type_id})
    assert location.status_code == 201, location.text
    location_id = location.json()["location"]["id"]
    assert location.json()["location"]["child_view"] is None
    assert location.json()["location"]["effective_child_view"] == "grid_cells"

    pinned = client.put(
        f"/api/locations/{location_id}/child-view",
        json={"child_view": "list", "client_op_id": "loc-pin-1"},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == {
        "location_id": location_id,
        "child_view": "list",
        "effective_child_view": "list",
        "replayed": False,
    }

    cleared = client.put(
        f"/api/locations/{location_id}/child-view",
        json={"child_view": None, "client_op_id": "loc-clear-1"},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["child_view"] is None
    assert cleared.json()["effective_child_view"] == "grid_cells"


def test_setting_a_view_on_creation_is_carried_through(client: TestClient) -> None:
    created = client.post(
        "/api/locations", json={"name": "Shelving by the door", "child_view": "shelf_run"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["location"]["child_view"] == "shelf_run"
    assert created.json()["location"]["effective_child_view"] == "shelf_run"


def test_an_unknown_view_kind_is_refused_at_the_edge(client: TestClient) -> None:
    """The set grows by a release, not by a request body: `StrEnumType` and the
    request model both validate, and only a row already in the database gets the
    benefit of the doubt (see the pass-through test above)."""
    created = client.post("/api/locations", json={"name": "Nope", "child_view": "hologram"})
    assert created.status_code == 422


def test_cloning_a_type_carries_its_pinned_view(client: TestClient) -> None:
    """The clone path is how a seed type is edited at all, so a clone that lost
    the pin would silently redraw every cabinet stamped from the copy."""
    created = client.post(
        "/api/container-types",
        json={
            "slug": "api-shelving",
            "display_name": "Shelving",
            "child_layout": "list",
            "grid_rows": 5,
            "grid_cols": 1,
            "child_view": "shelf_run",
        },
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["container_type"]["id"]

    clone = client.post(f"/api/container-types/{source_id}/clone", json={"client_op_id": "clone-1"})
    assert clone.status_code == 201, clone.text
    assert clone.json()["container_type"]["child_view"] == "shelf_run"
