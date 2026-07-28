"""Recursive container types — ADR 0002.

The property under test is the one the ADR is about: a container type answers
"what grid do I present?" and "what footprint do I occupy?" **independently**, so
a type can be both a child and a parent. Every level of a stacked Gridfinity
setup is exactly that, and a schema that conflated the two questions could not
express it.

Stacking gets no special machinery here on purpose — a bin's top face is a
mounting surface, so a stacked bin is an ordinary child in the `locations` tree.
If that claim is wrong, `test_bins_stack_as_ordinary_children` fails.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.enums import CapacityModel, ChildLayout
from app.models.storage import ContainerType, Location
from app.services import capacity
from app.services.capacity import (
    ContainerCapacityInputs,
    GridUnitsCapacityStrategy,
    consumed_grid_units,
    grid_incompatibility,
)
from app.services.tree import location_tree

#: Verified Gridfinity spec: 42 mm grid pitch, 7 mm height unit.
PITCH = 42.0
HEIGHT_UNIT = 7.0


def _baseplate_type(db: Session, cols: int, rows: int, slug: str = "gf-plate") -> ContainerType:
    container_type = ContainerType(
        slug=slug,
        display_name=f"Gridfinity {cols}x{rows} baseplate",
        child_layout=ChildLayout.GRID,
        grid_cols=cols,
        grid_rows=rows,
        grid_pitch_mm=PITCH,
        grid_height_unit_mm=HEIGHT_UNIT,
        capacity_model=CapacityModel.GRID_UNITS,
    )
    db.add(container_type)
    db.flush()
    return container_type


def _bin_type(
    db: Session,
    cols: int,
    rows: int,
    height_u: int = 6,
    slug: str | None = None,
    *,
    presents_grid: tuple[int, int] | None = None,
    pitch: float | None = PITCH,
) -> ContainerType:
    """A bin that occupies `cols x rows` units, and optionally presents its own
    grid of internal dividers — which is the whole point of the ADR."""
    container_type = ContainerType(
        slug=slug or f"gf-bin-{cols}x{rows}x{height_u}",
        display_name=f"Gridfinity {cols}x{rows}x{height_u}u bin",
        footprint_cols=cols,
        footprint_rows=rows,
        footprint_height_u=height_u,
        grid_pitch_mm=pitch,
        grid_height_unit_mm=HEIGHT_UNIT,
        child_layout=ChildLayout.GRID if presents_grid else ChildLayout.NONE,
        grid_cols=presents_grid[0] if presents_grid else None,
        grid_rows=presents_grid[1] if presents_grid else None,
        capacity_model=CapacityModel.GRID_UNITS if presents_grid else CapacityModel.VOLUME,
    )
    db.add(container_type)
    db.flush()
    return container_type


def _place(
    db: Session, name: str, container_type: ContainerType, parent: Location | None
) -> Location:
    location = Location(
        name=name,
        container_type_id=container_type.id,
        parent_id=parent.id if parent else None,
    )
    db.add(location)
    db.flush()
    location_tree(db).rebuild_paths()
    return location


# ---------------------------------------------------------------------------
# The two questions are independent
# ---------------------------------------------------------------------------


def test_a_bin_is_both_a_child_and_a_parent(db: Session) -> None:
    """The ADR's central claim. A 2x1 bin occupies two units of its baseplate AND
    presents its own 1x3 grid of dividers; those are unrelated facts."""
    plate_type = _baseplate_type(db, cols=4, rows=4)
    bin_type = _bin_type(db, cols=2, rows=1, presents_grid=(1, 3))

    # As a child: it occupies a footprint in its parent's grid.
    assert bin_type.footprint_cols == 2
    assert bin_type.footprint_rows == 1
    # As a parent: it presents a grid of its own, unrelated to that footprint.
    assert (bin_type.grid_cols, bin_type.grid_rows) == (1, 3)
    # And the plate's grid says nothing about what it sits in.
    assert plate_type.footprint_cols is None


def test_the_chain_recurses_to_arbitrary_depth(db: Session) -> None:
    """cabinet -> drawer -> baseplate -> bin -> divider, and nothing in the
    schema knows how deep it is."""
    cabinet_type = _baseplate_type(db, cols=1, rows=4, slug="cabinet")
    drawer_type = _bin_type(db, cols=1, rows=1, slug="drawer", presents_grid=(6, 4))
    plate_type = _bin_type(db, cols=6, rows=4, slug="plate", presents_grid=(6, 4))
    bin_type = _bin_type(db, cols=2, rows=1, slug="bin", presents_grid=(1, 3))
    divider_type = _bin_type(db, cols=1, rows=1, slug="divider")

    cabinet = _place(db, "Cabinet", cabinet_type, None)
    drawer = _place(db, "Drawer 1", drawer_type, cabinet)
    plate = _place(db, "Baseplate", plate_type, drawer)
    bin_ = _place(db, "Bin A", bin_type, plate)
    divider = _place(db, "Divider 1", divider_type, bin_)
    db.commit()

    assert divider.depth == 4
    assert divider.label_path == "Cabinet / Drawer 1 / Baseplate / Bin A / Divider 1"
    # The tree is the recursion — no stack table, no depth column to bump.
    assert [node.name for node in location_tree(db).ancestors(divider)] == [
        "Cabinet",
        "Drawer 1",
        "Baseplate",
        "Bin A",
    ]


def test_bins_stack_as_ordinary_children(db: Session) -> None:
    """Stacking needs no new machinery: a bin's top face is a mounting surface,
    so a stacked bin is a child of the bin below it."""
    plate_type = _baseplate_type(db, cols=4, rows=4)
    stackable = _bin_type(db, cols=1, rows=1, height_u=3, slug="stackable", presents_grid=(1, 1))

    plate = _place(db, "Plate", plate_type, None)
    lower = _place(db, "Lower bin", stackable, plate)
    upper = _place(db, "Upper bin", stackable, lower)
    db.commit()

    assert upper.parent_id == lower.id
    assert upper.label_path == "Plate / Lower bin / Upper bin"
    # The plate still sees only one direct child; the stack is not double-counted
    # against its footprint.
    assert consumed_grid_units(db, plate.id) == 1


# ---------------------------------------------------------------------------
# grid_units measures area, not compartments
# ---------------------------------------------------------------------------


def test_a_two_by_one_bin_consumes_two_units(db: Session) -> None:
    """The reason this is not the `slots` model. Counting compartments would call
    a half-covered plate nearly empty."""
    plate_type = _baseplate_type(db, cols=4, rows=4)
    plate = _place(db, "Plate", plate_type, None)
    _place(db, "Wide bin", _bin_type(db, cols=2, rows=1), plate)
    db.commit()

    assert consumed_grid_units(db, plate.id) == 2


def test_occupancy_sums_child_footprints(db: Session) -> None:
    plate_type = _baseplate_type(db, cols=4, rows=4)
    plate = _place(db, "Plate", plate_type, None)
    _place(db, "a", _bin_type(db, cols=2, rows=1, slug="b21"), plate)
    _place(db, "b", _bin_type(db, cols=1, rows=1, slug="b11"), plate)
    _place(db, "c", _bin_type(db, cols=3, rows=2, slug="b32"), plate)
    db.commit()

    assert consumed_grid_units(db, plate.id) == 2 + 1 + 6

    snapshot = capacity.compute_location_snapshot(db, plate)
    assert snapshot.model == CapacityModel.GRID_UNITS
    assert snapshot.capacity == 16.0
    assert snapshot.used == 9.0
    assert snapshot.fill_ratio == pytest.approx(9 / 16)
    assert snapshot.unit == "units"


def test_a_child_without_a_declared_footprint_counts_as_one(db: Session) -> None:
    """Conservative default. Counting it as zero would let a plate accumulate
    unlimited untyped children and still report itself empty."""
    plate_type = _baseplate_type(db, cols=2, rows=2)
    plate = _place(db, "Plate", plate_type, None)

    untyped = Location(name="loose thing", parent_id=plate.id)
    db.add(untyped)
    db.commit()

    assert consumed_grid_units(db, plate.id) == 1


def test_a_full_plate_is_flagged_but_never_blocks(db: Session) -> None:
    """Capacity stays advisory. Physically a bin either seats or it does not, but
    the database finding out second is not a reason to reject a scan."""
    plate_type = _baseplate_type(db, cols=2, rows=1)
    plate = _place(db, "Small plate", plate_type, None)
    _place(db, "fills it", _bin_type(db, cols=2, rows=1), plate)
    db.commit()

    assert capacity.compute_location_snapshot(db, plate).is_full is True

    # …and adding another still succeeds.
    _place(db, "one too many", _bin_type(db, cols=1, rows=1, slug="extra"), plate)
    db.commit()

    over = capacity.compute_location_snapshot(db, plate)
    assert over.used == 3.0
    assert over.capacity == 2.0
    assert over.is_full is True


def test_a_plate_with_no_declared_grid_has_no_capacity(db: Session) -> None:
    """Informational rather than a divide-by-zero."""
    inputs = ContainerCapacityInputs(
        capacity_model=CapacityModel.GRID_UNITS,
        capacity_slots=None,
        max_parts_per_slot=None,
        inner_length_mm=None,
        inner_width_mm=None,
        inner_height_mm=None,
        fill_factor=0.55,
        full_threshold=0.9,
        grid_rows=None,
        grid_cols=None,
        consumed_grid_units=3,
    )
    snapshot = GridUnitsCapacityStrategy().snapshot(inputs, [])
    assert snapshot.capacity is None
    assert snapshot.fill_ratio is None
    assert snapshot.is_full is False


# ---------------------------------------------------------------------------
# Pitch is the one hard geometric constraint
# ---------------------------------------------------------------------------


def test_a_matching_pitch_is_compatible(db: Session) -> None:
    plate = _baseplate_type(db, cols=4, rows=4)
    good = _bin_type(db, cols=2, rows=1)
    assert grid_incompatibility(plate, good) is None


def test_a_pitch_mismatch_is_refused(db: Session) -> None:
    """Unlike capacity, this is not a preference a defrag can tidy up later: a
    42 mm bin does not physically seat on a 50 mm plate. Capacity being over is a
    bin that looks full; pitch being wrong is a bin on the floor."""
    plate = _baseplate_type(db, cols=4, rows=4)
    wrong = _bin_type(db, cols=1, rows=1, slug="metric-ish", pitch=50.0)
    assert grid_incompatibility(plate, wrong) == "pitch_mismatch"


def test_a_footprint_larger_than_the_grid_is_refused(db: Session) -> None:
    plate = _baseplate_type(db, cols=2, rows=2)
    too_wide = _bin_type(db, cols=5, rows=1, slug="too-wide")
    too_deep = _bin_type(db, cols=1, rows=5, slug="too-deep")

    assert grid_incompatibility(plate, too_wide) == "footprint_too_wide"
    assert grid_incompatibility(plate, too_deep) == "footprint_too_deep"


def test_a_non_grid_container_is_never_incompatible(db: Session) -> None:
    """An Akro-Mils or Raaco cabinet leaves the pitch columns NULL and keeps
    using slot templates. Gridfinity is the reference case, not a privileged
    one — non-grid storage must not acquire a new failure mode."""
    irregular = ContainerType(
        slug="akro-mils-10144",
        display_name="Akro-Mils 10144",
        child_layout=ChildLayout.LIST,
        capacity_model=CapacityModel.SLOTS,
        capacity_slots=48,
    )
    db.add(irregular)
    db.flush()

    drawer = _bin_type(db, cols=1, rows=1, slug="a-drawer", pitch=None)
    assert grid_incompatibility(irregular, drawer) is None
    assert grid_incompatibility(None, drawer) is None


def test_float_pitch_comparison_tolerates_representation(db: Session) -> None:
    """41.999999999 mm is the same plate as 42 mm; an exact float compare would
    reject a value that round-tripped through JSON."""
    plate = _baseplate_type(db, cols=4, rows=4)
    nearly = _bin_type(db, cols=1, rows=1, slug="nearly", pitch=42.0 + 1e-9)
    assert grid_incompatibility(plate, nearly) is None
