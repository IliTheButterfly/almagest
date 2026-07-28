"""`app.services.layout_authoring` against a real database.

Everything that fits in `tests/unit/test_layout_authoring.py` lives there
instead — what is here is specifically the half that needs a session:
materialisation, the type's slot-template table, instantiation, and the
instance-level change guard.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    CapacityModel,
    ChildLayout,
    TagGranularity,
)
from app.models.storage import ContainerType, ContainerTypeSlotTemplate, Location, LocationTag
from app.models.types import utcnow
from app.services import layout_authoring as layout
from app.services.layout_authoring import (
    GuardedLayoutChange,
    LayoutError,
    SlotSpec,
)
from tests.factories import make_lot, make_part


def _grid_type(
    db: Session, *, rows: int, cols: int, slug: str = "cabinet", **overrides: object
) -> ContainerType:
    fields: dict[str, object] = {
        "slug": slug,
        "display_name": slug,
        "child_layout": ChildLayout.LIST,
        "grid_rows": rows,
        "grid_cols": cols,
        "capacity_model": CapacityModel.SLOTS,
        "capacity_slots": rows * cols,
    }
    fields.update(overrides)
    container_type = ContainerType(**fields)  # type: ignore[arg-type]
    db.add(container_type)
    db.flush()
    return container_type


def _template_rows(
    db: Session, container_type: ContainerType
) -> Sequence[ContainerTypeSlotTemplate]:
    return (
        db.execute(
            select(ContainerTypeSlotTemplate)
            .where(ContainerTypeSlotTemplate.container_type_id == container_type.id)
            .order_by(ContainerTypeSlotTemplate.sort_order)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Pure grids cost nothing to store
# ---------------------------------------------------------------------------


def test_a_pure_grid_stores_zero_template_rows(db: Session) -> None:
    container_type = _grid_type(db, rows=6, cols=8)
    db.flush()

    assert container_type.materialize_slots is False
    assert _template_rows(db, container_type) == []

    slots = layout.effective_slots_for_type(db, container_type)
    assert len(slots) == 48
    assert {s.slot_label for s in slots} >= {"A1", "F8"}


def test_materialising_a_pure_grid_writes_exactly_the_generated_rows(db: Session) -> None:
    container_type = _grid_type(db, rows=2, cols=2)
    layout.materialize_type(db, container_type)

    assert container_type.materialize_slots is True
    labels = {row.slot_label for row in _template_rows(db, container_type)}
    assert labels == {"A1", "A2", "B1", "B2"}


def test_materialize_type_is_a_no_op_once_materialised(db: Session) -> None:
    container_type = _grid_type(db, rows=2, cols=2)
    layout.materialize_type(db, container_type)
    layout.merge_type_region(
        db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=2, slot_label="Wide"
    )

    layout.materialize_type(db, container_type)  # must not overwrite the merge
    labels = {row.slot_label for row in _template_rows(db, container_type)}
    assert labels == {"Wide", "B1", "B2"}


def test_replace_with_the_identical_generated_grid_stays_pure(db: Session) -> None:
    """Writing back exactly what the generator already produces must not
    materialise — only an actual difference does."""
    container_type = _grid_type(db, rows=2, cols=2)
    generated = layout.generate_grid(container_type)

    layout.replace_type_slots(db, container_type, generated)

    assert container_type.materialize_slots is False
    assert _template_rows(db, container_type) == []


# ---------------------------------------------------------------------------
# Merge materialises; the generator is never consulted again
# ---------------------------------------------------------------------------


def test_the_first_merge_materialises_the_whole_canvas(db: Session) -> None:
    container_type = _grid_type(db, rows=2, cols=3)
    assert container_type.materialize_slots is False

    layout.merge_type_region(db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=2)

    assert container_type.materialize_slots is True
    labels = {row.slot_label for row in _template_rows(db, container_type)}
    # A1+A2 merged into one region labelled by its top-left; A3, B1-B3 survive
    # as base cells.
    assert labels == {"A1", "A3", "B1", "B2", "B3"}


def test_after_materialising_the_generator_is_never_consulted_again(db: Session) -> None:
    """Mutate `grid_cols` after materialising — if the generator were still
    being consulted, `effective_slots_for_type` would reflect the new size.
    It must not."""
    container_type = _grid_type(db, rows=2, cols=2)
    layout.materialize_type(db, container_type)
    before = {
        (s.row_idx, s.col_idx, s.slot_label)
        for s in layout.effective_slots_for_type(db, container_type)
    }

    container_type.grid_cols = 5  # would change `generate_grid`'s output entirely
    db.flush()

    after = {
        (s.row_idx, s.col_idx, s.slot_label)
        for s in layout.effective_slots_for_type(db, container_type)
    }
    assert after == before


def test_only_contiguous_rectangles_may_merge_not_contiguous(db: Session) -> None:
    """A target that would cut an existing region in half is refused."""
    container_type = _grid_type(db, rows=1, cols=4)
    layout.merge_type_region(
        db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=2, slot_label="Wide"
    )

    with pytest.raises(LayoutError) as excinfo:
        # cols 1-2: half inside "Wide" (col 1), half outside (col 2) -> crosses it
        layout.merge_type_region(db, container_type, row_idx=0, col_idx=1, row_span=1, col_span=2)
    assert excinfo.value.reason == "not_contiguous"


def test_only_contiguous_rectangles_may_merge_gap_in_region(db: Session) -> None:
    """A target reaching outside the declared grid has nothing there to merge."""
    container_type = _grid_type(db, rows=1, cols=2)
    with pytest.raises(LayoutError) as excinfo:
        layout.merge_type_region(db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=5)
    assert excinfo.value.reason == "gap_in_region"


def test_a_contiguous_merge_of_a_base_cell_and_an_existing_merge_succeeds(db: Session) -> None:
    container_type = _grid_type(db, rows=1, cols=4)
    layout.merge_type_region(
        db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=2, slot_label="AB"
    )
    layout.merge_type_region(
        db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=3, slot_label="ABC"
    )

    labels = {
        row.slot_label: (row.col_idx, row.col_span) for row in _template_rows(db, container_type)
    }
    assert labels == {"ABC": (0, 3), "A4": (3, 1)}


# ---------------------------------------------------------------------------
# Split round-trips back to the generator's own labels
# ---------------------------------------------------------------------------


def test_split_round_trips_to_the_generated_base_cells(db: Session) -> None:
    container_type = _grid_type(db, rows=1, cols=3)
    before = {(s.row_idx, s.col_idx, s.slot_label) for s in layout.generate_grid(container_type)}

    layout.merge_type_region(db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=2)
    layout.split_type_region(db, container_type, "A1")

    after = {
        (row.row_idx, row.col_idx, row.slot_label) for row in _template_rows(db, container_type)
    }
    assert after == before


def test_splitting_a_base_cell_is_refused(db: Session) -> None:
    container_type = _grid_type(db, rows=1, cols=2)
    layout.materialize_type(db, container_type)
    with pytest.raises(LayoutError) as excinfo:
        layout.split_type_region(db, container_type, "A1")
    assert excinfo.value.reason == "not_merged"


# ---------------------------------------------------------------------------
# Seed types clone on edit
# ---------------------------------------------------------------------------


def test_ensure_editable_leaves_a_non_seed_type_alone(db: Session) -> None:
    container_type = _grid_type(db, rows=1, cols=1)
    target, cloned = layout.ensure_editable(db, container_type)
    assert target is container_type
    assert cloned is False


def test_ensure_editable_clones_a_seed_type(db: Session) -> None:
    seed = _grid_type(db, rows=2, cols=2, is_seed=True)
    layout.materialize_type(db, seed)

    target, cloned = layout.ensure_editable(db, seed)

    assert cloned is True
    assert target.id != seed.id
    assert target.is_seed is False
    assert target.slug == "cabinet-copy"
    # The clone carries the seed's materialised template with it.
    assert {r.slot_label for r in _template_rows(db, target)} == {
        r.slot_label for r in _template_rows(db, seed)
    }
    # ...and the seed itself is untouched.
    assert seed.is_seed is True


def test_default_clone_slug_avoids_collisions(db: Session) -> None:
    _grid_type(db, rows=1, cols=1, slug="cabinet")
    _grid_type(db, rows=1, cols=1, slug="cabinet-copy")
    db.flush()
    assert layout.default_clone_slug(db, "cabinet") == "cabinet-copy-2"


# ---------------------------------------------------------------------------
# Instantiation: independent copies, grid_incompatibility enforced
# ---------------------------------------------------------------------------


def test_instantiate_creates_n_independent_copies(db: Session) -> None:
    cabinet_type = _grid_type(db, rows=2, cols=2)
    room = Location(name="Room")
    db.add(room)
    db.flush()

    created = layout.instantiate(
        db,
        room,
        cabinet_type,
        count=2,
        naming_pattern="Cabinet {n}",
        tag_granularity=TagGranularity.CONTAINER,
    )
    db.flush()

    assert [c.name for c in created] == ["Cabinet 1", "Cabinet 2"]
    for cabinet in created:
        children = (
            db.execute(select(Location).where(Location.parent_id == cabinet.id)).scalars().all()
        )
        assert {c.slot_label for c in children} == {"A1", "A2", "B1", "B2"}


def test_editing_the_type_after_instantiation_does_not_touch_existing_instances(
    db: Session,
) -> None:
    cabinet_type = _grid_type(db, rows=1, cols=2)
    room = Location(name="Room")
    db.add(room)
    db.flush()

    [cabinet] = layout.instantiate(
        db,
        room,
        cabinet_type,
        count=1,
        naming_pattern="Cabinet",
        tag_granularity=TagGranularity.CONTAINER,
    )
    db.flush()
    before = {
        c.slot_label: (c.row_idx, c.col_idx)
        for c in db.execute(select(Location).where(Location.parent_id == cabinet.id)).scalars()
    }

    # Materialise the type and merge its only two cells into one.
    layout.merge_type_region(
        db, cabinet_type, row_idx=0, col_idx=0, row_span=1, col_span=2, slot_label="Merged"
    )
    db.flush()

    after = {
        c.slot_label: (c.row_idx, c.col_idx)
        for c in db.execute(select(Location).where(Location.parent_id == cabinet.id)).scalars()
    }
    assert after == before


def test_instantiate_mints_short_ids_per_the_requested_granularity(db: Session) -> None:
    """The container root is always tagged (PLAN.md's baseline); `SLOT` is a
    superset that additionally tags every generated slot."""
    from app.models.identity import ObjectId

    cabinet_type = _grid_type(db, rows=1, cols=2)
    room = Location(name="Room")
    db.add(room)
    db.flush()

    [container_only] = layout.instantiate(
        db,
        room,
        cabinet_type,
        count=1,
        naming_pattern="Container-tagged",
        tag_granularity=TagGranularity.CONTAINER,
    )
    [slot_tagged] = layout.instantiate(
        db,
        room,
        cabinet_type,
        count=1,
        naming_pattern="Slot-tagged",
        tag_granularity=TagGranularity.SLOT,
    )
    db.flush()

    def has_short_id(location_id: int) -> bool:
        return (
            db.execute(select(ObjectId).where(ObjectId.entity_pk == location_id)).first()
            is not None
        )

    assert has_short_id(container_only.id) is True
    slots_a = (
        db.execute(select(Location).where(Location.parent_id == container_only.id)).scalars().all()
    )
    assert all(not has_short_id(s.id) for s in slots_a)

    assert has_short_id(slot_tagged.id) is True
    slots_b = (
        db.execute(select(Location).where(Location.parent_id == slot_tagged.id)).scalars().all()
    )
    assert all(has_short_id(s.id) for s in slots_b)


def test_reusing_an_existing_label_for_a_different_slot_is_refused(db: Session) -> None:
    """Even when the *region* side of the request looks like an ordinary safe
    rename of a surviving slot, reusing a still-existing *label* elsewhere is
    refused — the two facts about one desired spec can disagree, and the
    label always wins."""
    _, cabinet = _instantiated_cabinet(db, rows=1, cols=2)
    children = _children_by_label(db, cabinet)

    # A2 survives at its own position; but the label "A1" is desired there,
    # while A1's own row is being dropped from the layout entirely.
    desired = [SlotSpec(row_idx=0, col_idx=1, slot_label="A1")]
    with pytest.raises(LayoutError) as excinfo:
        layout.apply_layout_to_location(db, cabinet, desired)

    assert excinfo.value.reason == "slot_identity_reinterpreted"
    assert db.get(Location, children["A1"].id) is not None
    assert db.get(Location, children["A2"].id) is not None


def test_instantiate_enforces_grid_incompatibility(db: Session) -> None:
    plate_type = _grid_type(
        db, rows=4, cols=4, slug="plate", child_layout=ChildLayout.GRID, grid_pitch_mm=42.0
    )
    wrong_pitch_bin = ContainerType(
        slug="wrong-bin",
        display_name="wrong-bin",
        footprint_cols=1,
        footprint_rows=1,
        grid_pitch_mm=50.0,
        capacity_model=CapacityModel.VOLUME,
    )
    db.add(wrong_pitch_bin)
    plate = Location(name="Plate", container_type_id=plate_type.id)
    db.add(plate)
    db.flush()

    with pytest.raises(LayoutError) as excinfo:
        layout.instantiate(
            db,
            plate,
            wrong_pitch_bin,
            count=1,
            naming_pattern="Bin",
            tag_granularity=TagGranularity.CONTAINER,
        )
    assert excinfo.value.reason == "pitch_mismatch"


# ---------------------------------------------------------------------------
# The change guard
# ---------------------------------------------------------------------------


def _instantiated_cabinet(
    db: Session, *, rows: int = 2, cols: int = 2
) -> tuple[ContainerType, Location]:
    cabinet_type = _grid_type(db, rows=rows, cols=cols)
    room = Location(name="Room")
    db.add(room)
    db.flush()
    [cabinet] = layout.instantiate(
        db,
        room,
        cabinet_type,
        count=1,
        naming_pattern="Cabinet",
        tag_granularity=TagGranularity.CONTAINER,
    )
    db.flush()
    return cabinet_type, cabinet


def _children_by_label(db: Session, cabinet: Location) -> dict[str, Location]:
    rows = db.execute(select(Location).where(Location.parent_id == cabinet.id)).scalars().all()
    return {c.slot_label: c for c in rows if c.slot_label}


def test_safe_relabel_applies_even_when_the_slot_holds_stock(db: Session) -> None:
    _, cabinet = _instantiated_cabinet(db)
    a1 = _children_by_label(db, cabinet)["A1"]
    part = make_part(db)
    make_lot(db, part, a1)
    db.flush()

    desired = [
        SlotSpec(
            row_idx=c.row_idx,
            col_idx=c.col_idx,
            slot_label=("Resistors" if c.id == a1.id else c.slot_label),
        )
        for c in _children_by_label(db, cabinet).values()
    ]
    diff = layout.apply_layout_to_location(db, cabinet, desired)

    assert len(diff.safe_updates) == 1
    db.refresh(a1)
    assert a1.slot_label == "Resistors"
    assert a1.name == "Resistors"


def test_a_shrink_that_empties_a_slot_deletes_it(db: Session) -> None:
    _, cabinet = _instantiated_cabinet(db, rows=1, cols=2)
    children = _children_by_label(db, cabinet)

    desired = [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")]
    diff = layout.apply_layout_to_location(db, cabinet, desired)

    assert len(diff.deletes) == 1
    assert db.get(Location, children["A2"].id) is None
    assert db.get(Location, children["A1"].id) is not None


def test_a_shrink_never_renumbers_a_surviving_slot(db: Session) -> None:
    """B1 keeps its own label and grid position; only its `sort_order` may
    shift once row A is gone."""
    _, cabinet = _instantiated_cabinet(db, rows=2, cols=1)
    children = _children_by_label(db, cabinet)
    b1_id, b1_row, b1_col = children["B1"].id, children["B1"].row_idx, children["B1"].col_idx

    desired = [SlotSpec(row_idx=1, col_idx=0, slot_label="B1")]  # row A dropped
    layout.apply_layout_to_location(db, cabinet, desired)

    b1 = db.get(Location, b1_id)
    assert b1 is not None
    assert b1.slot_label == "B1"
    assert (b1.row_idx, b1.col_idx) == (b1_row, b1_col)
    assert b1.sort_order == 0  # now the only slot, first in reading order


def test_deleting_a_slot_that_holds_stock_is_guarded(db: Session) -> None:
    _, cabinet = _instantiated_cabinet(db, rows=1, cols=2)
    children = _children_by_label(db, cabinet)
    part = make_part(db)
    make_lot(db, part, children["A2"])
    db.flush()

    desired = [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")]  # drops A2
    with pytest.raises(GuardedLayoutChange) as excinfo:
        layout.apply_layout_to_location(db, cabinet, desired)

    assert [a.location_id for a in excinfo.value.affected] == [children["A2"].id]
    assert "has_stock" in excinfo.value.affected[0].reasons
    # Refused, not partially applied: A2 must still exist.
    assert db.get(Location, children["A2"].id) is not None


def test_deleting_a_slot_with_a_bound_tag_is_guarded(db: Session) -> None:
    _, cabinet = _instantiated_cabinet(db, rows=1, cols=2)
    children = _children_by_label(db, cabinet)
    db.add(
        LocationTag(
            location_id=children["A2"].id,
            ndef_url="https://example.test/s/AAAA-AAAA",
            written_at=utcnow(),
        )
    )
    db.flush()

    with pytest.raises(GuardedLayoutChange) as excinfo:
        layout.apply_layout_to_location(
            db, cabinet, [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")]
        )

    assert excinfo.value.affected[0].reasons == ("has_tag",)


def test_the_409_payload_lists_every_affected_slot_not_just_the_first(db: Session) -> None:
    _, cabinet = _instantiated_cabinet(db, rows=1, cols=3)
    children = _children_by_label(db, cabinet)
    part = make_part(db)
    make_lot(db, part, children["A2"])
    make_lot(db, part, children["A3"])
    db.flush()

    with pytest.raises(GuardedLayoutChange) as excinfo:
        layout.apply_layout_to_location(
            db, cabinet, [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")]
        )

    affected_ids = {a.location_id for a in excinfo.value.affected}
    assert affected_ids == {children["A2"].id, children["A3"].id}


def test_reinterpreting_an_existing_slot_identity_is_refused_outright(db: Session) -> None:
    _, cabinet = _instantiated_cabinet(db, rows=1, cols=2)
    children = _children_by_label(db, cabinet)

    # "A1" reappears at A2's position; A1's own position is simply gone.
    desired = [SlotSpec(row_idx=0, col_idx=1, slot_label="A1")]
    with pytest.raises(LayoutError) as excinfo:
        layout.apply_layout_to_location(db, cabinet, desired)

    assert excinfo.value.reason == "slot_identity_reinterpreted"
    # Refused outright: nothing was touched.
    assert db.get(Location, children["A1"].id) is not None
    assert db.get(Location, children["A2"].id) is not None


def test_a_guarded_refusal_leaves_every_slot_in_the_batch_untouched(db: Session) -> None:
    """Ten slots, one blocked -> all ten stay exactly as they were, not nine
    gone and one refused."""
    _, cabinet = _instantiated_cabinet(db, rows=2, cols=5)
    children = _children_by_label(db, cabinet)
    part = make_part(db)
    make_lot(db, part, children["B5"])
    db.flush()
    before_ids = {c.id for c in children.values()}

    with pytest.raises(GuardedLayoutChange):
        layout.apply_layout_to_location(
            db, cabinet, [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")]
        )

    after_ids = {c.id for c in _children_by_label(db, cabinet).values()}
    assert after_ids == before_ids
