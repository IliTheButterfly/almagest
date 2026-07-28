"""Capacity strategies and the item-dimension cascade — pure functions, no
session required. Every strategy is exercised directly against
`ContainerCapacityInputs`/`OccupantLot`, mirroring how `app.services.capacity`
is actually called from the bulk occupancy rebuild.
"""

from __future__ import annotations

import pytest

from app.models.enums import CapacityModel, SizeClass, VolumeSource
from app.services import capacity
from app.services.capacity import (
    CapacitySnapshot,
    ContainerCapacityInputs,
    OccupantLot,
    cascade_unit_volume_mm3,
    get_strategy,
    lot_volume_mm3,
)


def _inputs(**overrides: object) -> ContainerCapacityInputs:
    defaults: dict[str, object] = {
        "capacity_model": CapacityModel.NONE,
        "capacity_slots": None,
        "max_parts_per_slot": None,
        "inner_length_mm": None,
        "inner_width_mm": None,
        "inner_height_mm": None,
        "fill_factor": 0.55,
        "full_threshold": 0.9,
    }
    defaults.update(overrides)
    return ContainerCapacityInputs(**defaults)  # type: ignore[arg-type]


def _lot(
    *,
    lot_id: int = 1,
    part_id: int = 1,
    qty_milli: int = 1000,
    packaging_volume_mm3: float | None = None,
    packaging_pitch_mm: float | None = None,
    unit_volume_mm3: float | None = None,
) -> OccupantLot:
    return OccupantLot(
        lot_id=lot_id,
        part_id=part_id,
        qty_milli=qty_milli,
        packaging_volume_mm3=packaging_volume_mm3,
        packaging_pitch_mm=packaging_pitch_mm,
        unit_volume_mm3=unit_volume_mm3,
    )


# ---------------------------------------------------------------------------
# `none`
# ---------------------------------------------------------------------------


def test_none_model_is_never_full_regardless_of_occupants() -> None:
    strategy = get_strategy(CapacityModel.NONE)
    snapshot = strategy.snapshot(_inputs(), [_lot() for _ in range(50)])
    assert snapshot.capacity is None
    assert snapshot.is_full is False
    assert snapshot.is_overfull is False


# ---------------------------------------------------------------------------
# `slots`
# ---------------------------------------------------------------------------


def test_slots_capacity_counts_distinct_parts_not_lots() -> None:
    strategy = get_strategy(CapacityModel.SLOTS)
    inputs = _inputs(capacity_model=CapacityModel.SLOTS, capacity_slots=4)
    # Two lots of the *same* part occupy one slot, not two.
    occupants = [_lot(lot_id=1, part_id=1), _lot(lot_id=2, part_id=1), _lot(lot_id=3, part_id=2)]
    snapshot = strategy.snapshot(inputs, occupants)
    assert snapshot.capacity == 4.0
    assert snapshot.used == 2.0
    assert snapshot.is_full is False


def test_slots_capacity_is_full_when_every_slot_holds_a_distinct_part() -> None:
    strategy = get_strategy(CapacityModel.SLOTS)
    inputs = _inputs(capacity_model=CapacityModel.SLOTS, capacity_slots=2)
    occupants = [_lot(lot_id=1, part_id=1), _lot(lot_id=2, part_id=2)]
    snapshot = strategy.snapshot(inputs, occupants)
    assert snapshot.is_full is True
    assert snapshot.is_overfull is False  # exactly at capacity, not over it


def test_slots_capacity_widened_by_max_parts_per_slot() -> None:
    strategy = get_strategy(CapacityModel.SLOTS)
    inputs = _inputs(capacity_model=CapacityModel.SLOTS, capacity_slots=2, max_parts_per_slot=3)
    snapshot = strategy.snapshot(inputs, [])
    assert snapshot.capacity == 6.0


def test_slots_capacity_undefined_without_a_slot_count() -> None:
    strategy = get_strategy(CapacityModel.SLOTS)
    snapshot = strategy.snapshot(_inputs(capacity_model=CapacityModel.SLOTS), [])
    assert snapshot.capacity is None
    assert snapshot.fill_ratio is None
    assert snapshot.is_full is False


