"""Auto-assignment's hard filters and score components — pure functions, no
session required. `assign_location` itself (the DB-facing orchestrator, and
the escalation ladder) is exercised in `tests/integration/test_assignment.py`.
"""

from __future__ import annotations

import json

import pytest

from app.models.enums import CapacityModel
from app.services.assignment import (
    DEFAULT_WEIGHTS,
    PlacementContext,
    ScoreComponents,
    access_score,
    compartment_ok,
    consolidation_score,
    depth_penalty,
    dimension_ok,
    esd_compatible,
    fit_score,
    fragmentation_penalty,
    hard_filter_reasons,
    homing_score,
    packaging_ok,
    part_kind_allowed,
    wu_palmer_similarity,
)


def _ctx(**overrides: object) -> PlacementContext:
    defaults: dict[str, object] = {
        "part_id": 1,
        "part_kind_slug": "component",
        "part_requires_esd": False,
        "part_max_dimension_mm": None,
        "packaging_pitch_mm": None,
    }
    defaults.update(overrides)
    return PlacementContext(**defaults)  # type: ignore[arg-type]


def _reasons(*, strict: bool, **overrides: object) -> list[str]:
    defaults: dict[str, object] = {
        "is_placeable": True,
        "location_esd_safe": None,
        "allowed_part_kinds_json": None,
        "max_item_dimension_mm": None,
        "capacity_model": CapacityModel.SLOTS,
        "max_parts_per_slot": None,
        "capacity_slots": None,
        "distinct_parts_at_location": 0,
        "part_already_present": False,
        "is_overfull": False,
        "ctx": _ctx(),
    }
    defaults.update(overrides)
    return hard_filter_reasons(**defaults, strict=strict)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Wu-Palmer affinity
# ---------------------------------------------------------------------------


def test_wu_palmer_identical_categories_are_maximally_similar() -> None:
    path = [1, 5, 12]
    assert wu_palmer_similarity(path, path) == pytest.approx(1.0)


def test_wu_palmer_siblings_share_their_parent_as_the_lca() -> None:
    # root(1) -> passives(5) -> {resistors(12), capacitors(13)}
    resistors = [1, 5, 12]
    capacitors = [1, 5, 13]
    # depth(lca)=1 (node 5), depth(a)=depth(b)=2 -> 2*1/(2+2) = 0.5
    assert wu_palmer_similarity(resistors, capacitors) == pytest.approx(0.5)


def test_wu_palmer_only_common_ancestor_is_the_root_scores_zero() -> None:
    resistors = [1, 5, 12]
    ics = [1, 6, 20]
    assert wu_palmer_similarity(resistors, ics) == 0.0


def test_wu_palmer_disjoint_trees_score_zero_not_negative() -> None:
    tree_a = [1, 2, 3]
    tree_b = [9, 10, 11]
    assert wu_palmer_similarity(tree_a, tree_b) == 0.0


def test_wu_palmer_empty_path_scores_zero() -> None:
    assert wu_palmer_similarity([], [1, 2]) == 0.0
    assert wu_palmer_similarity([1, 2], []) == 0.0


def test_wu_palmer_two_root_nodes() -> None:
    assert wu_palmer_similarity([1], [1]) == 1.0
    assert wu_palmer_similarity([1], [2]) == 0.0


# ---------------------------------------------------------------------------
# `fit` — peaked, not monotonic
# ---------------------------------------------------------------------------


def test_fit_peaks_at_70_percent_full() -> None:
    assert fit_score(0.70) == pytest.approx(1.0)


def test_fit_a_70_percent_full_bin_outscores_nearly_empty_and_nearly_full() -> None:
    peak = fit_score(0.70)
    nearly_empty = fit_score(0.05)
    nearly_full = fit_score(0.98)
    assert peak > nearly_empty
    assert peak > nearly_full


def test_fit_is_symmetric_around_the_peak() -> None:
    below = fit_score(0.70 - 0.10)
    above = fit_score(0.70 + 0.10)
    assert below == pytest.approx(above)


def test_fit_is_monotonically_increasing_toward_the_peak_from_below() -> None:
    scores = [fit_score(x) for x in (0.0, 0.2, 0.4, 0.6, 0.70)]
    assert scores == sorted(scores)


