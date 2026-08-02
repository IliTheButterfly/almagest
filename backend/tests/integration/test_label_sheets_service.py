"""`app.services.labels` against a real database.

Everything that fits in `tests/unit/test_label_rendering.py` lives there
instead — what needs a session is specifically: deriving card geometry from a
real `ContainerType`, walking a real instantiated cabinet's slots in reading
order, the `slot_ids` filter's grid-position guarantee, and the two records
(`label_prints`, `locations.last_printed_at`) a real print has to leave
behind.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    ChildLayout,
    EntityType,
    LabelBackendKind,
    LabelTemplate,
    SlotLabelScheme,
    TagGranularity,
)
from app.models.layout_authoring import LabelPrint, LabelSheetJob
from app.models.storage import ContainerType, Location
from app.services import labels
from app.services import layout_authoring as layout
from app.services.labels import LabelError
from app.services.tree import location_tree
from tests.factories import make_container_type, make_location


def _cabinet_type(
    db: Session, *, rows: int = 2, cols: int = 2, slug: str = "test-cabinet", **overrides: object
) -> ContainerType:
    fields: dict[str, object] = {
        "display_name": "Test cabinet",
        "child_layout": ChildLayout.GRID,
        "grid_rows": rows,
        "grid_cols": cols,
        "slot_label_scheme": SlotLabelScheme.ROW_ALPHA_COL_NUM,
        "front_width_mm": 46.0,
        "front_height_mm": 22.0,
    }
    fields.update(overrides)
    return make_container_type(db, slug, **fields)


def _instantiate(
    db: Session, container_type: ContainerType, parent: Location | None = None
) -> Location:
    root = parent or location_tree(db).insert_and_index(Location(name="Room"))
    created = layout.instantiate(
        db,
        root,
        container_type,
        count=1,
        naming_pattern="Cabinet",
        tag_granularity=TagGranularity.CONTAINER,
    )
    db.flush()
    return created[0]


def _slots(db: Session, cabinet: Location) -> list[Location]:
    return list(
        db.execute(
            select(Location)
            .where(Location.parent_id == cabinet.id)
            .order_by(Location.sort_order, Location.id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Card geometry: the worked example, from a real ContainerType row
# ---------------------------------------------------------------------------


def test_card_size_derives_from_front_dimensions_minus_the_lip_margin(db: Session) -> None:
    """docs/PLAN.md's own worked example: 46x22 mm -> 40x18 mm."""
    container_type = _cabinet_type(db)
    assert labels.card_size_mm(container_type) == (40.0, 18.0)


def test_missing_front_dimensions_is_a_clear_error(db: Session) -> None:
    container_type = _cabinet_type(db, front_width_mm=None, front_height_mm=None)
    with pytest.raises(LabelError) as excinfo:
        labels.card_size_mm(container_type)
    assert excinfo.value.reason == "missing_front_dimensions"
    # A refusal with no path forward teaches people to stop trying. It names the
    # type and the measurement to take.
    assert container_type.display_name in str(excinfo.value)
    assert "measure" in str(excinfo.value)


def test_a_container_type_with_no_type_at_all_is_the_same_error() -> None:
    with pytest.raises(LabelError) as excinfo:
        labels.card_size_mm(None)
    assert excinfo.value.reason == "missing_front_dimensions"


# ---------------------------------------------------------------------------
# Reading order: row-major, matching the physical grid
# ---------------------------------------------------------------------------


def test_a_sheet_has_one_card_per_slot_in_reading_order(db: Session) -> None:
    container_type = _cabinet_type(db, rows=2, cols=3)
    cabinet = _instantiate(db, container_type)
    db.commit()

    result = labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.DRAWER_CARD,
        slot_ids=None,
        backend_kind=LabelBackendKind.FILE,
        dpi=300,
    )
    db.commit()

    assert result.job.item_count == 6
    assert [(p.row, p.col) for p in result.placements] == [
        (0, 0),
        (0, 1),
        (0, 2),
        (1, 0),
        (1, 1),
        (1, 2),
    ]
    assert [p.slot_label for p in result.placements] == ["A1", "A2", "A3", "B1", "B2", "B3"]


