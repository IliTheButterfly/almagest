"""Regressions for the four defects adversarial review found on this branch.

Each was reproduced before being fixed, and each is here rather than tucked into
the suite for the feature it belongs to, because what they have in common is more
instructive than what they are individually: three of the four turned malformed
or duplicate *input* into an unhandled exception and therefore a bare 500, and
the fourth was two code paths answering the same question with different answers.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.maintenance import rebuild_location_occupancy
from app.models.enums import CapacityModel, ChildLayout
from app.models.storage import ContainerType, Location, LocationOccupancy
from app.services import capacity
from app.services.layout_authoring import LayoutError, merge_type_region
from app.services.tree import location_tree


def _grid_type(client: TestClient, slug: str, rows: int = 1, cols: int = 2) -> int:
    response = client.post(
        "/api/container-types",
        json={
            "slug": slug,
            "display_name": slug,
            "child_layout": "grid",
            "grid_rows": rows,
            "grid_cols": cols,
            "capacity_model": "slots",
            "capacity_slots": rows * cols,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["container_type"]["id"])


def _location(client: TestClient, name: str) -> int:
    response = client.post("/api/locations", json={"name": name})
    assert response.status_code == 201, response.text
    return int(response.json()["location"]["id"])


# ---------------------------------------------------------------------------
# 1. naming_pattern is client text handed to str.format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        "Cabinet {n} {oops}",  # unknown placeholder -> KeyError
        "Cabinet {n} {0}",  # positional -> IndexError
        "Cabinet {n} {",  # unbalanced brace -> ValueError
    ],
)
def test_a_malformed_naming_pattern_is_422_not_500(client: TestClient, pattern: str) -> None:
    """Only `{n}` is substituted, so anything else is a malformed pattern and the
    user needs telling which — not an unhandled exception."""
    type_id = _grid_type(client, "naming-probe")
    parent = _location(client, "Naming probe root")

    response = client.post(
        f"/api/locations/{parent}/instantiate",
        json={"container_type_id": type_id, "count": 1, "naming_pattern": pattern},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "bad_naming_pattern"


def test_a_valid_naming_pattern_still_substitutes(client: TestClient) -> None:
    """The fix must not have broken the feature."""
    type_id = _grid_type(client, "naming-ok")
    parent = _location(client, "Naming ok root")

    response = client.post(
        f"/api/locations/{parent}/instantiate",
        json={"container_type_id": type_id, "count": 2, "naming_pattern": "Cabinet {n}"},
    )
    assert response.status_code in {200, 201}, response.text
    assert "Cabinet 1" in response.text
    assert "Cabinet 2" in response.text


# ---------------------------------------------------------------------------
# 2. A duplicate slug reached the UNIQUE constraint
# ---------------------------------------------------------------------------


def test_a_duplicate_container_type_slug_is_409_not_500(client: TestClient) -> None:
    """Checked before the insert rather than caught after: `idempotency.run`
    rolls back on IntegrityError to absorb a duplicate *client_op_id*, so a slug
    collision reaching the same handler conflated two unrelated conditions."""
    _grid_type(client, "collide")

    response = client.post(
        "/api/container-types",
        json={"slug": "collide", "display_name": "Second", "capacity_model": "none"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "duplicate_slug"


def test_a_distinct_slug_still_creates(client: TestClient) -> None:
    _grid_type(client, "distinct-a")
    _grid_type(client, "distinct-b")


# ---------------------------------------------------------------------------
# 3. The bulk occupancy rebuild ignored grid_units
# ---------------------------------------------------------------------------


def _plate_with_wide_bin(db: Session) -> Location:
    plate_type = ContainerType(
        slug="rebuild-plate",
        display_name="plate",
        child_layout=ChildLayout.GRID,
        grid_cols=2,
        grid_rows=1,
        grid_pitch_mm=42.0,
        capacity_model=CapacityModel.GRID_UNITS,
    )
    bin_type = ContainerType(
        slug="rebuild-bin",
        display_name="bin",
        footprint_cols=2,
        footprint_rows=1,
        grid_pitch_mm=42.0,
        capacity_model=CapacityModel.VOLUME,
    )
    db.add_all([plate_type, bin_type])
    db.flush()

    plate = Location(name="Plate", container_type_id=plate_type.id)
    db.add(plate)
    db.flush()
    db.add(Location(name="Wide bin", parent_id=plate.id, container_type_id=bin_type.id))
    db.flush()
    location_tree(db).rebuild_paths()
    return plate


def test_the_bulk_rebuild_agrees_with_the_single_location_read(db: Session) -> None:
    """The defect: the read said used=2.0/full, the rebuild said used=0.0.

    The rebuild is the path that *persists* `location_occupancy` and sets
    `is_overfull`, so the wrong answer was the one that stuck. A cache being
    reconstructible only helps if the reconstruction is the correct one.
    """
    plate = _plate_with_wide_bin(db)
    db.commit()

    read = capacity.compute_location_snapshot(db, plate)
    rebuild_location_occupancy(db)
    db.commit()

    cached = db.get(LocationOccupancy, plate.id)
    assert cached is not None
    assert cached.used == read.used == 2.0
    assert cached.capacity == read.capacity == 2.0


def test_an_overfull_grid_is_flagged_by_the_rebuild(db: Session) -> None:
    """Consequence of the same bug: a baseplate could never be flagged overfull,
    so no defrag suggestion was ever generated for one."""
    plate = _plate_with_wide_bin(db)
    extra_type = ContainerType(
        slug="rebuild-extra",
        display_name="extra",
        footprint_cols=1,
        footprint_rows=1,
        grid_pitch_mm=42.0,
        capacity_model=CapacityModel.VOLUME,
    )
    db.add(extra_type)
    db.flush()
    db.add(Location(name="One too many", parent_id=plate.id, container_type_id=extra_type.id))
    db.flush()
    location_tree(db).rebuild_paths()
    db.commit()

    rebuild_location_occupancy(db)
    db.commit()
    db.refresh(plate)

    cached = db.get(LocationOccupancy, plate.id)
    assert cached is not None
    assert cached.used == 3.0  # two units plus one, on a two-unit plate
    assert plate.is_overfull is True


def test_the_bulk_helper_matches_the_per_location_one(db: Session) -> None:
    """Two implementations of the same sum is how they drifted in the first
    place, so they are pinned against each other."""
    plate = _plate_with_wide_bin(db)
    db.commit()

    assert capacity.all_consumed_grid_units(db).get(plate.id, 0) == capacity.consumed_grid_units(
        db, plate.id
    )


# ---------------------------------------------------------------------------
# 4. merge_type_region skipped the validation its sibling performs
# ---------------------------------------------------------------------------


def test_a_merge_with_a_colliding_label_is_refused(db: Session) -> None:
    """An explicit label for the merged region can collide with an untouched slot
    elsewhere on the canvas. Refused cleanly rather than reaching the
    UNIQUE(container_type_id, slot_label) constraint as an IntegrityError."""
    container_type = ContainerType(
        slug="merge-collide",
        display_name="merge collide",
        child_layout=ChildLayout.GRID,
        grid_rows=2,
        grid_cols=2,
        capacity_model=CapacityModel.SLOTS,
        capacity_slots=4,
    )
    db.add(container_type)
    db.flush()

    with pytest.raises(LayoutError) as excinfo:
        merge_type_region(
            db,
            container_type,
            row_idx=0,
            col_idx=0,
            row_span=1,
            col_span=2,
            slot_label="B1",  # already owned by the untouched second row
        )
    assert excinfo.value.reason == "duplicate_label"


def test_an_ordinary_merge_still_succeeds(db: Session) -> None:
    container_type = ContainerType(
        slug="merge-ok",
        display_name="merge ok",
        child_layout=ChildLayout.GRID,
        grid_rows=2,
        grid_cols=2,
        capacity_model=CapacityModel.SLOTS,
        capacity_slots=4,
    )
    db.add(container_type)
    db.flush()

    merge_type_region(db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=2)
    db.flush()
    assert container_type.materialize_slots is True


# ---------------------------------------------------------------------------
# The shape of all four
# ---------------------------------------------------------------------------


def test_no_layout_route_returns_a_500_for_hostile_input(client: TestClient) -> None:
    """Three of the four defects were malformed input becoming an unhandled
    exception. This sweeps the layout routes for the same shape."""
    type_id = _grid_type(client, "hostile-probe")
    parent = _location(client, "Hostile probe root")

    cases: list[tuple[str, dict[str, object]]] = [
        (
            f"/api/locations/{parent}/instantiate",
            {"container_type_id": type_id, "count": 1, "naming_pattern": "{"},
        ),
        (
            f"/api/locations/{parent}/instantiate",
            {"container_type_id": type_id, "count": 1, "naming_pattern": "{n:{n}}"},
        ),
        ("/api/container-types", {"slug": "hostile-probe", "display_name": "dup"}),
        ("/api/container-types", {"slug": "", "display_name": "empty slug"}),
    ]
    for path, body in cases:
        response = client.post(path, json=body)
        assert response.status_code < 500, f"{path} {body} -> {response.status_code}"
