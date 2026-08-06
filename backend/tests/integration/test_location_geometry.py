"""`LocationRead.geometry` — how big the container actually is, in millimetres.

The dimensions were always in the schema, on `container_types`, and the detail
response carried only `container_type_id` to reach them by. Anything that could
not follow that integer therefore could not answer "will this part fit in that
drawer" — most sharply the MCP tool surface, where the container-type routes are
excluded as geometry *authoring* (`mcpserver/almagest_mcp/coverage.py`) and a
model was left with a fill percentage and no envelope.

Three properties are worth pinning, and none of them is "the fields exist":

* **The raw envelope is not the capacity.** `inner_volume_mm3` is the plain
  product of the three inner dimensions; the volume capacity model multiplies
  that by `fill_factor`. Conflating them overstates usable space by ~45% at the
  default, so the arithmetic between the two numbers is asserted, not described.
* **Null means unmeasured, never zero.** Most container types in a real setup
  have never had a tape measure taken to them, and a smuggled zero would read as
  "nothing fits" — the same rule `CapacitySnapshot.capacity` already follows.
* **Malformed data does not break the read.** `allowed_part_kinds_json` is a text
  blob, and `services.assignment.part_kind_allowed` treats an unparseable one as
  "no restriction" so it can never block a scan. A detail read that 500s on the
  data put-away shrugs at would be the stricter of the two.

Real Alembic migrations, per `tests/conftest.py`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import CapacityModel
from app.services.tree import location_tree
from tests.factories import make_container_type, make_location


def _geometry(client: TestClient, location_id: int) -> dict[str, object]:
    response = client.get(f"/api/locations/{location_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    geometry = payload["geometry"]
    assert isinstance(geometry, dict)
    return geometry


def _placed(db: Session, container_type_id: int | None, **kwargs: object) -> int:
    location = make_location(db, container_type_id=container_type_id, **kwargs)
    location_tree(db).rebuild_paths()
    db.commit()
    return location.id


def test_a_measured_drawer_reports_the_millimetres_it_was_measured_in(
    client: TestClient, db: Session
) -> None:
    """The dead end this exists to remove: dimensions without a second lookup."""
    container_type = make_container_type(
        db,
        slug="raaco-a75",
        display_name="Raaco A75 drawer",
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=150.0,
        inner_width_mm=100.0,
        inner_height_mm=40.0,
        max_item_dimension_mm=95.0,
    )
    geometry = _geometry(client, _placed(db, container_type.id, name="Drawer B2"))

    assert geometry["container_type_slug"] == "raaco-a75"
    assert geometry["container_type_display_name"] == "Raaco A75 drawer"
    assert geometry["inner_length_mm"] == 150.0
    assert geometry["inner_width_mm"] == 100.0
    assert geometry["inner_height_mm"] == 40.0
    assert geometry["max_item_dimension_mm"] == 95.0
    assert geometry["inner_volume_mm3"] == 150.0 * 100.0 * 40.0


def test_the_envelope_is_the_raw_product_and_the_capacity_is_the_discounted_one(
    client: TestClient, db: Session
) -> None:
    """The one number pair that would be silently, expensively wrong if swapped.

    `inner_volume_mm3` is what a tape measure gives; `capacity.capacity` is what
    the container will really take once packing loss is allowed for. Asserting the
    factor between them is what stops a consumer quoting either as the other.
    """
    container_type = make_container_type(
        db,
        slug="measured-bin",
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=100.0,
        inner_width_mm=50.0,
        inner_height_mm=20.0,
        default_fill_factor=0.5,
    )
    location_id = _placed(db, container_type.id)

    payload = client.get(f"/api/locations/{location_id}").json()
    geometry, reported_capacity = payload["geometry"], payload["capacity"]

    assert geometry["inner_volume_mm3"] == 100_000.0
    assert geometry["fill_factor"] == 0.5
    assert reported_capacity["capacity"] == 50_000.0
    assert reported_capacity["capacity"] == geometry["inner_volume_mm3"] * geometry["fill_factor"]


def test_a_location_override_is_the_fill_factor_that_gets_reported(
    client: TestClient, db: Session
) -> None:
    """Resolved, not the type's raw default — `capacity.container_inputs` owns
    that chain, and reporting the unresolved value would disagree with the
    capacity printed beside it."""
    container_type = make_container_type(
        db,
        slug="overridden-bin",
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=10.0,
        inner_width_mm=10.0,
        inner_height_mm=10.0,
        default_fill_factor=0.55,
    )
    location_id = _placed(db, container_type.id, fill_factor=0.25)

    payload = client.get(f"/api/locations/{location_id}").json()
    assert payload["geometry"]["fill_factor"] == 0.25
    assert payload["capacity"]["capacity"] == 1000.0 * 0.25


def test_an_unmeasured_container_says_so_instead_of_saying_zero(
    client: TestClient, db: Session
) -> None:
    """A zero here reads as "nothing fits", which is a different claim from
    "nobody has measured this drawer" — and the second is the common one."""
    container_type = make_container_type(db, slug="unmeasured")
    geometry = _geometry(client, _placed(db, container_type.id))

    assert geometry["inner_length_mm"] is None
    assert geometry["inner_width_mm"] is None
    assert geometry["inner_height_mm"] is None
    assert geometry["inner_volume_mm3"] is None
    assert geometry["max_item_dimension_mm"] is None
    assert geometry["allowed_part_kinds"] is None


def test_two_dimensions_out_of_three_is_not_a_volume(client: TestClient, db: Session) -> None:
    """A partially measured type must not have its missing depth read as 1 mm —
    or as 0. The product is all three or it is nothing."""
    container_type = make_container_type(
        db,
        slug="half-measured",
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=120.0,
        inner_width_mm=80.0,
    )
    geometry = _geometry(client, _placed(db, container_type.id))

    assert geometry["inner_length_mm"] == 120.0
    assert geometry["inner_height_mm"] is None
    assert geometry["inner_volume_mm3"] is None


def test_a_room_with_no_container_type_has_no_geometry_at_all(
    client: TestClient, db: Session
) -> None:
    """Null geometry is a real state, not an omission: a room and a bare shelf
    have no type to be measured through."""
    response = client.get(f"/api/locations/{_placed(db, None, name='Workshop')}")
    assert response.status_code == 200, response.text
    assert response.json()["geometry"] is None


def test_a_restricted_container_names_the_kinds_it_takes(client: TestClient, db: Session) -> None:
    container_type = make_container_type(
        db,
        slug="esd-tray",
        allowed_part_kinds_json='["ic", "transistor"]',
    )
    geometry = _geometry(client, _placed(db, container_type.id))
    assert geometry["allowed_part_kinds"] == ["ic", "transistor"]


def test_a_malformed_restriction_reads_as_no_restriction_rather_than_a_500(
    client: TestClient, db: Session
) -> None:
    """`services.assignment.part_kind_allowed` shrugs at an unparseable blob so
    it can never block a put-away. This read has to be no stricter than the write
    path it describes."""
    container_type = make_container_type(
        db,
        slug="corrupt-tray",
        allowed_part_kinds_json="{not json at all",
    )
    geometry = _geometry(client, _placed(db, container_type.id))
    assert geometry["allowed_part_kinds"] is None


def test_an_empty_restriction_list_is_also_no_restriction(client: TestClient, db: Session) -> None:
    """`part_kind_allowed` treats `[]` as "everything", so reporting an empty list
    would invite a consumer to conclude the opposite — that nothing may go in."""
    container_type = make_container_type(db, slug="empty-tray", allowed_part_kinds_json="[]")
    geometry = _geometry(client, _placed(db, container_type.id))
    assert geometry["allowed_part_kinds"] is None


def test_a_gridfinity_bin_reports_the_footprint_it_occupies_and_the_grid_it_offers(
    client: TestClient, db: Session
) -> None:
    """ADR 0002's two independent questions, both answerable from one read: what
    this bin takes up in its baseplate, and what pitch it presents to its own
    dividers."""
    container_type = make_container_type(
        db,
        slug="gridfinity-2x1x6",
        display_name="Gridfinity 2x1x6u",
        grid_pitch_mm=42.0,
        grid_height_unit_mm=7.0,
        footprint_cols=2,
        footprint_rows=1,
        footprint_height_u=6,
    )
    geometry = _geometry(client, _placed(db, container_type.id))

    assert geometry["footprint_cols"] == 2
    assert geometry["footprint_rows"] == 1
    assert geometry["footprint_height_u"] == 6
    assert geometry["grid_pitch_mm"] == 42.0
    assert geometry["grid_height_unit_mm"] == 7.0