def test_a_cabinet_with_no_slots_refuses_to_print(db: Session) -> None:
    container_type = _cabinet_type(db, rows=0, cols=0)
    cabinet = _instantiate(db, container_type)
    db.commit()

    with pytest.raises(LabelError) as excinfo:
        labels.render_sheet(
            db,
            root=cabinet,
            template=LabelTemplate.DRAWER_CARD,
            slot_ids=None,
            backend_kind=LabelBackendKind.FILE,
            dpi=300,
        )
    assert excinfo.value.reason == "no_slots"


# ---------------------------------------------------------------------------
# slot_ids: a partial reprint lands at its true grid cell
# ---------------------------------------------------------------------------


def test_slot_ids_filter_positions_a_single_card_at_its_real_cell(db: Session) -> None:
    container_type = _cabinet_type(db, rows=2, cols=2)
    cabinet = _instantiate(db, container_type)
    db.commit()
    slots = _slots(db, cabinet)
    b1 = next(s for s in slots if s.slot_label == "B1")  # row 1, col 0

    result = labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.DRAWER_CARD,
        slot_ids=[b1.id],
        backend_kind=LabelBackendKind.FILE,
        dpi=300,
    )
    db.commit()

    assert len(result.placements) == 1
    placement = result.placements[0]
    assert placement.location_id == b1.id
    assert (placement.row, placement.col) == (1, 0)


def test_slot_ids_order_in_the_request_does_not_change_the_sheet_order(db: Session) -> None:
    """The sheet's reading order is a property of the physical grid, not of
    whatever order the caller happened to list the ids in."""
    container_type = _cabinet_type(db, rows=1, cols=3)
    cabinet = _instantiate(db, container_type)
    db.commit()
    slots = {s.slot_label: s for s in _slots(db, cabinet)}

    result = labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.DRAWER_CARD,
        slot_ids=[slots["A3"].id, slots["A1"].id],
        backend_kind=LabelBackendKind.FILE,
        dpi=300,
    )
    db.commit()

    assert [p.slot_label for p in result.placements] == ["A1", "A3"]


def test_unknown_slot_ids_are_refused_outright(db: Session) -> None:
    container_type = _cabinet_type(db, rows=1, cols=2)
    cabinet = _instantiate(db, container_type)
    other = _cabinet_type(db, rows=1, cols=1, slug="test-cabinet-other")
    elsewhere = _instantiate(db, other)
    db.commit()
    foreign_slot = _slots(db, elsewhere)[0]

    with pytest.raises(LabelError) as excinfo:
        labels.render_sheet(
            db,
            root=cabinet,
            template=LabelTemplate.DRAWER_CARD,
            slot_ids=[foreign_slot.id],
            backend_kind=LabelBackendKind.FILE,
            dpi=300,
        )
    assert excinfo.value.reason == "unknown_slot_ids"


# ---------------------------------------------------------------------------
# Spanning cards: scale to the footprint, from a real merged region
# ---------------------------------------------------------------------------


def test_a_spanning_slot_prints_a_card_scaled_to_its_footprint(db: Session) -> None:
    container_type = _cabinet_type(db, rows=2, cols=2)
    layout.merge_type_region(db, container_type, row_idx=0, col_idx=0, row_span=1, col_span=2)
    db.flush()
    cabinet = _instantiate(db, container_type)
    db.commit()

    result = labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.DRAWER_CARD,
        slot_ids=None,
        backend_kind=LabelBackendKind.FILE,
        dpi=300,
    )
    db.commit()

    base_width, base_height = labels.card_size_mm(container_type)
    wide = next(p for p in result.placements if p.col_span == 2)
    assert wide.width_mm == pytest.approx(base_width * 2)
    assert wide.height_mm == pytest.approx(base_height)
    normal = next(p for p in result.placements if p.col_span == 1)
    assert normal.width_mm == pytest.approx(base_width)


# ---------------------------------------------------------------------------
# Records: label_prints and last_printed_at, so a reprint matches the original
# ---------------------------------------------------------------------------


