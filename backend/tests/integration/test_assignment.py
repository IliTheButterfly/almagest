"""`assign_location` against a real migrated database: hard filters wired from
real container-type/tag data, ESD inheritance through the location tree, the
full escalation ladder, and scorer determinism.

Pure scoring/filter logic is unit-tested without a session in
`tests/unit/test_assignment.py`; this file is about the DB-facing
orchestration and the escalation ladder that only exists once real rows are
involved.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.maintenance import rebuild_location_occupancy
from app.models.catalog import Part, PartTag, Tag
from app.models.enums import CapacityModel, ChildLayout, EscalationLevel, LedgerKind
from app.models.storage import Location
from app.services.assignment import ESD_SENSITIVE_TAG_SLUG, AssignmentResult, assign_location
from app.services.tree import location_tree
from tests.factories import (
    inbox_location,
    make_container_type,
    make_location,
    make_lot,
    make_part,
    post,
)


def _esd_tag(db: Session) -> Tag:
    return db.execute(select(Tag).where(Tag.slug == ESD_SENSITIVE_TAG_SLUG)).scalar_one()


def _make_esd_sensitive_part(db: Session, name: str = "ESD part") -> Part:
    part = make_part(db, name)
    db.add(PartTag(part_id=part.id, tag_id=_esd_tag(db).id))
    db.flush()
    return part


# ---------------------------------------------------------------------------
# DIRECT: the ordinary case
# ---------------------------------------------------------------------------


def test_direct_assignment_picks_the_best_scoring_admissible_location(db: Session) -> None:
    """`INBOX` and any other neutral, unrestricted location are legitimate,
    equally-scored candidates too — so the location that already holds a
    compatible lot of this part (consolidation) is what breaks the tie on
    genuine score, not on the deterministic id tie-break."""
    part = make_part(db)
    good = make_location(db, "Good bin")
    make_lot(db, part, good)
    make_location(db, "Neutral bin")
    ct = make_container_type(db, "restrictive", allowed_part_kinds_json='["nonexistent-kind"]')
    bad = make_location(db, "Bad bin", container_type_id=ct.id)  # excluded by part_kind

    result = assign_location(db, part)

    assert result.location_id == good.id
    assert result.escalation_level == EscalationLevel.DIRECT
    assert result.defrag_plan is None
    assert result.new_sibling_proposal is None
    assert bad.id not in _candidate_ids(result)


# ---------------------------------------------------------------------------
# Hard filters, wired from real data
# ---------------------------------------------------------------------------


def test_esd_sensitive_part_is_excluded_from_a_non_esd_safe_location(db: Session) -> None:
    part = _make_esd_sensitive_part(db)
    unsafe = make_location(db, "Unsafe bin")

    result = assign_location(db, part)

    assert result.location_id != unsafe.id
    # Nothing else exists and safe either, so it must have escalated past
    # both filtering rungs to find *something* — proving the filter actually
    # excluded the only alternative rather than merely being outscored.
    assert result.escalation_level != EscalationLevel.DIRECT


def test_esd_sensitive_part_is_admitted_to_an_esd_safe_location(db: Session) -> None:
    part = _make_esd_sensitive_part(db)
    safe = make_location(db, "Safe bin", esd_safe=True)
    make_location(db, "Unsafe bin", esd_safe=False)

    result = assign_location(db, part)

    assert result.location_id == safe.id
    assert result.escalation_level == EscalationLevel.DIRECT


def test_esd_safety_is_inherited_from_an_ancestor(db: Session) -> None:
    """Marking a whole cabinet ESD-safe must be one edit, not one per drawer —
    the same inheritance `TreeRepository.nearest_ancestor_value` already
    proves for the tree itself, now exercised through assignment.

    The cabinet itself is marked non-placeable (a cabinet holds drawers, not
    loose parts directly) so the *drawer* is unambiguously the only candidate
    this can resolve to.
    """
    part = _make_esd_sensitive_part(db)
    tree = location_tree(db)
    cabinet = tree.insert_and_index(Location(name="ESD cabinet", esd_safe=True, is_placeable=False))
    drawer = tree.insert_and_index(Location(name="Drawer", parent_id=cabinet.id))

    result = assign_location(db, part)

    assert result.location_id == drawer.id
    assert result.escalation_level == EscalationLevel.DIRECT


def _candidate_ids(result: AssignmentResult) -> set[int]:
    return {c.location_id for c in result.candidates}


def test_part_kind_restriction_excludes_a_mismatched_container(db: Session) -> None:
    part = make_part(db)  # kind: component
    ct = make_container_type(db, "tools-only", allowed_part_kinds_json='["tool"]')
    restricted = make_location(db, "Tool drawer", container_type_id=ct.id)

    result = assign_location(db, part)

    assert result.escalation_level == EscalationLevel.DIRECT
    assert restricted.id not in _candidate_ids(result)


def test_max_item_dimension_excludes_a_part_that_is_too_large(db: Session) -> None:
    part = make_part(db, length_mm=50.0, width_mm=10.0, height_mm=10.0)
    ct = make_container_type(db, "small-only", max_item_dimension_mm=20.0)
    too_small = make_location(db, "Small drawer", container_type_id=ct.id)

    result = assign_location(db, part)

    assert result.escalation_level == EscalationLevel.DIRECT
    assert too_small.id not in _candidate_ids(result)


def test_positions_model_rejects_packaging_with_no_pitch(db: Session) -> None:
    part = make_part(db)
    rack_type = make_container_type(
        db, "reel-rack", capacity_model=CapacityModel.POSITIONS, capacity_slots=10
    )
    rack = make_location(db, "Rack", container_type_id=rack_type.id)

    # No packaging pitch supplied for the new lot -> the rack must be rejected.
    result = assign_location(db, part, packaging_pitch_mm=None)

    assert result.escalation_level == EscalationLevel.DIRECT
    assert rack.id not in _candidate_ids(result)


def test_one_part_per_slot_compartment_rejects_a_location_already_holding_another_part(
    db: Session,
) -> None:
    ct = make_container_type(
        db,
        "strict-slots",
        capacity_model=CapacityModel.SLOTS,
        capacity_slots=1,
        max_parts_per_slot=1,
    )
    full = make_location(db, "Strict bin", container_type_id=ct.id)
    other_part = make_part(db, "Occupant")
    make_lot(db, other_part, full)

    new_part = make_part(db, "Newcomer")
    result = assign_location(db, new_part)

    assert result.escalation_level == EscalationLevel.DIRECT
    assert full.id not in _candidate_ids(result)


def test_already_overfull_location_is_excluded_from_direct_assignment(db: Session) -> None:
    ct = make_container_type(db, "declared-overfull")
    overfull = make_location(db, "Overfull bin", container_type_id=ct.id, is_overfull=True)

    part = make_part(db)
    result = assign_location(db, part)

    assert result.escalation_level == EscalationLevel.DIRECT
    assert overfull.id not in _candidate_ids(result)


# ---------------------------------------------------------------------------
# Escalation level 1: drop soft preferences
# ---------------------------------------------------------------------------


def test_soft_preferences_dropped_when_the_only_esd_safe_location_is_too_small(
    db: Session,
) -> None:
    """`overfull` deliberately survives the relaxed pass (see
    `hard_filter_reasons`), so this exercises a filter that genuinely is
    dropped: the part is nominally too large for the container's declared
    `max_item_dimension_mm`, but that is a soft preference, not a hard stop."""
    part = _make_esd_sensitive_part(db, name="Big ESD part")
    part.length_mm, part.width_mm, part.height_mm = 50.0, 10.0, 10.0
    ct = make_container_type(db, "esd-box", max_item_dimension_mm=20.0)
    only_option = make_location(db, "ESD bin", container_type_id=ct.id, esd_safe=True)
    db.commit()

    result = assign_location(db, part)

    assert result.location_id == only_option.id
    assert result.escalation_level == EscalationLevel.SOFT_PREFERENCES_DROPPED


# ---------------------------------------------------------------------------
# Escalation level 2: materialize an unused grid cell
# ---------------------------------------------------------------------------


def test_materialized_cell_when_only_a_grid_container_with_room_exists(db: Session) -> None:
    part = _make_esd_sensitive_part(db)
    box_type = make_container_type(
        db,
        "assortment-box",
        child_layout=ChildLayout.GRID,
        grid_rows=2,
        grid_cols=2,
        is_placeable=False,  # stock goes in child cells, never the box itself
    )
    # `esd_safe` lives on `Location`, not `ContainerType` — the tree walk
    # `TreeRepository.nearest_ancestor_value` uses only ever reads the
    # location's own column, per its use elsewhere in this codebase
    # (`tests/integration/test_tree.py`).
    box = make_location(db, "Box", container_type_id=box_type.id, esd_safe=True)

    result = assign_location(db, part)

    assert result.escalation_level == EscalationLevel.MATERIALIZED_CELL
    cell = db.get(Location, result.location_id)
    assert cell is not None
    assert cell.parent_id == box.id
    assert cell.slot_label == "A1"
    # The new cell must itself be usable: a second call finds it directly.
    second = assign_location(db, part)
    assert second.escalation_level == EscalationLevel.DIRECT
    assert second.location_id == cell.id


def test_materialized_cell_respects_the_nominal_grid_capacity(db: Session) -> None:
    part = _make_esd_sensitive_part(db)
    box_type = make_container_type(
        db,
        "one-cell-box",
        child_layout=ChildLayout.GRID,
        grid_rows=1,
        grid_cols=1,
        is_placeable=False,
    )
    box = make_location(db, "Tiny box", container_type_id=box_type.id, esd_safe=True)
    tree = location_tree(db)
    tree.insert_and_index(Location(name="Existing cell", parent_id=box.id, slot_label="A1"))

    result = assign_location(db, part)

    # The one nominal cell already exists; no room left to materialize.
    assert result.escalation_level != EscalationLevel.MATERIALIZED_CELL


# ---------------------------------------------------------------------------
# Escalation level 3: the cheapest defrag plan
# ---------------------------------------------------------------------------


def test_defrag_plan_proposed_when_the_only_fit_is_over_capacity(db: Session) -> None:
    part = _make_esd_sensitive_part(db)
    ct = make_container_type(db, "esd-slots", capacity_model=CapacityModel.SLOTS, capacity_slots=1)
    location = make_location(db, "ESD bin", container_type_id=ct.id, esd_safe=True)
    occupant_a = make_part(db, "Occupant A")
    occupant_b = make_part(db, "Occupant B")
    lot_a = make_lot(db, occupant_a, location)
    make_lot(db, occupant_b, location)
    post(db, lot_a, 1000, LedgerKind.RECEIVE)
    db.commit()
    # capacity=1 (1 slot), used=2 distinct parts -> literally overfull. The
    # hard filter reads the *cached* `locations.is_overfull` flag (the fast
    # path `assign_location` relies on to avoid recomputing occupancy for
    # every candidate), so that cache has to be rebuilt before it is true.
    rebuild_location_occupancy(db)
    db.commit()

    result = assign_location(db, part)

    assert result.escalation_level == EscalationLevel.DEFRAG_PLAN
    assert result.location_id == location.id
    assert result.defrag_plan is not None
    assert len(result.defrag_plan.steps) == 1
    step = result.defrag_plan.steps[0]
    assert step.from_location_id == location.id
    assert step.to_location_id == inbox_location(db).id


def test_defrag_plan_is_not_proposed_when_nothing_is_evictable(db: Session) -> None:
    """Every occupant is the part being placed itself (already the maximum
    consolidation) -> nothing safe to evict, so this rung must not fire."""
    part = _make_esd_sensitive_part(db)
    ct = make_container_type(
        db, "esd-slots-2", capacity_model=CapacityModel.SLOTS, capacity_slots=0
    )
    location = make_location(db, "ESD bin", container_type_id=ct.id, esd_safe=True)
    lot = make_lot(db, part, location)
    post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()
    # capacity=0, used=1 (this same part) -> overfull, but evicting *this*
    # part would defeat the purpose, and `exclude_part_id` rules it out.
    rebuild_location_occupancy(db)
    db.commit()

    result = assign_location(db, part)

    assert result.escalation_level != EscalationLevel.DEFRAG_PLAN


# ---------------------------------------------------------------------------
# Escalation level 4: propose a new sibling container
# ---------------------------------------------------------------------------


def test_new_sibling_proposed_when_every_instance_of_a_suitable_type_is_esd_mismatched(
    db: Session,
) -> None:
    part = _make_esd_sensitive_part(db)
    tree = location_tree(db)
    cabinet = tree.insert_and_index(Location(name="Cabinet"))  # not ESD-safe
    ct = make_container_type(db, "drawer-type")
    drawer = tree.insert_and_index(
        Location(name="Drawer", parent_id=cabinet.id, container_type_id=ct.id)
    )

    result = assign_location(db, part)

    assert result.escalation_level == EscalationLevel.NEW_SIBLING
    assert result.location_id == inbox_location(db).id
    assert result.new_sibling_proposal is not None
    assert result.new_sibling_proposal.container_type_id == ct.id
    assert result.new_sibling_proposal.parent_id == cabinet.id
    assert result.new_sibling_proposal.based_on_location_id == drawer.id


# ---------------------------------------------------------------------------
# Escalation level 5: INBOX, unconditionally
# ---------------------------------------------------------------------------


def test_completely_incompatible_warehouse_still_falls_back_to_inbox_without_raising(
    db: Session,
) -> None:
    """Nothing at all is ESD-safe, no grid container exists, no defrag target
    exists, and no location even has its own container type to model a
    sibling on. The ladder must still terminate on `INBOX`, not raise."""
    part = _make_esd_sensitive_part(db)
    make_location(db, "Bare bin")  # no container_type at all -> no sibling template

    result = assign_location(db, part)

    assert result.escalation_level == EscalationLevel.INBOX
    assert result.location_id == inbox_location(db).id
    assert result.new_sibling_proposal is None
    assert result.defrag_plan is None


def test_an_entirely_empty_warehouse_still_resolves_via_inbox_itself(db: Session) -> None:
    """`INBOX` is, itself, a placeable location — for a part with no special
    requirements it is found directly, with nothing to escalate past."""
    part = make_part(db)

    result = assign_location(db, part)

    assert result.location_id == inbox_location(db).id
    assert result.escalation_level == EscalationLevel.DIRECT


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_scorer_determinism_repeated_calls_produce_identical_ordering(db: Session) -> None:
    part = make_part(db)
    locations = [make_location(db, f"Bin {i}") for i in range(6)]

    first = assign_location(db, part)
    orderings = [tuple(c.location_id for c in first.candidates)]
    scores = [tuple(c.score for c in first.candidates)]
    for _ in range(9):
        result = assign_location(db, part)
        orderings.append(tuple(c.location_id for c in result.candidates))
        scores.append(tuple(c.score for c in result.candidates))
        assert result.location_id == first.location_id

    assert len(set(orderings)) == 1
    assert len(set(scores)) == 1
    # Sanity: the candidate set actually includes every admissible location,
    # not a coincidentally-stable empty list.
    assert set(orderings[0]) >= {loc.id for loc in locations}


def test_scorer_determinism_ties_break_on_free_capacity_then_location_id(db: Session) -> None:
    """Every location here is identical in every scoring dimension, so the
    ranking can only be resolved by the documented tie-break."""
    part = make_part(db)
    identical = [make_location(db, f"Identical {i}") for i in range(4)]

    result = assign_location(db, part)

    ordering = [
        c.location_id for c in result.candidates if c.location_id in {loc.id for loc in identical}
    ]
    assert ordering == sorted(ordering)