# ---------------------------------------------------------------------------
# `volume` — including the packaging-aware reel case
# ---------------------------------------------------------------------------


def test_volume_capacity_from_inner_dimensions_and_fill_factor() -> None:
    strategy = get_strategy(CapacityModel.VOLUME)
    inputs = _inputs(
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=100.0,
        inner_width_mm=100.0,
        inner_height_mm=50.0,
        fill_factor=0.5,
    )
    snapshot = strategy.snapshot(inputs, [])
    assert snapshot.capacity == pytest.approx(100 * 100 * 50 * 0.5)


def test_volume_capacity_undefined_when_any_inner_dimension_is_missing() -> None:
    strategy = get_strategy(CapacityModel.VOLUME)
    inputs = _inputs(
        capacity_model=CapacityModel.VOLUME, inner_length_mm=100.0, inner_width_mm=100.0
    )
    snapshot = strategy.snapshot(inputs, [])
    assert snapshot.capacity is None


def test_volume_used_scales_with_quantity_when_no_packaging_footprint() -> None:
    strategy = get_strategy(CapacityModel.VOLUME)
    inputs = _inputs(
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=1000.0,
        inner_width_mm=1000.0,
        inner_height_mm=1000.0,
        fill_factor=1.0,
    )
    # 2000 milli-units = 2 units, each 10 mm^3, no packaging footprint.
    occupants = [_lot(qty_milli=2000, unit_volume_mm3=10.0)]
    snapshot = strategy.snapshot(inputs, occupants)
    assert snapshot.used == pytest.approx(20.0)


def test_volume_used_is_the_packaging_footprint_regardless_of_quantity() -> None:
    """The packaging-aware reel case: a reel occupies the reel's volume
    whether it holds 5000 parts or 12 — PLAN.md's own example."""
    strategy = get_strategy(CapacityModel.VOLUME)
    inputs = _inputs(
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=1000.0,
        inner_width_mm=1000.0,
        inner_height_mm=1000.0,
        fill_factor=1.0,
    )
    huge_reel = _lot(qty_milli=5_000_000, packaging_volume_mm3=1_200_000.0, unit_volume_mm3=2.0)
    sparse_reel = _lot(qty_milli=12_000, packaging_volume_mm3=1_200_000.0, unit_volume_mm3=2.0)
    assert (
        strategy.snapshot(inputs, [huge_reel]).used == strategy.snapshot(inputs, [sparse_reel]).used
    )
    assert strategy.snapshot(inputs, [huge_reel]).used == 1_200_000.0


def test_volume_is_full_at_the_advisory_threshold_not_at_100_percent() -> None:
    strategy = get_strategy(CapacityModel.VOLUME)
    inputs = _inputs(
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=10.0,
        inner_width_mm=10.0,
        inner_height_mm=10.0,
        fill_factor=1.0,
        full_threshold=0.9,
    )
    # 900/1000 = 0.9 exactly, at the threshold.
    occupants = [_lot(qty_milli=1000, unit_volume_mm3=900.0)]
    snapshot = strategy.snapshot(inputs, occupants)
    assert snapshot.fill_ratio == pytest.approx(0.9)
    assert snapshot.is_full is True
    assert snapshot.is_overfull is False


def test_volume_is_overfull_only_when_capacity_is_literally_exceeded() -> None:
    strategy = get_strategy(CapacityModel.VOLUME)
    inputs = _inputs(
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=10.0,
        inner_width_mm=10.0,
        inner_height_mm=10.0,
        fill_factor=1.0,
    )
    occupants = [_lot(qty_milli=1000, unit_volume_mm3=1500.0)]  # 1500 > 1000 capacity
    snapshot = strategy.snapshot(inputs, occupants)
    assert snapshot.is_overfull is True


# ---------------------------------------------------------------------------
# `positions`
# ---------------------------------------------------------------------------


