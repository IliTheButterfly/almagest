"""Auto-assignment: hard filters, a weighted score, and an escalation ladder
that never lets a scan come back empty.

Scoring and filtering are pure functions of already-fetched, primitive data —
mirroring `app.services.capacity` — so both are unit-testable without a
session, and `assign_location` (the only DB-facing entry point) does one bulk
evaluation pass rather than one query per candidate location.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Part, PartCategory, PartKind, PartTag, Tag
from app.models.enums import (
    CapacityModel,
    ChildLayout,
    EscalationLevel,
    LotStatus,
    SlotLabelScheme,
)
from app.models.stock import StockLedger, StockLot
from app.models.storage import ContainerType, Location
from app.models.system import Setting
from app.services import capacity
from app.services.capacity import DefragPlan, MoveStep, OccupantLot
from app.services.tree import TreeRepository, location_tree

#: The schema deliberately gives `parts` almost no columns beyond
#: `name`/`part_kind_id`, so intake stays a one-tap action. There is no
#: dedicated ESD column for the same reason. `tags` already exists as the
#: generic, freeform categorisation mechanism, so ESD-sensitivity is expressed
#: the same way a hobbyist would tag anything else: a part carrying this
#: well-known tag. The migration that introduces this service seeds the tag
#: row so the filter is usable out of the box. A judgement call — PLAN.md
#: describes only the *location* side of ESD inheritance, not how a part
#: declares its own requirement.
ESD_SENSITIVE_TAG_SLUG = "esd-sensitive"

# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

#: Row key in `settings`. `value_json` holds a partial override dict; any key
#: it omits falls back to `DEFAULT_WEIGHTS`.
WEIGHTS_SETTING_KEY = "assignment.weights"

#: PLAN.md gives the score formula's *symbols* (`w_consol`, `w_afty`, ...) but
#: no numbers — a genuine silence. These defaults are a judgement call: bias
#: heavily toward `w_consol` (a part's own existing home should almost always
#: win), moderately toward `w_fit` (the peaked bell around 70% full matters,
#: but not more than "does this part already live here"), and lightly toward
#: the two penalty terms (fragmentation/depth are tie-breaking nudges, not
#: primary drivers). All tunable at runtime via the `settings` table.
DEFAULT_WEIGHTS: dict[str, float] = {
    "w_consol": 3.0,
    "w_affinity": 1.5,
    "w_fit": 2.0,
    "w_access": 1.0,
    "w_home": 1.0,
    "w_frag": 1.0,
    "w_depth": 0.5,
}


def load_weights(session: Session) -> dict[str, float]:
    """Read scorer weights from `settings`, falling back to `DEFAULT_WEIGHTS`
    for anything missing or malformed. Tuning is therefore never a deploy."""
    row = session.get(Setting, WEIGHTS_SETTING_KEY)
    weights = dict(DEFAULT_WEIGHTS)
    if row is None:
        return weights
    try:
        overrides = json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return weights
    if not isinstance(overrides, dict):
        return weights
    for key, value in overrides.items():
        if key in weights and isinstance(value, int | float):
            weights[key] = float(value)
    return weights


# ---------------------------------------------------------------------------
# Score components — pure functions, unit-testable without a session
# ---------------------------------------------------------------------------


def wu_palmer_similarity(path_a: Sequence[int], path_b: Sequence[int]) -> float:
    """``2*depth(LCA) / (depth(a) + depth(b))``, computed from `id_path`
    prefix comparison — no recursion, per PLAN.md.

    `path_a`/`path_b` are root-first id sequences, e.g. from
    `TreeRepository.path_ids`. Two categories in different top-level trees
    (no shared root at all) score 0, the same as when their only common
    ancestor is the root — both mean "unrelated" under Wu-Palmer's definition,
    since depth is measured from the root at 0.
    """
    if not path_a or not path_b:
        return 0.0
    common = 0
    for a, b in zip(path_a, path_b, strict=False):
        if a != b:
            break
        common += 1
    depth_lca = common - 1
    depth_a = len(path_a) - 1
    depth_b = len(path_b) - 1
    if depth_a + depth_b == 0:
        # Both are root nodes: identical roots are maximally similar, distinct
        # roots share nothing.
        return 1.0 if path_a[:1] == path_b[:1] else 0.0
    return max(0.0, 2 * depth_lca / (depth_a + depth_b))


def consolidation_score(part_already_present: bool) -> float:
    """The part already has a compatible lot here."""
    return 1.0 if part_already_present else 0.0


def affinity_score(
    part_category_path: Sequence[int] | None,
    occupant_category_paths: Sequence[Sequence[int]],
) -> float:
    """Best-case Wu-Palmer similarity to whatever is already stored here.

    PLAN.md names the formula but not how to aggregate it across possibly many
    existing occupants; the maximum (closest existing neighbour) is used
    rather than a mean, so one well-matched item is not diluted by an
    otherwise-mixed bin.
    """
    if not part_category_path or not occupant_category_paths:
        return 0.0
    return max(wu_palmer_similarity(part_category_path, other) for other in occupant_category_paths)


def fit_score(fill_ratio: float | None) -> float:
    """Peaked, not monotonic: ``exp(-((fill-0.70)^2)/0.08)``.

    Pure best-fit creates unusable slivers; pure worst-fit burns prime real
    estate on one resistor. A location with no defined capacity (the `none`
    model, or missing dimensions) contributes a neutral 0 rather than biasing
    the score in either direction.
    """
    if fill_ratio is None:
        return 0.0
    return math.exp(-((fill_ratio - 0.70) ** 2) / 0.08)


def access_score(location_access_score: float, part_hot_score: float) -> float:
    return location_access_score * part_hot_score


def homing_score(part_ever_stored_here: bool) -> float:
    """Whether this part has ever had a ledger row landing at this location,
    even if nothing is here right now.

    PLAN.md names `homing(L)` in the score formula but never defines it — a
    genuine silence. This reading is deliberately distinct from
    `consolidation` (which only rewards a *currently occupied* compatible
    lot): a location that used to hold this part, then was fully depleted, is
    still plausibly "this part's home" and worth a nudge back, which
    `consolidation` alone cannot express since it looks at present-tense stock
    only.
    """
    return 1.0 if part_ever_stored_here else 0.0


def fragmentation_penalty(distinct_locations_holding_part: int) -> float:
    return 0.25 * distinct_locations_holding_part


def depth_penalty(depth: int, max_depth: int) -> float:
    if max_depth <= 0:
        return 0.0
    return depth / max_depth


@dataclass(frozen=True)
class ScoreComponents:
    consolidation: float
    affinity: float
    fit: float
    access: float
    homing: float
    fragmentation_penalty: float
    depth_penalty: float

    def total(self, weights: Mapping[str, float]) -> float:
        return (
            weights["w_consol"] * self.consolidation
            + weights["w_affinity"] * self.affinity
            + weights["w_fit"] * self.fit
            + weights["w_access"] * self.access
            + weights["w_home"] * self.homing
            - weights["w_frag"] * self.fragmentation_penalty
            - weights["w_depth"] * self.depth_penalty
        )


# ---------------------------------------------------------------------------
# Hard filters — pure functions, unit-testable without a session
# ---------------------------------------------------------------------------


def esd_compatible(part_requires_esd: bool, location_esd_safe: bool | None) -> bool:
    if not part_requires_esd:
        return True
    return bool(location_esd_safe)


def part_kind_allowed(allowed_part_kinds_json: str | None, part_kind_slug: str) -> bool:
    if not allowed_part_kinds_json:
        return True
    try:
        allowed = json.loads(allowed_part_kinds_json)
    except (json.JSONDecodeError, TypeError):
        return True  # malformed data must never block a scan
    if not allowed:
        return True
    return part_kind_slug in allowed


def dimension_ok(max_item_dimension_mm: float | None, part_max_dimension_mm: float | None) -> bool:
    if max_item_dimension_mm is None or part_max_dimension_mm is None:
        # Unknown either side: permissive, not restrictive. Blocking on
        # missing data would penalise exactly the freshly-stubbed parts intake
        # is designed to create quickly.
        return True
    return part_max_dimension_mm <= max_item_dimension_mm


def packaging_ok(capacity_model: CapacityModel, packaging_pitch_mm: float | None) -> bool:
    """Only `positions` (reel/tube rack) containers care about packaging at
    all: a loose/bag/box lot has no pitch and does not belong in a rack."""
    if capacity_model != CapacityModel.POSITIONS:
        return True
    return packaging_pitch_mm is not None


def compartment_ok(
    *,
    max_parts_per_slot: int | None,
    capacity_slots: int | None,
    distinct_parts_at_location: int,
    part_already_present: bool,
) -> bool:
    """A free compartment when one-part-per-slot, per PLAN.md."""
    if part_already_present:
        return True
    if max_parts_per_slot != 1:
        return True
    if capacity_slots is None:
        return True
    return distinct_parts_at_location < capacity_slots


@dataclass(frozen=True)
class PlacementContext:
    """Everything about the *part being placed* that a hard filter or score
    component needs, gathered once per `assign_location` call."""

    part_id: int
    part_kind_slug: str
    part_requires_esd: bool
    part_max_dimension_mm: float | None
    packaging_pitch_mm: float | None


def hard_filter_reasons(
    *,
    is_placeable: bool,
    location_esd_safe: bool | None,
    allowed_part_kinds_json: str | None,
    max_item_dimension_mm: float | None,
    capacity_model: CapacityModel,
    max_parts_per_slot: int | None,
    capacity_slots: int | None,
    distinct_parts_at_location: int,
    part_already_present: bool,
    is_overfull: bool,
    ctx: PlacementContext,
    strict: bool,
) -> list[str]:
    """Every reason a location is rejected, in PLAN.md's order.

    `strict=True` is the full seven-filter set ("DIRECT"). `strict=False`
    drops three *preference-shaped* filters — size, packaging, and
    one-part-per-slot compartments — keeping placeable, ESD, allowed part
    kinds, **and overfull**. This is escalation level 1, "drop soft
    preferences", from PLAN.md's ladder.

    `is_overfull` deliberately survives the relaxation, even though PLAN.md
    lists it alongside the others in the same "Hard filters" bullet with no
    explicit soft/hard split of its own. Dropping it here as readily as the
    others would make it silently re-admit any over-capacity location by
    score alone, with no acknowledgement that capacity was ever a problem —
    and it would make escalation level 3 ("propose the cheapest defrag move
    plan") almost never fire, since the *one* filter it exists to negotiate
    would already have been waved through one rung earlier. Keeping it here
    means the only way to accept an over-capacity location is via that
    explicit, explainable plan.
    """
    reasons = []
    if not is_placeable:
        reasons.append("not_placeable")
    if not esd_compatible(ctx.part_requires_esd, location_esd_safe):
        reasons.append("esd_mismatch")
    if not part_kind_allowed(allowed_part_kinds_json, ctx.part_kind_slug):
        reasons.append("part_kind_not_allowed")
    if is_overfull:
        reasons.append("overfull")
    if strict:
        if not dimension_ok(max_item_dimension_mm, ctx.part_max_dimension_mm):
            reasons.append("too_large")
        if not packaging_ok(capacity_model, ctx.packaging_pitch_mm):
            reasons.append("packaging_incompatible")
        if not compartment_ok(
            max_parts_per_slot=max_parts_per_slot,
            capacity_slots=capacity_slots,
            distinct_parts_at_location=distinct_parts_at_location,
            part_already_present=part_already_present,
        ):
            reasons.append("no_free_compartment")
    return reasons


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    location_id: int
    score: float
    components: ScoreComponents
    #: `capacity - used`; 0.0 when capacity is undefined. Only ever consulted
    #: as the tie-break's second key.
    free_capacity: float


@dataclass(frozen=True)
class NewSiblingProposal:
    parent_id: int
    container_type_id: int
    #: The existing location this proposal is modelled on, purely so the UI
    #: can show "like this one".
    based_on_location_id: int


@dataclass(frozen=True)
class AssignmentResult:
    location_id: int
    escalation_level: EscalationLevel
    #: Human-readable explanation of *why* this rung fired — never parsed,
    #: only displayed. "Return a result object that says which escalation
    #: level produced the answer, so the UI can explain itself" (PLAN.md).
    reason: str
    candidates: tuple[Candidate, ...] = ()
    defrag_plan: DefragPlan | None = None
    new_sibling_proposal: NewSiblingProposal | None = None


# ---------------------------------------------------------------------------
# The one DB-facing evaluation pass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LocationEval:
    location: Location
    container_type: ContainerType | None
    snapshot: capacity.CapacitySnapshot
    reasons_strict: tuple[str, ...]
    reasons_relaxed: tuple[str, ...]
    components: ScoreComponents
    free_capacity: float


def _part_requires_esd(session: Session, part_id: int) -> bool:
    count = session.execute(
        select(func.count())
        .select_from(PartTag)
        .join(Tag, Tag.id == PartTag.tag_id)
        .where(PartTag.part_id == part_id, Tag.slug == ESD_SENSITIVE_TAG_SLUG)
    ).scalar_one()
    return count > 0


def _homed_location_ids(session: Session, part_id: int) -> set[int]:
    rows = session.execute(
        select(StockLedger.to_location_id).where(
            StockLedger.part_id == part_id, StockLedger.to_location_id.isnot(None)
        )
    ).scalars()
    return {row for row in rows if row is not None}


def _distinct_active_locations_for_part(session: Session, part_id: int) -> int:
    return session.execute(
        select(func.count(func.distinct(StockLot.location_id))).where(
            StockLot.part_id == part_id,
            StockLot.status.in_([LotStatus.ACTIVE, LotStatus.QUARANTINED]),
        )
    ).scalar_one()


def _is_overfull(snapshot: capacity.CapacitySnapshot, location: Location) -> bool:
    """Over capacity by the live count **or** by the last persisted one.

    `locations.is_overfull` is written only by the nightly bulk pass, so between
    over-filling a bin and that pass running it still reads `False` — and the
    scorer would offer the bin as a `DIRECT` candidate for the parts that just
    over-filled it. That is the same defect the container page had, one function
    away: a live `used`/`fill_ratio` served beside a stale flag.

    The `or` is not belt-and-braces. Dropping the column would take the *other*
    direction with it: the persisted flag is what the bulk pass sets for
    conditions this snapshot cannot see on its own, and `hard_filter_reasons`
    keeps overfull excluded even under relaxation precisely because it is the one
    condition that a looser search must not talk itself out of. Reading both means
    the scorer never offers a bin that either measure calls full — and an emptied
    bin is still gated by the stale column until the pass clears it, which is the
    conservative direction of the two.
    """
    return bool(snapshot.is_overfull or location.is_overfull)


def _effective_is_placeable(location: Location, container_type: ContainerType | None) -> bool:
    if location.is_placeable is not None:
        return location.is_placeable
    return container_type.is_placeable if container_type else True


def _category_path(
    categories_by_id: Mapping[int, PartCategory], category_id: int | None
) -> list[int] | None:
    category = categories_by_id.get(category_id) if category_id is not None else None
    return TreeRepository.path_ids(category) if category is not None else None


def _build_placement_context(
    session: Session, part: Part, packaging_pitch_mm: float | None
) -> PlacementContext:
    part_kind = session.get(PartKind, part.part_kind_id)
    dims = [d for d in (part.length_mm, part.width_mm, part.height_mm) if d is not None]
    return PlacementContext(
        part_id=part.id,
        part_kind_slug=part_kind.slug if part_kind else "",
        part_requires_esd=_part_requires_esd(session, part.id),
        part_max_dimension_mm=max(dims) if dims else None,
        packaging_pitch_mm=packaging_pitch_mm,
    )


def _evaluate_all_locations(
    session: Session, part: Part, ctx: PlacementContext
) -> list[_LocationEval]:
    """One bulk pass over every location, computing hard-filter reasons (both
    strict and relaxed) and score components for each. Every escalation rung
    derives its candidates from this same list, so a single evaluation always
    backs the whole ladder for one call."""
    tree = location_tree(session)
    # Retired locations are excluded before any scoring happens rather than
    # filtered out as a hard-filter reason (`app.services.removal`): a removed
    # container is not a rejected candidate, it is not a candidate. Leaving it in
    # the list would let the escalation ladder propose putting stock into
    # something the user has taken out of the tree — including via the INBOX
    # fallback, which never refuses anything.
    locations = (
        session.execute(select(Location).where(Location.retired_at.is_(None)).order_by(Location.id))
        .scalars()
        .all()
    )
    container_types = {ct.id: ct for ct in session.execute(select(ContainerType)).scalars()}
    occupants_by_location = capacity.load_all_occupants(session)
    # Both per-model extras, in bulk, so the scorer sees the same fill the map
    # and the container's own page do. It saw neither before: a full cabinet and
    # a full baseplate both scored as empty.
    grid_units_by_location = capacity.all_consumed_grid_units(session)
    child_slots_by_location = capacity.all_occupied_child_slots(session)
    parts_by_id = {p.id: p for p in session.execute(select(Part)).scalars()}
    categories_by_id = {c.id: c for c in session.execute(select(PartCategory)).scalars()}

    part_category_path = _category_path(categories_by_id, part.category_id)
    homed_location_ids = _homed_location_ids(session, part.id)
    fragmentation_count = _distinct_active_locations_for_part(session, part.id)
    max_depth = session.execute(select(func.max(Location.depth))).scalar_one() or 0

    evals: list[_LocationEval] = []
    for location in locations:
        container_type = (
            container_types.get(location.container_type_id)
            if location.container_type_id is not None
            else None
        )
        occupants: list[OccupantLot] = occupants_by_location.get(location.id, [])
        inputs = capacity.enrich(
            capacity.container_inputs(location, container_type),
            grid_units=grid_units_by_location.get(location.id, 0),
            child_slots=child_slots_by_location.get(location.id),
        )
        try:
            snapshot = capacity.get_strategy(inputs.capacity_model).snapshot(inputs, occupants)
        except NotImplementedError:
            # `mass` is reserved for later — exclude rather than crash.
            continue

        distinct_parts = {o.part_id for o in occupants}
        part_already_present = part.id in distinct_parts
        is_placeable = _effective_is_placeable(location, container_type)
        location_esd_safe = tree.nearest_ancestor_value(location, "esd_safe")
        allowed_part_kinds_json = container_type.allowed_part_kinds_json if container_type else None
        max_item_dimension_mm = container_type.max_item_dimension_mm if container_type else None
        max_parts_per_slot = container_type.max_parts_per_slot if container_type else None
        capacity_slots = container_type.capacity_slots if container_type else None

        reasons_strict = tuple(
            hard_filter_reasons(
                is_placeable=is_placeable,
                location_esd_safe=location_esd_safe,
                allowed_part_kinds_json=allowed_part_kinds_json,
                max_item_dimension_mm=max_item_dimension_mm,
                capacity_model=inputs.capacity_model,
                max_parts_per_slot=max_parts_per_slot,
                capacity_slots=capacity_slots,
                distinct_parts_at_location=len(distinct_parts),
                part_already_present=part_already_present,
                is_overfull=_is_overfull(snapshot, location),
                ctx=ctx,
                strict=True,
            )
        )
        reasons_relaxed = tuple(
            hard_filter_reasons(
                is_placeable=is_placeable,
                location_esd_safe=location_esd_safe,
                allowed_part_kinds_json=allowed_part_kinds_json,
                max_item_dimension_mm=max_item_dimension_mm,
                capacity_model=inputs.capacity_model,
                max_parts_per_slot=max_parts_per_slot,
                capacity_slots=capacity_slots,
                distinct_parts_at_location=len(distinct_parts),
                part_already_present=part_already_present,
                is_overfull=_is_overfull(snapshot, location),
                ctx=ctx,
                strict=False,
            )
        )

        occupant_category_paths = [
            path
            for o in occupants
            if (occupant_part := parts_by_id.get(o.part_id)) is not None
            and (path := _category_path(categories_by_id, occupant_part.category_id)) is not None
        ]

        components = ScoreComponents(
            consolidation=consolidation_score(part_already_present),
            affinity=affinity_score(part_category_path, occupant_category_paths),
            fit=fit_score(snapshot.fill_ratio),
            access=access_score(location.access_score, part.hot_score),
            homing=homing_score(location.id in homed_location_ids),
            fragmentation_penalty=fragmentation_penalty(fragmentation_count),
            depth_penalty=depth_penalty(location.depth, max_depth),
        )
        free_capacity = snapshot.capacity - snapshot.used if snapshot.capacity is not None else 0.0

        evals.append(
            _LocationEval(
                location=location,
                container_type=container_type,
                snapshot=snapshot,
                reasons_strict=reasons_strict,
                reasons_relaxed=reasons_relaxed,
                components=components,
                free_capacity=free_capacity,
            )
        )
    return evals


def _rank(
    evals: Sequence[_LocationEval], weights: Mapping[str, float], *, relaxed: bool
) -> list[Candidate]:
    passing = [e for e in evals if not (e.reasons_relaxed if relaxed else e.reasons_strict)]
    candidates = [
        Candidate(
            location_id=e.location.id,
            score=e.components.total(weights),
            components=e.components,
            free_capacity=e.free_capacity,
        )
        for e in passing
    ]
    # Deterministic tie-break: PLAN.md specifies `(-free_capacity, short_id)`,
    # but `locations` has no `short_id` column in this schema (one shared ID
    # space lives in `object_ids` instead) — see storage.py. `locations.id`
    # is the closest stable, monotonic substitute.
    candidates.sort(key=lambda c: (-c.score, -c.free_capacity, c.location_id))
    return candidates


def _next_grid_slot(
    container_type: ContainerType, children: Sequence[Location]
) -> tuple[int, int, str]:
    used_labels = {c.slot_label for c in children if c.slot_label}
    cols = container_type.grid_cols or 1
    index = len(children)
    while True:
        row_idx, col_idx = divmod(index, cols)
        if container_type.slot_label_scheme == SlotLabelScheme.ROW_ALPHA_COL_NUM:
            label = f"{chr(ord('A') + row_idx)}{col_idx + 1}"
        else:
            label = str(index + 1)
        if label not in used_labels:
            return row_idx, col_idx, label
        index += 1


def _materialize_unused_cell(session: Session, ctx: PlacementContext) -> Location | None:
    """Escalation level 2: create the next empty cell in an existing grid
    container that has nominal room left and would admit this part's kind.

    Deliberately minimal — a plain, unlabelled cell, never consulting
    `container_type_slot_templates`'s mixed compartment sizes. Real per-cell
    sizing and merged regions belong to the layout-authoring feature, which is
    out of scope for capacity/assignment; this only guarantees a scan always
    has *somewhere concrete* to land, which a human can relabel or resize
    later.
    """
    tree = location_tree(session)
    rows = session.execute(
        select(Location, ContainerType)
        .join(ContainerType, ContainerType.id == Location.container_type_id)
        .where(ContainerType.child_layout == ChildLayout.GRID)
        .order_by(Location.id)
    ).all()
    for parent, container_type in rows:
        if container_type.grid_rows is None or container_type.grid_cols is None:
            continue
        if not part_kind_allowed(container_type.allowed_part_kinds_json, ctx.part_kind_slug):
            continue
        parent_esd_safe = tree.nearest_ancestor_value(parent, "esd_safe")
        if not esd_compatible(ctx.part_requires_esd, parent_esd_safe):
            continue
        nominal = container_type.grid_rows * container_type.grid_cols
        children = tree.children(parent.id)
        if len(children) >= nominal:
            continue
        row_idx, col_idx, label = _next_grid_slot(container_type, children)
        cell = Location(
            name=f"{parent.name} {label}",
            parent_id=parent.id,
            slot_label=label,
            row_idx=row_idx,
            col_idx=col_idx,
        )
        return tree.insert_and_index(cell)
    return None


def _cheapest_defrag_plan(
    evals: Sequence[_LocationEval], session: Session, part: Part
) -> tuple[int, DefragPlan] | None:
    """Escalation level 3: a location that fits every filter *except* being
    over capacity, with a plan to evict its smallest occupant to `INBOX`.

    `evals` is already ordered by `location.id`, so the first qualifying
    location wins deterministically. Only the single cheapest, always-available
    move (to the permanent staging location) is proposed — a general
    bin-packing search across the whole warehouse is out of scope here.
    """
    for e in evals:
        if e.reasons_strict != ("overfull",):
            continue
        evictable = capacity.cheapest_lot_to_evict(session, e.location.id, exclude_part_id=part.id)
        if evictable is None:
            continue
        inbox = capacity.get_inbox_location(session)
        plan = DefragPlan(
            steps=(
                MoveStep(
                    lot_id=evictable.id,
                    from_location_id=e.location.id,
                    to_location_id=inbox.id,
                    qty_milli=0,
                ),
            ),
            rationale=(
                f"relocate lot {evictable.id} to INBOX to free room for this part "
                f"in location {e.location.id}"
            ),
        )
        return e.location.id, plan
    return None


def _propose_new_sibling(
    evals: Sequence[_LocationEval], ctx: PlacementContext
) -> NewSiblingProposal | None:
    """Escalation level 4: name an existing container type/instance known to
    accept this part's *kind*, as a template for a human to physically add a
    new one next to it.

    Deliberately ignores the specific instance's current ESD state and
    capacity — that is the point: this rung exists precisely for when every
    existing instance of an otherwise-suitable type is full, over capacity, or
    ESD-mismatched, none of which a *type-level* incompatibility (this part's
    kind is simply never allowed here) can be fixed by adding another one.
    `evals` is ordered by `location.id`, so the answer is deterministic.
    """
    for e in evals:
        if e.container_type is None or e.location.parent_id is None:
            continue
        if not part_kind_allowed(e.container_type.allowed_part_kinds_json, ctx.part_kind_slug):
            continue
        return NewSiblingProposal(
            parent_id=e.location.parent_id,
            container_type_id=e.container_type.id,
            based_on_location_id=e.location.id,
        )
    return None


def assign_location(
    session: Session, part: Part, *, packaging_pitch_mm: float | None = None
) -> AssignmentResult:
    """Suggest a location for a new lot of `part`.

    **A scan is never rejected.** The escalation ladder — drop soft
    preferences, materialize an unused grid cell, propose the cheapest defrag
    move plan, propose a new sibling container, fall back to `INBOX` — always
    terminates in a concrete `location_id`, even when the warehouse holds no
    other space at all. `escalation_level` names exactly which rung answered,
    so the caller (eventually, an API response) can explain itself rather
    than presenting every answer as equally confident.

    `packaging_pitch_mm` is the *new* lot's own packaging pitch (not any
    existing occupant's) — pass `Packaging.pitch_mm` when the packaging is
    already known at put-away time; `None` when it is not, which simply skips
    the `positions`-model packaging-compatibility filter.
    """
    weights = load_weights(session)
    ctx = _build_placement_context(session, part, packaging_pitch_mm)
    evals = _evaluate_all_locations(session, part, ctx)

    direct = _rank(evals, weights, relaxed=False)
    if direct:
        return AssignmentResult(
            location_id=direct[0].location_id,
            escalation_level=EscalationLevel.DIRECT,
            reason="best-scoring location passing every hard filter",
            candidates=tuple(direct),
        )

    relaxed = _rank(evals, weights, relaxed=True)
    if relaxed:
        return AssignmentResult(
            location_id=relaxed[0].location_id,
            escalation_level=EscalationLevel.SOFT_PREFERENCES_DROPPED,
            reason=(
                "no location passed every filter; dropped size, packaging, "
                "one-part-per-slot and overfull checks, keeping only "
                "placement/ESD/part-kind"
            ),
            candidates=tuple(relaxed),
        )

    cell = _materialize_unused_cell(session, ctx)
    if cell is not None:
        return AssignmentResult(
            location_id=cell.id,
            escalation_level=EscalationLevel.MATERIALIZED_CELL,
            reason=f"materialized a new empty cell ({cell.name}) in an existing grid container",
        )

    defrag = _cheapest_defrag_plan(evals, session, part)
    if defrag is not None:
        location_id, plan = defrag
        return AssignmentResult(
            location_id=location_id,
            escalation_level=EscalationLevel.DEFRAG_PLAN,
            reason=(
                "location fits every filter but is over capacity; proposing to "
                "relocate its smallest lot to INBOX first"
            ),
            defrag_plan=plan,
        )

    inbox = capacity.get_inbox_location(session)
    sibling = _propose_new_sibling(evals, ctx)
    if sibling is not None:
        return AssignmentResult(
            location_id=inbox.id,
            escalation_level=EscalationLevel.NEW_SIBLING,
            reason=(
                "no existing location has room; proposing a new sibling "
                "container as a template, staged in INBOX for now"
            ),
            new_sibling_proposal=sibling,
        )

    return AssignmentResult(
        location_id=inbox.id,
        escalation_level=EscalationLevel.INBOX,
        reason=(
            "no location, cell, defrag plan or sibling proposal was found; "
            "falling back to permanent staging"
        ),
    )
