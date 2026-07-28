"""Pure-function pieces of the layout authoring service: the label generator,
the overlap/bounds validator, sort-order assignment, and the instance change
guard's classification step. None of these need a database — that is the
whole point of factoring them out this way.
"""

from __future__ import annotations

import pytest

from app.models.enums import SizeClass, SlotLabelScheme
from app.models.storage import Location
from app.services.layout_authoring import (
    LayoutError,
    SlotSpec,
    compute_sort_order,
    diff_instance_layout,
    generate_label,
    validate_no_overlaps,
)

# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------


def test_row_alpha_col_num_labels_a1_through_h6() -> None:
    labels = {
        (r, c): generate_label(
            SlotLabelScheme.ROW_ALPHA_COL_NUM, {}, r, c, grid_rows=6, grid_cols=8
        )
        for r in range(6)
        for c in range(8)
    }
    assert labels[(0, 0)] == "A1"
    assert labels[(0, 5)] == "A6"
    assert labels[(5, 7)] == "F8"


def test_row_alpha_wraps_past_z_spreadsheet_style() -> None:
    """Row 26 is 'AA', matching spreadsheet column naming — needed the moment a
    cabinet has more than 26 rows of drawers."""
    assert (
        generate_label(SlotLabelScheme.ROW_ALPHA_COL_NUM, {}, 25, 0, grid_rows=30, grid_cols=1)
        == "Z1"
    )
    assert (
        generate_label(SlotLabelScheme.ROW_ALPHA_COL_NUM, {}, 26, 0, grid_rows=30, grid_cols=1)
        == "AA1"
    )


def test_sequential_labels_are_1_indexed_row_major() -> None:
    assert generate_label(SlotLabelScheme.SEQUENTIAL, {}, 0, 0, grid_rows=6, grid_cols=8) == "1"
    assert generate_label(SlotLabelScheme.SEQUENTIAL, {}, 0, 7, grid_rows=6, grid_cols=8) == "8"
    assert generate_label(SlotLabelScheme.SEQUENTIAL, {}, 1, 0, grid_rows=6, grid_cols=8) == "9"


def test_sequential_zero_pad_true_derives_width_from_total_cells() -> None:
    """100 cells -> width `len('100') == 3`, so the first cell is '001'."""
    first = generate_label(
        SlotLabelScheme.SEQUENTIAL, {"zero_pad": True}, 0, 0, grid_rows=10, grid_cols=10
    )
    last = generate_label(
        SlotLabelScheme.SEQUENTIAL, {"zero_pad": True}, 9, 9, grid_rows=10, grid_cols=10
    )
    assert first == "001"
    assert last == "100"


def test_sequential_zero_pad_explicit_width() -> None:
    assert (
        generate_label(SlotLabelScheme.SEQUENTIAL, {"zero_pad": 3}, 0, 0, grid_rows=5, grid_cols=1)
        == "001"
    )


def test_custom_scheme_reads_the_explicit_label_list() -> None:
    params = {"labels": ["Nails", "Screws", "Bolts", "Nuts"]}
    assert generate_label(SlotLabelScheme.CUSTOM, params, 0, 0, grid_rows=2, grid_cols=2) == "Nails"
    assert generate_label(SlotLabelScheme.CUSTOM, params, 1, 1, grid_rows=2, grid_cols=2) == "Nuts"


def test_custom_scheme_wrong_length_is_a_layout_error() -> None:
    with pytest.raises(LayoutError) as excinfo:
        generate_label(
            SlotLabelScheme.CUSTOM, {"labels": ["only-one"]}, 0, 0, grid_rows=2, grid_cols=2
        )
    assert excinfo.value.reason == "invalid_custom_labels"


# ---------------------------------------------------------------------------
# validate_no_overlaps — "only contiguous rectangles may merge", generalised
# ---------------------------------------------------------------------------


def test_non_overlapping_slots_are_accepted() -> None:
    validate_no_overlaps(
        [
            SlotSpec(row_idx=0, col_idx=0, slot_label="A1"),
            SlotSpec(row_idx=0, col_idx=1, slot_label="A2", col_span=2),
            SlotSpec(row_idx=1, col_idx=0, slot_label="B1", col_span=3),
        ],
        grid_rows=2,
        grid_cols=3,
    )  # no raise


def test_overlapping_slots_are_refused() -> None:
    with pytest.raises(LayoutError) as excinfo:
        validate_no_overlaps(
            [
                SlotSpec(row_idx=0, col_idx=0, slot_label="A1", col_span=2),
                SlotSpec(row_idx=0, col_idx=1, slot_label="A2"),
            ],
            grid_rows=1,
            grid_cols=3,
        )
    assert excinfo.value.reason == "overlap"


def test_a_slot_outside_the_grid_is_refused() -> None:
    with pytest.raises(LayoutError) as excinfo:
        validate_no_overlaps(
            [SlotSpec(row_idx=0, col_idx=5, slot_label="X")], grid_rows=1, grid_cols=3
        )
    assert excinfo.value.reason == "out_of_bounds"


def test_a_non_positive_span_is_refused() -> None:
    with pytest.raises(LayoutError) as excinfo:
        validate_no_overlaps(
            [SlotSpec(row_idx=0, col_idx=0, slot_label="X", row_span=0)], grid_rows=1, grid_cols=1
        )
    assert excinfo.value.reason == "invalid_span"


def test_duplicate_labels_are_refused() -> None:
    with pytest.raises(LayoutError) as excinfo:
        validate_no_overlaps(
            [
                SlotSpec(row_idx=0, col_idx=0, slot_label="A1"),
                SlotSpec(row_idx=1, col_idx=0, slot_label="A1"),
            ],
            grid_rows=2,
            grid_cols=1,
        )
    assert excinfo.value.reason == "duplicate_label"