def test_positions_capacity_is_the_slot_count() -> None:
    strategy = get_strategy(CapacityModel.POSITIONS)
    inputs = _inputs(capacity_model=CapacityModel.POSITIONS, capacity_slots=20)
    snapshot = strategy.snapshot(inputs, [])
    assert snapshot.capacity == 20.0


def test_positions_used_falls_back_to_one_lot_one_position_without_pitch_data() -> None:
    strategy = get_strategy(CapacityModel.POSITIONS)
    inputs = _inputs(capacity_model=CapacityModel.POSITIONS, capacity_slots=10)
    occupants = [_lot(lot_id=1), _lot(lot_id=2)]  # no packaging pitch at all
    snapshot = strategy.snapshot(inputs, occupants)
    assert snapshot.used == 2.0


def test_positions_wide_reel_consumes_more_than_one_position() -> None:
    strategy = get_strategy(CapacityModel.POSITIONS)
    # inner_width_mm / capacity_slots = 100 / 10 = 10mm per nominal position.
    inputs = _inputs(
        capacity_model=CapacityModel.POSITIONS, capacity_slots=10, inner_width_mm=100.0
    )
    nominal_reel = _lot(lot_id=1, packaging_pitch_mm=10.0)
    wide_reel = _lot(lot_id=2, packaging_pitch_mm=25.0)
    assert strategy.snapshot(inputs, [nominal_reel]).used == 1.0
    assert strategy.snapshot(inputs, [wide_reel]).used == 3.0  # ceil(25/10)


def test_positions_is_full_when_positions_are_exhausted() -> None:
    strategy = get_strategy(CapacityModel.POSITIONS)
    inputs = _inputs(capacity_model=CapacityModel.POSITIONS, capacity_slots=2, inner_width_mm=20.0)
    occupants = [_lot(lot_id=1, packaging_pitch_mm=10.0), _lot(lot_id=2, packaging_pitch_mm=10.0)]
    snapshot = strategy.snapshot(inputs, occupants)
    assert snapshot.is_full is True


# ---------------------------------------------------------------------------
# `mass` — explicit not-yet-supported stub
# ---------------------------------------------------------------------------


def test_mass_model_raises_not_implemented_rather_than_inventing_a_formula() -> None:
    strategy = get_strategy(CapacityModel.MASS)
    with pytest.raises(NotImplementedError):
        strategy.snapshot(_inputs(capacity_model=CapacityModel.MASS), [])


# ---------------------------------------------------------------------------
# `lot_volume_mm3`
# ---------------------------------------------------------------------------


def test_lot_volume_prefers_packaging_footprint_over_unit_volume() -> None:
    assert (
        lot_volume_mm3(qty_milli=999_000, packaging_volume_mm3=1_200_000.0, unit_volume_mm3=5.0)
        == 1_200_000.0
    )


def test_lot_volume_falls_back_to_unit_volume_times_quantity() -> None:
    assert lot_volume_mm3(qty_milli=3000, packaging_volume_mm3=None, unit_volume_mm3=10.0) == 30.0


def test_lot_volume_is_zero_for_unknown_unit_volume() -> None:
    assert lot_volume_mm3(qty_milli=3000, packaging_volume_mm3=None, unit_volume_mm3=None) == 0.0


def test_lot_volume_clamps_negative_quantity_to_zero() -> None:
    """Balances may go negative (a bad recount is data, not blocked at write
    time) but a negative occupied volume is meaningless."""
    assert lot_volume_mm3(qty_milli=-500, packaging_volume_mm3=None, unit_volume_mm3=10.0) == 0.0


# ---------------------------------------------------------------------------
# Item-dimension cascade — every rung, and which `volume_source` it records
# ---------------------------------------------------------------------------


def _cascade(**overrides: object) -> tuple[float, VolumeSource]:
    defaults: dict[str, object] = {
        "current_unit_volume_mm3": None,
        "current_volume_source": None,
        "length_mm": None,
        "width_mm": None,
        "height_mm": None,
        "shape_factor": None,
        "package_length_mm": None,
        "package_width_mm": None,
        "package_height_mm": None,
        "package_size_class": None,
        "category_size_class": None,
    }
    defaults.update(overrides)
    return cascade_unit_volume_mm3(**defaults)  # type: ignore[arg-type]