def test_fit_is_monotonically_decreasing_away_from_the_peak_above() -> None:
    scores = [fit_score(x) for x in (0.70, 0.8, 0.9, 1.0)]
    assert scores == sorted(scores, reverse=True)


def test_fit_is_neutral_when_capacity_is_undefined() -> None:
    assert fit_score(None) == 0.0


# ---------------------------------------------------------------------------
# Other score components
# ---------------------------------------------------------------------------


def test_consolidation_is_binary() -> None:
    assert consolidation_score(True) == 1.0
    assert consolidation_score(False) == 0.0


def test_access_score_multiplies_location_access_by_part_hot_score() -> None:
    assert access_score(0.8, 0.5) == pytest.approx(0.4)
    assert access_score(0.9, 0.0) == 0.0


def test_homing_is_binary() -> None:
    assert homing_score(True) == 1.0
    assert homing_score(False) == 0.0


def test_fragmentation_penalty_scales_linearly_at_quarter_weight() -> None:
    assert fragmentation_penalty(0) == 0.0
    assert fragmentation_penalty(4) == pytest.approx(1.0)


def test_depth_penalty_is_relative_to_max_depth() -> None:
    assert depth_penalty(3, 6) == pytest.approx(0.5)
    assert depth_penalty(0, 6) == 0.0


def test_depth_penalty_is_zero_when_the_tree_is_flat() -> None:
    assert depth_penalty(0, 0) == 0.0


def test_score_components_total_matches_the_plan_md_formula() -> None:
    components = ScoreComponents(
        consolidation=1.0,
        affinity=0.5,
        fit=0.8,
        access=0.4,
        homing=1.0,
        fragmentation_penalty=2.0,
        depth_penalty=0.5,
    )
    weights = dict(DEFAULT_WEIGHTS)
    expected = (
        weights["w_consol"] * 1.0
        + weights["w_affinity"] * 0.5
        + weights["w_fit"] * 0.8
        + weights["w_access"] * 0.4
        + weights["w_home"] * 1.0
        - weights["w_frag"] * 2.0
        - weights["w_depth"] * 0.5
    )
    assert components.total(weights) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Hard filters — each rejecting and admitting
# ---------------------------------------------------------------------------


def test_esd_incompatible_part_requires_but_location_is_not_safe() -> None:
    assert esd_compatible(True, None) is False
    assert esd_compatible(True, False) is False


def test_esd_compatible_when_location_is_safe_or_part_does_not_care() -> None:
    assert esd_compatible(True, True) is True
    assert esd_compatible(False, None) is True
    assert esd_compatible(False, False) is True


def test_part_kind_allowed_admits_when_unrestricted() -> None:
    assert part_kind_allowed(None, "component") is True
    assert part_kind_allowed("[]", "component") is True


def test_part_kind_allowed_rejects_when_not_in_the_list() -> None:
    assert part_kind_allowed(json.dumps(["tool", "cable"]), "component") is False


def test_part_kind_allowed_admits_when_in_the_list() -> None:
    assert part_kind_allowed(json.dumps(["tool", "component"]), "component") is True


def test_part_kind_allowed_is_permissive_on_malformed_json() -> None:
    """Malformed data must never block a scan."""
    assert part_kind_allowed("{not valid json", "component") is True


def test_dimension_ok_admits_when_either_side_is_unknown() -> None:
    assert dimension_ok(None, 50.0) is True
    assert dimension_ok(50.0, None) is True


def test_dimension_ok_rejects_a_part_too_large_for_the_container() -> None:
    assert dimension_ok(20.0, 25.0) is False


def test_dimension_ok_admits_a_part_that_fits() -> None:
    assert dimension_ok(20.0, 15.0) is True
    assert dimension_ok(20.0, 20.0) is True  # exactly at the limit


def test_packaging_ok_only_constrains_positions_model() -> None:
    assert packaging_ok(CapacityModel.VOLUME, None) is True
    assert packaging_ok(CapacityModel.SLOTS, None) is True


def test_packaging_ok_rejects_pitchless_packaging_in_a_positions_rack() -> None:
    assert packaging_ok(CapacityModel.POSITIONS, None) is False


def test_packaging_ok_admits_packaging_with_a_pitch_in_a_positions_rack() -> None:
    assert packaging_ok(CapacityModel.POSITIONS, 12.0) is True