def test_every_printed_card_gets_a_label_print_row(db: Session) -> None:
    container_type = _cabinet_type(db, rows=1, cols=2)
    cabinet = _instantiate(db, container_type)
    db.commit()

    result = labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.DRAWER_CARD,
        slot_ids=None,
        backend_kind=LabelBackendKind.PDF_SHEET,
        dpi=300,
    )
    db.commit()

    printed_ids = {p.location_id for p in result.placements}
    rows = (
        db.execute(
            select(LabelPrint).where(
                LabelPrint.entity_type == EntityType.LOCATION, LabelPrint.entity_pk.in_(printed_ids)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == len(printed_ids)
    for row in rows:
        assert row.template == str(LabelTemplate.DRAWER_CARD)
        assert row.backend == str(LabelBackendKind.PDF_SHEET)
        assert row.dpi == 300
        assert row.job_ref == str(result.job.id)
        assert row.succeeded is True


def test_printing_sets_last_printed_at_on_every_card(db: Session) -> None:
    container_type = _cabinet_type(db, rows=1, cols=2)
    cabinet = _instantiate(db, container_type)
    db.commit()
    slots_before = _slots(db, cabinet)
    assert all(s.last_printed_at is None for s in slots_before)

    labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.DRAWER_CARD,
        slot_ids=None,
        backend_kind=LabelBackendKind.FILE,
        dpi=300,
    )
    db.commit()

    slots_after = _slots(db, cabinet)
    assert all(s.last_printed_at is not None for s in slots_after)


def test_placements_round_trip_through_json(db: Session) -> None:
    container_type = _cabinet_type(db, rows=1, cols=2)
    cabinet = _instantiate(db, container_type)
    db.commit()

    result = labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.DRAWER_CARD,
        slot_ids=None,
        backend_kind=LabelBackendKind.FILE,
        dpi=300,
    )
    db.commit()

    job = db.get(LabelSheetJob, result.job.id)
    assert job is not None
    restored = labels.placements_from_json(job.placements_json)
    assert restored == result.placements


# ---------------------------------------------------------------------------
# Content is always re-derived: renaming between two prints changes the card
# ---------------------------------------------------------------------------


def test_resolve_label_fields_reflects_a_rename_made_after_the_first_print(db: Session) -> None:
    """There is no field a stale value could travel through — the only way
    to prove that is to change the database between two calls and show the
    second one follows it."""
    container_type = _cabinet_type(db, rows=1, cols=1)
    cabinet = _instantiate(db, container_type)
    db.commit()
    slot = _slots(db, cabinet)[0]

    first, _ = labels.resolve_label_fields(db, slot, cabinet, LabelTemplate.DRAWER_CARD)
    assert first.tertiary == "Cabinet"

    cabinet.name = "Renamed Cabinet"
    db.flush()

    second, _ = labels.resolve_label_fields(db, slot, cabinet, LabelTemplate.DRAWER_CARD)
    assert second.tertiary == "Renamed Cabinet"


def test_cabinet_card_shows_the_current_label_path_even_after_a_move(db: Session) -> None:
    container_type = _cabinet_type(db, rows=1, cols=1)
    room_a = make_location(db, "Room A")
    room_b = make_location(db, "Room B")
    cabinet = _instantiate(db, container_type, parent=room_a)
    db.commit()

    before, _ = labels.resolve_label_fields(db, cabinet, cabinet, LabelTemplate.CABINET_CARD)
    assert before.tertiary == cabinet.label_path
    assert "Room A" in before.tertiary

    location_tree(db).move(cabinet, room_b.id)
    db.commit()

    after, _ = labels.resolve_label_fields(db, cabinet, cabinet, LabelTemplate.CABINET_CARD)
    assert "Room B" in after.tertiary
    assert "Room A" not in after.tertiary


# ---------------------------------------------------------------------------
# The cabinet_card template: exactly one card, no grid to filter
# ---------------------------------------------------------------------------


def test_cabinet_card_renders_exactly_one_card_for_the_root_itself(db: Session) -> None:
    container_type = _cabinet_type(db, rows=2, cols=2)
    cabinet = _instantiate(db, container_type)
    db.commit()

    result = labels.render_sheet(
        db,
        root=cabinet,
        template=LabelTemplate.CABINET_CARD,
        slot_ids=None,
        backend_kind=LabelBackendKind.FILE,
        dpi=300,
    )
    db.commit()

    assert result.job.item_count == 1
    assert result.placements[0].location_id == cabinet.id


def test_slot_ids_is_refused_for_a_cabinet_card(db: Session) -> None:
    container_type = _cabinet_type(db, rows=1, cols=1)
    cabinet = _instantiate(db, container_type)
    db.commit()
    slot = _slots(db, cabinet)[0]

    with pytest.raises(LabelError) as excinfo:
        labels.render_sheet(
            db,
            root=cabinet,
            template=LabelTemplate.CABINET_CARD,
            slot_ids=[slot.id],
            backend_kind=LabelBackendKind.FILE,
            dpi=300,
        )
    assert excinfo.value.reason == "slot_ids_not_applicable"


def test_a_cabinet_from_the_seed_library_can_actually_print(db: Session) -> None:
    """A fresh install's cabinets could not print a card at all.

    The seed library shipped with no `front_width_mm`/`front_height_mm`, and
    nothing could supply them afterwards: `PATCH /api/container-types/{id}`
    clones a seed rather than mutating it, and no route repoints a standing
    location at the clone. So `POST /api/labels/sheets` answered 422 for every
    container created from the library, permanently — and "printing a drawer
    card today means curl" was not true either, because curl got the same 422.

    The numbers are read off the seed's own committed description ("full-width
    label holders (~18x87 mm cards)"), which is why the card comes out at exactly
    that size rather than at something plausible.
    """
    seeded = db.execute(
        select(ContainerType).where(ContainerType.slug == "raaco-c8-30")
    ).scalar_one()

    assert labels.card_size_mm(seeded) == (87.0, 18.0)


def test_exactly_which_seed_types_can_print(db: Session) -> None:
    """The scope, pinned, because a committed migration got it wrong.

    Its docstring said "Akro-Mils is deliberately left null" as though that were
    the only omission; nine of the eleven seeds have no front dimensions. An
    inaccurate scope claim in a migration is the sort of thing everything
    downstream trusts, so the true set is asserted here rather than described.

    Adding a seed type with dimensions, or measuring one of the nine, is meant to
    change this list — that is the point. Adding one *without* dimensions and not
    noticing is not.
    """
    printable = {
        row.slug
        for row in db.execute(select(ContainerType).where(ContainerType.is_seed)).scalars()
        if row.front_width_mm is not None and row.front_height_mm is not None
    }
    all_seeds = {
        row.slug for row in db.execute(select(ContainerType).where(ContainerType.is_seed)).scalars()
    }

    assert printable == {"raaco-c8-30", "raaco-c10-40"}
    # And the rest, named, so the count cannot drift silently either way.
    assert all_seeds - printable == {
        "akro-mils-10144",
        "gridfinity-baseplate-2x2",
        "gridfinity-baseplate-4x4",
        "gridfinity-baseplate-4x6",
        "gridfinity-bin-1x1x3",
        "gridfinity-bin-1x1x6",
        "gridfinity-bin-2x1x6",
        "gridfinity-bin-2x2x6",
        "gridfinity-bin-3x2x6",
    }


def test_the_seed_whose_front_nobody_has_measured_still_refuses(db: Session) -> None:
    """Akro-Mils is left null on purpose. Its description gives no card size and
    PLAN.md gives no drawer front, so a plausible-looking number would print
    cards that are subtly the wrong size and look deliberate. The refusal now
    tells whoever has the cabinet what to measure."""
    seeded = db.execute(
        select(ContainerType).where(ContainerType.slug == "akro-mils-10144")
    ).scalar_one()

    with pytest.raises(LabelError) as excinfo:
        labels.card_size_mm(seeded)
    assert excinfo.value.reason == "missing_front_dimensions"