def test_cascade_rung_1_override_is_preserved_verbatim() -> None:
    volume, source = _cascade(
        current_unit_volume_mm3=42.0,
        current_volume_source=VolumeSource.OVERRIDE,
        # Even though full dimensions are present, override wins outright.
        length_mm=1.0,
        width_mm=1.0,
        height_mm=1.0,
    )
    assert (volume, source) == (42.0, VolumeSource.OVERRIDE)


def test_cascade_rung_2_dimensions_times_shape_factor() -> None:
    volume, source = _cascade(length_mm=10.0, width_mm=10.0, height_mm=10.0, shape_factor=0.5)
    assert volume == pytest.approx(500.0)
    assert source == VolumeSource.DIMENSIONS


def test_cascade_rung_2_defaults_shape_factor_to_one() -> None:
    volume, source = _cascade(length_mm=2.0, width_mm=3.0, height_mm=4.0)
    assert volume == pytest.approx(24.0)
    assert source == VolumeSource.DIMENSIONS


def test_cascade_rung_3_package_type_dimensions() -> None:
    volume, source = _cascade(package_length_mm=2.0, package_width_mm=3.0, package_height_mm=4.0)
    assert volume == pytest.approx(24.0)
    assert source == VolumeSource.PACKAGE_TYPE


def test_cascade_rung_3_package_type_size_class_when_no_dimensions() -> None:
    volume, source = _cascade(package_size_class=SizeClass.SMALL)
    assert volume == capacity.SIZE_CLASS_VOLUME_MM3[SizeClass.SMALL]
    assert source == VolumeSource.PACKAGE_TYPE


def test_cascade_rung_4_category_default_size_class() -> None:
    volume, source = _cascade(category_size_class=SizeClass.LARGE)
    assert volume == capacity.SIZE_CLASS_VOLUME_MM3[SizeClass.LARGE]
    assert source == VolumeSource.CATEGORY


def test_cascade_rung_5_falls_back_to_a_size_class_constant() -> None:
    volume, source = _cascade()
    assert volume == capacity.SIZE_CLASS_VOLUME_MM3[capacity.DEFAULT_SIZE_CLASS]
    assert source == VolumeSource.SIZE_CLASS


def test_cascade_precedence_dimensions_beat_package_type() -> None:
    volume, source = _cascade(
        length_mm=1.0,
        width_mm=1.0,
        height_mm=1.0,
        package_length_mm=100.0,
        package_width_mm=100.0,
        package_height_mm=100.0,
    )
    assert volume == pytest.approx(1.0)
    assert source == VolumeSource.DIMENSIONS


def test_cascade_precedence_package_type_beats_category() -> None:
    volume, source = _cascade(
        package_size_class=SizeClass.TINY, category_size_class=SizeClass.BULKY
    )
    assert volume == capacity.SIZE_CLASS_VOLUME_MM3[SizeClass.TINY]
    assert source == VolumeSource.PACKAGE_TYPE


def test_size_class_constants_match_plan_md() -> None:
    assert capacity.SIZE_CLASS_VOLUME_MM3 == {
        SizeClass.TINY: 2.0,
        SizeClass.SMALL: 30.0,
        SizeClass.MEDIUM: 300.0,
        SizeClass.LARGE: 3000.0,
        SizeClass.BULKY: 30000.0,
    }


# ---------------------------------------------------------------------------
# `CapacitySnapshot.is_overfull`
# ---------------------------------------------------------------------------


def test_snapshot_is_overfull_requires_a_defined_capacity() -> None:
    snapshot = CapacitySnapshot(
        model=CapacityModel.NONE,
        capacity=None,
        used=1000.0,
        fill_ratio=None,
        is_full=False,
        unit="none",
    )
    assert snapshot.is_overfull is False