def test_compartment_ok_admits_when_part_already_present() -> None:
    assert (
        compartment_ok(
            max_parts_per_slot=1,
            capacity_slots=1,
            distinct_parts_at_location=1,
            part_already_present=True,
        )
        is True
    )


def test_compartment_ok_admits_when_not_one_part_per_slot() -> None:
    assert (
        compartment_ok(
            max_parts_per_slot=3,
            capacity_slots=1,
            distinct_parts_at_location=1,
            part_already_present=False,
        )
        is True
    )


def test_compartment_ok_rejects_a_full_one_part_per_slot_container() -> None:
    assert (
        compartment_ok(
            max_parts_per_slot=1,
            capacity_slots=2,
            distinct_parts_at_location=2,
            part_already_present=False,
        )
        is False
    )


def test_compartment_ok_admits_a_one_part_per_slot_container_with_room() -> None:
    assert (
        compartment_ok(
            max_parts_per_slot=1,
            capacity_slots=2,
            distinct_parts_at_location=1,
            part_already_present=False,
        )
        is True
    )


# ---------------------------------------------------------------------------
# `hard_filter_reasons` — the combined strict/relaxed sets
# ---------------------------------------------------------------------------


def test_hard_filter_reasons_empty_when_everything_passes() -> None:
    assert _reasons(strict=True) == []
    assert _reasons(strict=False) == []


def test_hard_filter_not_placeable_is_reported() -> None:
    assert _reasons(strict=True, is_placeable=False) == ["not_placeable"]


def test_hard_filter_esd_mismatch_is_reported_in_both_strict_and_relaxed() -> None:
    ctx = _ctx(part_requires_esd=True)
    assert _reasons(strict=True, ctx=ctx, location_esd_safe=False) == ["esd_mismatch"]
    assert _reasons(strict=False, ctx=ctx, location_esd_safe=False) == ["esd_mismatch"]


def test_hard_filter_part_kind_not_allowed_is_reported_in_both() -> None:
    kwargs = {"allowed_part_kinds_json": json.dumps(["tool"])}
    assert _reasons(strict=True, **kwargs) == ["part_kind_not_allowed"]
    assert _reasons(strict=False, **kwargs) == ["part_kind_not_allowed"]


def test_hard_filter_preference_reasons_only_appear_when_strict() -> None:
    """Size, packaging and one-part-per-slot are dropped at the relaxed rung;
    `overfull` is not — see `hard_filter_reasons`'s docstring for why."""
    ctx = _ctx(part_max_dimension_mm=100.0)
    kwargs = {
        "max_item_dimension_mm": 10.0,
        "capacity_model": CapacityModel.POSITIONS,
        "is_overfull": True,
        "max_parts_per_slot": 1,
        "capacity_slots": 1,
        "distinct_parts_at_location": 1,
        "part_already_present": False,
        "ctx": ctx,
    }
    strict_reasons = set(_reasons(strict=True, **kwargs))
    assert strict_reasons == {
        "too_large",
        "packaging_incompatible",
        "no_free_compartment",
        "overfull",
    }
    assert _reasons(strict=False, **kwargs) == ["overfull"]


def test_hard_filter_overfull_alone_is_the_signature_a_defrag_plan_looks_for() -> None:
    """`app.services.assignment._cheapest_defrag_plan` treats "reasons is
    exactly `['overfull']`" as "this location fits otherwise" — so overfull
    being the *only* reason, on its own, must be reachable."""
    assert _reasons(strict=True, is_overfull=True) == ["overfull"]
    assert _reasons(strict=False, is_overfull=True) == ["overfull"]


def test_hard_filter_reasons_are_returned_in_plan_md_order() -> None:
    ctx = _ctx(part_requires_esd=True, part_max_dimension_mm=100.0)
    reasons = _reasons(
        strict=True,
        ctx=ctx,
        is_placeable=False,
        location_esd_safe=False,
        allowed_part_kinds_json=json.dumps(["tool"]),
        max_item_dimension_mm=10.0,
        is_overfull=True,
        max_parts_per_slot=1,
        capacity_slots=1,
        distinct_parts_at_location=1,
    )
    assert reasons == [
        "not_placeable",
        "esd_mismatch",
        "part_kind_not_allowed",
        "overfull",
        "too_large",
        "no_free_compartment",
    ]