# ---------------------------------------------------------------------------
# sort_order: reading order, merges sort by their top-left corner
# ---------------------------------------------------------------------------


def test_sort_order_follows_reading_order_in_steps_of_ten() -> None:
    specs = [
        SlotSpec(row_idx=0, col_idx=1, slot_label="A2"),
        SlotSpec(row_idx=1, col_idx=0, slot_label="B1"),
        SlotSpec(row_idx=0, col_idx=0, slot_label="A1"),
    ]
    ordered = compute_sort_order(specs)
    assert [spec.slot_label for spec, _ in ordered] == ["A1", "A2", "B1"]
    assert [order for _, order in ordered] == [0, 10, 20]


def test_a_merged_region_sorts_by_its_top_left_corner() -> None:
    """Exactly where a reader's eye reaches it — merging must never reorder
    what was already read before it."""
    specs = [
        SlotSpec(row_idx=0, col_idx=2, slot_label="A3"),
        # A merge spanning A1-A2: its row_idx/col_idx (0, 0) *is* its top-left.
        SlotSpec(row_idx=0, col_idx=0, slot_label="A1-A2", col_span=2),
        SlotSpec(row_idx=1, col_idx=0, slot_label="B1"),
    ]
    ordered = compute_sort_order(specs)
    assert [spec.slot_label for spec, _ in ordered] == ["A1-A2", "A3", "B1"]


# ---------------------------------------------------------------------------
# diff_instance_layout — the change guard's classification step
# ---------------------------------------------------------------------------


def _loc(
    id_: int, *, row: int, col: int, label: str, row_span: int = 1, col_span: int = 1
) -> Location:
    loc = Location(
        id=id_,
        name=label,
        slot_label=label,
        row_idx=row,
        col_idx=col,
        row_span=row_span,
        col_span=col_span,
    )
    return loc


def test_an_unchanged_slot_is_neither_created_updated_nor_deleted() -> None:
    a1 = _loc(1, row=0, col=0, label="A1")
    diff = diff_instance_layout([a1], [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")])
    assert diff.creates == ()
    assert diff.safe_updates == ()
    assert diff.deletes == ()
    assert diff.reinterpreted == ()


def test_a_relabel_at_the_same_region_is_a_safe_update() -> None:
    a1 = _loc(1, row=0, col=0, label="A1")
    diff = diff_instance_layout([a1], [SlotSpec(row_idx=0, col_idx=0, slot_label="Resistors")])
    assert diff.safe_updates == ((a1, SlotSpec(row_idx=0, col_idx=0, slot_label="Resistors")),)
    assert diff.deletes == ()
    assert diff.reinterpreted == ()


def test_a_size_class_edit_at_the_same_region_is_a_safe_update() -> None:
    a1 = _loc(1, row=0, col=0, label="A1")
    desired = SlotSpec(row_idx=0, col_idx=0, slot_label="A1", size_class=SizeClass.LARGE)
    diff = diff_instance_layout([a1], [desired])
    assert diff.safe_updates == ((a1, desired),)


def test_a_slot_missing_from_the_desired_layout_is_deleted() -> None:
    a1 = _loc(1, row=0, col=0, label="A1")
    b1 = _loc(2, row=1, col=0, label="B1")
    diff = diff_instance_layout([a1, b1], [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")])
    assert diff.deletes == (b1,)
    assert diff.creates == ()


def test_a_new_region_with_no_current_match_is_created() -> None:
    diff = diff_instance_layout([], [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")])
    assert diff.creates == (SlotSpec(row_idx=0, col_idx=0, slot_label="A1"),)


def test_reusing_a_label_at_a_different_region_is_reinterpreted_not_deleted_or_created() -> None:
    """The one thing refused outright: sliding an existing slot's identity to
    a different grid position rather than deleting and recreating it."""
    a1 = _loc(1, row=0, col=0, label="A1")
    desired = SlotSpec(row_idx=3, col_idx=3, slot_label="A1")
    diff = diff_instance_layout([a1], [desired])
    assert diff.reinterpreted == ((a1, desired),)
    assert diff.deletes == ()
    assert diff.creates == ()


def test_a_label_reused_at_a_survivors_own_position_is_not_reinterpreted() -> None:
    """The label check only fires when the label belongs to a *different* row
    than the one already sitting at the desired region — a slot keeping its
    own label at its own position is the ordinary unchanged case."""
    a1 = _loc(1, row=0, col=0, label="A1")
    diff = diff_instance_layout([a1], [SlotSpec(row_idx=0, col_idx=0, slot_label="A1")])
    assert diff.reinterpreted == ()
    assert diff.safe_updates == ()
    assert diff.deletes == ()


def test_a_label_that_would_land_on_a_different_surviving_slot_is_reinterpreted() -> None:
    """Even though the region side of this spec looks like an ordinary safe
    rename of A2, the label "A1" is still held by a different row (A1) — the
    label check wins over the region match."""
    a1 = _loc(1, row=0, col=0, label="A1")
    a2 = _loc(2, row=0, col=1, label="A2")
    desired = SlotSpec(row_idx=0, col_idx=1, slot_label="A1")
    diff = diff_instance_layout([a1, a2], [desired])
    assert diff.reinterpreted == ((a1, desired),)


def test_a_merge_absorbing_two_slots_deletes_both_and_creates_the_region() -> None:
    a1 = _loc(1, row=0, col=0, label="A1")
    a2 = _loc(2, row=0, col=1, label="A2")
    merged = SlotSpec(row_idx=0, col_idx=0, slot_label="A1-A2", col_span=2)
    diff = diff_instance_layout([a1, a2], [merged])
    assert set(diff.deletes) == {a1, a2}
    assert diff.creates == (merged,)
