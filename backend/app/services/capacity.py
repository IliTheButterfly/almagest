"""Capacity strategies, the item-dimension cascade, and occupancy plumbing.

**The model is data; the formula is code.** `container_types.capacity_model`
selects one of the strategy classes below by string; the formula itself is
never stored, because a capacity formula as a database string is precisely
the over-engineering `docs/PLAN.md` blames for the prior art in this space
becoming unmaintainable.

Every strategy is a pure function of already-fetched data
(`ContainerCapacityInputs` + `OccupantLot`), never of a live session — that is
what makes each one independently unit-testable and what lets
`app.db.maintenance.rebuild_location_occupancy` recompute every location in
one bulk pass instead of one query per location.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any, ClassVar

from sqlalchemy import Row, delete, func, select
from sqlalchemy.orm import Session

from app.models.catalog import PackageType, Packaging, Part, PartCategory
from app.models.enums import (
    CapacityModel,
    LayoutSuggestionKind,
    LayoutSuggestionStatus,
    LotStatus,
    SizeClass,
    VolumeSource,
)
from app.models.layout import LayoutSuggestion
from app.models.stock import StockLot
from app.models.storage import ContainerType, Location

# ---------------------------------------------------------------------------
# Item-dimension cascade
# ---------------------------------------------------------------------------

#: mm^3, straight from PLAN.md. The last-resort rung of the cascade.
SIZE_CLASS_VOLUME_MM3: dict[SizeClass, float] = {
    SizeClass.TINY: 2.0,
    SizeClass.SMALL: 30.0,
    SizeClass.MEDIUM: 300.0,
    SizeClass.LARGE: 3000.0,
    SizeClass.BULKY: 30000.0,
}

#: The absolute last resort: fired only when neither the part, its package
#: type nor its category say anything about size at all. PLAN.md gives the
#: five size-class constants but not which one an entirely-unknown part should
#: default to — a judgement call. Medium is the least-wrong single guess for
#: an inventory that holds tools and mechanical items alongside SMD passives,
#: and it is never presented as a measurement: `volume_source` records
#: `SIZE_CLASS` whenever this rung is the one that fired.
DEFAULT_SIZE_CLASS = SizeClass.MEDIUM

DEFAULT_FILL_FACTOR = 0.55
DEFAULT_FULL_THRESHOLD = 0.9


def cascade_unit_volume_mm3(
    *,
    current_unit_volume_mm3: float | None,
    current_volume_source: VolumeSource | None,
    length_mm: float | None,
    width_mm: float | None,
    height_mm: float | None,
    shape_factor: float | None,
    package_length_mm: float | None,
    package_width_mm: float | None,
    package_height_mm: float | None,
    package_size_class: SizeClass | None,
    category_size_class: SizeClass | None,
) -> tuple[float, VolumeSource]:
    """The item-dimension cascade, as a pure function.

    ``override -> L*W*H*shape_factor -> package_type default -> category
    default -> size-class constant``, in that order, returning the value and
    which rung produced it.

    `parts.unit_volume_mm3` doubles as both the manual-override slot and the
    cascade's own cache — the schema has no separate override column.
    `current_volume_source == OVERRIDE` is what makes that safe: once a human
    has set one, this function returns it unchanged forever; every other rung
    recomputes fresh on every call, so re-running it after editing a package
    type's dimensions is always safe.
    """
    if current_volume_source == VolumeSource.OVERRIDE and current_unit_volume_mm3 is not None:
        return current_unit_volume_mm3, VolumeSource.OVERRIDE

    if length_mm is not None and width_mm is not None and height_mm is not None:
        factor = shape_factor if shape_factor is not None else 1.0
        return length_mm * width_mm * height_mm * factor, VolumeSource.DIMENSIONS

    if (
        package_length_mm is not None
        and package_width_mm is not None
        and package_height_mm is not None
    ):
        return package_length_mm * package_width_mm * package_height_mm, VolumeSource.PACKAGE_TYPE
    if package_size_class is not None:
        return SIZE_CLASS_VOLUME_MM3[package_size_class], VolumeSource.PACKAGE_TYPE

    if category_size_class is not None:
        return SIZE_CLASS_VOLUME_MM3[category_size_class], VolumeSource.CATEGORY

    return SIZE_CLASS_VOLUME_MM3[DEFAULT_SIZE_CLASS], VolumeSource.SIZE_CLASS


def apply_volume_cascade(
    part: Part, package_type: PackageType | None, category: PartCategory | None
) -> None:
    """Recompute and cache `part.unit_volume_mm3` / `part.volume_source` in place."""
    volume, source = cascade_unit_volume_mm3(
        current_unit_volume_mm3=part.unit_volume_mm3,
        current_volume_source=VolumeSource(part.volume_source) if part.volume_source else None,
        length_mm=part.length_mm,
        width_mm=part.width_mm,
        height_mm=part.height_mm,
        shape_factor=part.shape_factor,
        package_length_mm=package_type.length_mm if package_type else None,
        package_width_mm=package_type.width_mm if package_type else None,
        package_height_mm=package_type.height_mm if package_type else None,
        package_size_class=(
            SizeClass(package_type.size_class) if package_type and package_type.size_class else None
        ),
        category_size_class=(
            SizeClass(category.default_size_class)
            if category and category.default_size_class
            else None
        ),
    )
    part.unit_volume_mm3 = volume
    part.volume_source = source


def lot_volume_mm3(
    *, qty_milli: int, packaging_volume_mm3: float | None, unit_volume_mm3: float | None
) -> float:
    """Packaging-aware lot volume.

    A packaging with its own footprint (a reel, a tube, a tray) occupies
    *that* volume regardless of whether it holds 5000 parts or 12. Only when
    there is no such footprint does volume scale with quantity.
    """
    if packaging_volume_mm3 is not None:
        return packaging_volume_mm3
    if unit_volume_mm3 is None:
        return 0.0
    # Quantities may go negative (a bad recount is data, not blocked at write
    # time) but a negative occupied volume is meaningless; clamp at zero.
    qty_units = max(qty_milli, 0) / 1000.0
    return unit_volume_mm3 * qty_units


# ---------------------------------------------------------------------------
# Capacity strategies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerCapacityInputs:
    """Only the container-type fields a strategy needs, decoupled from the ORM
    so every strategy is unit-testable without a session.

    `fill_factor` and `full_threshold` are passed in already *resolved*
    (location override, else container-type default, else the module
    constant) — resolving them is the caller's job, not the strategy's.
    """

    capacity_model: CapacityModel
    capacity_slots: int | None
    max_parts_per_slot: int | None
    inner_length_mm: float | None
    inner_width_mm: float | None
    inner_height_mm: float | None
    fill_factor: float
    full_threshold: float

    # --- grid_units (ADR 0002) ----------------------------------------------
    #
    # This model is the odd one out: every other strategy measures the *lots*
    # inside a container, but a Gridfinity baseplate's occupancy is measured in
    # the child *containers* sitting on it. Rather than widen the strategy
    # protocol for one model, the already-resolved numbers are passed in — which
    # is the same convention `fill_factor` above already follows.
    grid_rows: int | None = None
    grid_cols: int | None = None
    #: Sum of child footprints, in units. Computed by the caller because it
    #: needs a query, and a strategy must stay unit-testable without a session.
    consumed_grid_units: int = 0


@dataclass(frozen=True)
class OccupantLot:
    """One active/quarantined lot at a location, trimmed to what capacity math
    needs. Decoupled from `StockLot` for the same reason as
    `ContainerCapacityInputs`: pure, session-free strategies."""

    lot_id: int
    part_id: int
    qty_milli: int
    packaging_volume_mm3: float | None
    packaging_pitch_mm: float | None
    unit_volume_mm3: float | None


@dataclass(frozen=True)
class CapacitySnapshot:
    model: CapacityModel
    #: NULL means "no defined capacity" — the `none` model, or dimensions that
    #: are not yet filled in — never a smuggled zero.
    capacity: float | None
    used: float
    fill_ratio: float | None
    #: The *advisory* signal: `fill_ratio >= full_threshold` (volume) or
    #: "no free unit remains" (slots/positions). Never blocks anything.
    is_full: bool
    #: Display label for the UI: "slots" | "mm3" | "positions" | "none".
    unit: str

    @property
    def is_overfull(self) -> bool:
        """Capacity **literally exceeded** — a different, stronger signal than
        `is_full`. This is what sets `locations.is_overfull`; `is_full` is only
        ever a scoring/UI nudge."""
        return self.capacity is not None and self.used > self.capacity


class CapacityStrategy(ABC):
    model: ClassVar[CapacityModel]

    @abstractmethod
    def snapshot(
        self, inputs: ContainerCapacityInputs, occupants: list[OccupantLot]
    ) -> CapacitySnapshot: ...


class NoneCapacityStrategy(CapacityStrategy):
    """Shelves, rooms — informational only. Per PLAN.md's table: never full."""

    model = CapacityModel.NONE

    def snapshot(
        self,
        inputs: ContainerCapacityInputs,  # noqa: ARG002 - shared interface, unused here
        occupants: list[OccupantLot],  # noqa: ARG002 - shared interface, unused here
    ) -> CapacitySnapshot:
        return CapacitySnapshot(
            model=self.model, capacity=None, used=0.0, fill_ratio=None, is_full=False, unit="none"
        )


class SlotsCapacityStrategy(CapacityStrategy):
    """Fixed-compartment boxes. One slot = one distinct part, unless
    `max_parts_per_slot` widens that (multiple small parts sharing a
    compartment) — per-compartment assignment tracking belongs to the layout
    editor, out of scope here, so this strategy only ever sees the aggregate."""

    model = CapacityModel.SLOTS

    def snapshot(
        self, inputs: ContainerCapacityInputs, occupants: list[OccupantLot]
    ) -> CapacitySnapshot:
        per_slot = max(inputs.max_parts_per_slot or 1, 1)
        capacity = (
            float(inputs.capacity_slots * per_slot) if inputs.capacity_slots is not None else None
        )
        used = float(len({o.part_id for o in occupants}))
        fill_ratio = used / capacity if capacity else None
        is_full = capacity is not None and used >= capacity
        return CapacitySnapshot(
            model=self.model,
            capacity=capacity,
            used=used,
            fill_ratio=fill_ratio,
            is_full=is_full,
            unit="slots",
        )


class GridUnitsCapacityStrategy(CapacityStrategy):
    """A measured grid of interchangeable units — Gridfinity's reference case.

    Deliberately **not** `slots`. A slot is a compartment and a unit is an
    *area*: a 2x1 bin consumes two units of its baseplate, so counting
    compartments would report a half-covered baseplate as nearly empty and
    happily suggest putting a third 3x2 bin on a 4x4 plate that has room for
    one.

    Capacity is `grid_rows x grid_cols`; usage is the summed footprint of the
    child containers. Both come from `ContainerCapacityInputs` because occupancy
    here is about children rather than lots — see the note on those fields.

    Still **advisory**, like every other model: an over-capacity put-away is
    accepted and the location is flagged, never rejected. Physically a bin
    either seats on the plate or it does not, but the database finding out
    second is not a reason to block a scan.
    """

    model = CapacityModel.GRID_UNITS

    def snapshot(
        self, inputs: ContainerCapacityInputs, occupants: list[OccupantLot]
    ) -> CapacitySnapshot:
        del occupants  # occupancy here is children, not lots
        capacity = (
            float(inputs.grid_rows * inputs.grid_cols)
            if inputs.grid_rows and inputs.grid_cols
            else None
        )
        used = float(inputs.consumed_grid_units)
        fill_ratio = used / capacity if capacity else None
        is_full = capacity is not None and used >= capacity
        return CapacitySnapshot(
            model=self.model,
            capacity=capacity,
            used=used,
            fill_ratio=fill_ratio,
            is_full=is_full,
            unit="units",
        )


class VolumeCapacityStrategy(CapacityStrategy):
    model = CapacityModel.VOLUME

    def snapshot(
        self, inputs: ContainerCapacityInputs, occupants: list[OccupantLot]
    ) -> CapacitySnapshot:
        capacity = None
        if inputs.inner_length_mm and inputs.inner_width_mm and inputs.inner_height_mm:
            capacity = (
                inputs.inner_length_mm
                * inputs.inner_width_mm
                * inputs.inner_height_mm
                * inputs.fill_factor
            )
        used = sum(
            lot_volume_mm3(
                qty_milli=o.qty_milli,
                packaging_volume_mm3=o.packaging_volume_mm3,
                unit_volume_mm3=o.unit_volume_mm3,
            )
            for o in occupants
        )
        fill_ratio = used / capacity if capacity else None
        is_full = fill_ratio is not None and fill_ratio >= inputs.full_threshold
        return CapacitySnapshot(
            model=self.model,
            capacity=capacity,
            used=used,
            fill_ratio=fill_ratio,
            is_full=is_full,
            unit="mm3",
        )


def _rack_slot_width_mm(inputs: ContainerCapacityInputs) -> float | None:
    """Derived rack pitch: the container's own inner width split evenly across
    its nominal position count.

    The schema has no dedicated "rack pitch" column — only
    `packagings.pitch_mm` (how much space *one lot* of that packaging needs)
    and `container_types.inner_width_mm`/`capacity_slots`. Dividing the two
    gives "how wide one nominal position is", which is what the PLAN.md
    formula's `pitch` term actually has to mean for `ceil(pkg_width / pitch)`
    to produce whole positions per lot. A judgement call, documented because
    nothing else in the schema names it directly.
    """
    if inputs.inner_width_mm and inputs.capacity_slots:
        return inputs.inner_width_mm / inputs.capacity_slots
    return None


def _positions_used(occupant: OccupantLot, slot_width_mm: float | None) -> float:
    if occupant.packaging_pitch_mm is None or slot_width_mm is None or slot_width_mm <= 0:
        # No pitch data on either side: fall back to "one lot, one position"
        # rather than refusing to count it at all.
        return 1.0
    return math.ceil(occupant.packaging_pitch_mm / slot_width_mm)


class PositionsCapacityStrategy(CapacityStrategy):
    """Reel/tube racks. See `_rack_slot_width_mm` for how "pitch" is derived —
    the one genuinely ambiguous corner of the capacity table in PLAN.md."""

    model = CapacityModel.POSITIONS

    def snapshot(
        self, inputs: ContainerCapacityInputs, occupants: list[OccupantLot]
    ) -> CapacitySnapshot:
        capacity = float(inputs.capacity_slots) if inputs.capacity_slots is not None else None
        slot_width = _rack_slot_width_mm(inputs)
        used = float(sum(_positions_used(o, slot_width) for o in occupants))
        fill_ratio = used / capacity if capacity else None
        is_full = capacity is not None and used >= capacity
        return CapacitySnapshot(
            model=self.model,
            capacity=capacity,
            used=used,
            fill_ratio=fill_ratio,
            is_full=is_full,
            unit="positions",
        )


class MassCapacityStrategy(CapacityStrategy):
    """Reserved for later. PLAN.md explicitly lists `mass` with no formula —
    "do not invent one" per the task brief, so this raises rather than
    guessing. Callers that bulk-process every location (see
    `app.db.maintenance.rebuild_location_occupancy`) must catch
    `NotImplementedError` and skip, not let one unsupported location abort a
    whole rebuild."""

    model = CapacityModel.MASS

    def snapshot(
        self, inputs: ContainerCapacityInputs, occupants: list[OccupantLot]
    ) -> CapacitySnapshot:
        raise NotImplementedError(
            "the 'mass' capacity model is reserved for later (see docs/PLAN.md); "
            "no formula exists yet"
        )


_STRATEGIES: dict[CapacityModel, CapacityStrategy] = {
    strategy.model: strategy
    for strategy in (
        NoneCapacityStrategy(),
        SlotsCapacityStrategy(),
        VolumeCapacityStrategy(),
        GridUnitsCapacityStrategy(),
        PositionsCapacityStrategy(),
        MassCapacityStrategy(),
    )
}


def get_strategy(model: CapacityModel) -> CapacityStrategy:
    return _STRATEGIES[model]


def container_inputs(
    location: Location, container_type: ContainerType | None
) -> ContainerCapacityInputs:
    """Resolve a location's effective capacity inputs.

    `fill_factor`: location override, else the container type's default, else
    the module constant. `full_threshold` similarly, container type or the
    module constant (there is no per-location override for it in the schema).
    """
    model = CapacityModel(container_type.capacity_model) if container_type else CapacityModel.NONE
    fill_factor = (
        location.fill_factor
        if location.fill_factor is not None
        else (container_type.default_fill_factor if container_type else DEFAULT_FILL_FACTOR)
    )
    full_threshold = container_type.full_threshold if container_type else DEFAULT_FULL_THRESHOLD
    return ContainerCapacityInputs(
        capacity_model=model,
        capacity_slots=container_type.capacity_slots if container_type else None,
        max_parts_per_slot=container_type.max_parts_per_slot if container_type else None,
        inner_length_mm=container_type.inner_length_mm if container_type else None,
        inner_width_mm=container_type.inner_width_mm if container_type else None,
        inner_height_mm=container_type.inner_height_mm if container_type else None,
        grid_rows=container_type.grid_rows if container_type else None,
        grid_cols=container_type.grid_cols if container_type else None,
        fill_factor=fill_factor,
        full_threshold=full_threshold,
    )


# ---------------------------------------------------------------------------
# DB-facing loaders
# ---------------------------------------------------------------------------

#: Physically present, whether or not it is fully trusted (quarantined stock
#: still occupies real space). Consumed/retired lots are logically gone even
#: if the row survives for history, so they never count.
_OCCUPYING_STATUSES = (LotStatus.ACTIVE, LotStatus.QUARANTINED)


def _occupant_rows(session: Session, *, location_id: int | None) -> Sequence[Row[Any]]:
    stmt = (
        select(
            StockLot.id,
            StockLot.location_id,
            StockLot.part_id,
            StockLot.qty_milli_cached,
            Packaging.package_volume_mm3,
            Packaging.pitch_mm,
            Part.unit_volume_mm3,
        )
        .join(Part, Part.id == StockLot.part_id)
        .outerjoin(Packaging, Packaging.id == StockLot.packaging_id)
        .where(StockLot.status.in_(_OCCUPYING_STATUSES))
        .order_by(StockLot.id)
    )
    if location_id is not None:
        stmt = stmt.where(StockLot.location_id == location_id)
    return session.execute(stmt).all()


def _row_to_occupant(row: Row[Any]) -> OccupantLot:
    return OccupantLot(
        lot_id=row[0],
        part_id=row[2],
        qty_milli=row[3],
        packaging_volume_mm3=row[4],
        packaging_pitch_mm=row[5],
        unit_volume_mm3=row[6],
    )


def load_occupants(session: Session, location_id: int) -> list[OccupantLot]:
    """Every occupying lot at one location, deterministically ordered."""
    return [_row_to_occupant(row) for row in _occupant_rows(session, location_id=location_id)]


def load_all_occupants(session: Session) -> dict[int, list[OccupantLot]]:
    """Every occupying lot, grouped by location, in one query.

    The bulk counterpart to `load_occupants`: used by `rebuild_location_occupancy`
    and by `app.services.assignment` so ranking every candidate location never
    costs one query per location.
    """
    grouped: dict[int, list[OccupantLot]] = {}
    for row in _occupant_rows(session, location_id=None):
        grouped.setdefault(row[1], []).append(_row_to_occupant(row))
    return grouped


def compute_location_snapshot(session: Session, location: Location) -> CapacitySnapshot:
    """Convenience one-off. Bulk callers should use `load_all_occupants` plus
    the strategies directly rather than calling this in a loop."""
    container_type = (
        session.get(ContainerType, location.container_type_id)
        if location.container_type_id
        else None
    )
    inputs = container_inputs(location, container_type)
    if inputs.capacity_model == CapacityModel.GRID_UNITS:
        inputs = replace(inputs, consumed_grid_units=consumed_grid_units(session, location.id))
    occupants = load_occupants(session, location.id)
    return get_strategy(inputs.capacity_model).snapshot(inputs, occupants)


def consumed_grid_units(session: Session, location_id: int) -> int:
    """Grid units taken by the child containers of `location_id`.

    A child whose container type declares no footprint counts as 1x1 — the
    conservative default, since a bin that does not say otherwise occupies one
    cell rather than none. Counting it as zero would let a plate accumulate
    unlimited untyped children and still report itself empty.
    """
    rows = session.execute(
        select(ContainerType.footprint_rows, ContainerType.footprint_cols)
        .select_from(Location)
        .outerjoin(ContainerType, ContainerType.id == Location.container_type_id)
        .where(Location.parent_id == location_id)
    ).all()
    return sum(max(row[0] or 1, 1) * max(row[1] or 1, 1) for row in rows)


def all_consumed_grid_units(session: Session) -> dict[int, int]:
    """Grid units consumed, for every parent at once.

    The bulk sibling of `consumed_grid_units`. It exists because the two paths
    that answer "how full is this?" — the single-location read and the bulk
    rebuild that *persists* the answer — must agree. When only the read knew
    about grid units, a baseplate reported full on its own detail screen and
    zero in `location_occupancy`, so it was never flagged overfull and the
    assignment scorer read a stale fill of zero. A cache being reconstructible
    only helps if the reconstruction is the correct one.

    One grouped query rather than one per location, matching the rest of the
    bulk rebuild.
    """
    rows = session.execute(
        select(
            Location.parent_id,
            func.sum(
                func.max(func.coalesce(ContainerType.footprint_rows, 1), 1)
                * func.max(func.coalesce(ContainerType.footprint_cols, 1), 1)
            ),
        )
        .select_from(Location)
        .outerjoin(ContainerType, ContainerType.id == Location.container_type_id)
        .where(Location.parent_id.isnot(None))
        .group_by(Location.parent_id)
    ).all()
    return {int(row[0]): int(row[1] or 0) for row in rows}


def grid_incompatibility(parent: ContainerType | None, child: ContainerType | None) -> str | None:
    """Why `child` cannot sit in `parent`'s grid, or None if it can.

    **This is the one geometric constraint worth making a hard error**, unlike
    capacity, which is advisory throughout. A pitch mismatch is not a preference
    that a defrag can tidy up later: a 42 mm bin does not physically seat on a
    50 mm plate, so accepting the placement would record a world that cannot
    exist. Capacity being over is a bin that looks full; pitch being wrong is a
    bin on the floor.
    """
    if parent is None or child is None:
        return None
    if parent.grid_pitch_mm is None or child.footprint_cols is None:
        return None  # not a measured grid on one side; nothing to check

    if child.grid_pitch_mm is not None and not math.isclose(
        child.grid_pitch_mm, parent.grid_pitch_mm, rel_tol=1e-6
    ):
        return "pitch_mismatch"

    if parent.grid_cols is not None and (child.footprint_cols or 1) > parent.grid_cols:
        return "footprint_too_wide"
    if parent.grid_rows is not None and (child.footprint_rows or 1) > parent.grid_rows:
        return "footprint_too_deep"
    return None


def get_inbox_location(session: Session) -> Location:
    """The permanent staging fallback. Guaranteed to exist — seeded by the
    migration that introduces `locations.is_staging` — so this is the one
    lookup in the whole escalation ladder that must never come back empty.

    `is_staging` alone stopped identifying it uniquely when ADR 0004 gave every
    project a staging box, so the filter also requires the row to be
    **placeable**: this function's whole job is to name somewhere stock can
    legitimately be put, and a project box is explicitly not that. Ordering by
    `id` would have kept returning the seeded row by luck; luck is not a filter.
    """
    location = (
        session.execute(
            select(Location)
            .where(Location.is_staging.is_(True), Location.is_placeable.is_not(False))
            .order_by(Location.id)
        )
        .scalars()
        .first()
    )
    if location is None:
        raise RuntimeError("no staging location found; the INBOX seed row is missing")
    return location


def cheapest_lot_to_evict(
    session: Session, location_id: int, *, exclude_part_id: int | None = None
) -> StockLot | None:
    """The smallest occupying lot at a location, by occupied volume then
    quantity then id — deterministic, so the same location always proposes the
    same eviction.

    This is "the cheapest move" in PLAN.md's "propose the cheapest defrag move
    plan": relocating the smallest occupant is the least-disruptive way to
    free room.
    """
    occupants = load_occupants(session, location_id)
    if exclude_part_id is not None:
        occupants = [o for o in occupants if o.part_id != exclude_part_id]
    if not occupants:
        return None

    def sort_key(o: OccupantLot) -> tuple[float, int, int]:
        volume = lot_volume_mm3(
            qty_milli=o.qty_milli,
            packaging_volume_mm3=o.packaging_volume_mm3,
            unit_volume_mm3=o.unit_volume_mm3,
        )
        return (volume, o.qty_milli, o.lot_id)

    smallest = min(occupants, key=sort_key)
    return session.get(StockLot, smallest.lot_id)


# ---------------------------------------------------------------------------
# Move plans and the `overfull` suggestion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MoveStep:
    lot_id: int
    from_location_id: int
    to_location_id: int
    #: Zero for a whole-lot move (mirrors `stock_ledger`'s own `move` semantics
    #: — one row, `delta_milli=0`); non-zero only for a proposed partial split.
    qty_milli: int


@dataclass(frozen=True)
class DefragPlan:
    steps: tuple[MoveStep, ...]
    #: Human-readable, for the UI's one-tap confirmation — never parsed back.
    rationale: str


def move_plan_to_json(plan: DefragPlan) -> str:
    return json.dumps({"rationale": plan.rationale, "steps": [asdict(step) for step in plan.steps]})


def move_plan_from_json(raw: str) -> DefragPlan:
    data = json.loads(raw)
    steps = tuple(MoveStep(**step) for step in data["steps"])
    return DefragPlan(steps=steps, rationale=data["rationale"])


def upsert_overfull_suggestion(
    session: Session, location: Location, snapshot: CapacitySnapshot
) -> LayoutSuggestion:
    """Ensure an `overfull` suggestion exists for `location`, without ever
    resurrecting one a human has already dismissed or applied.

    Idempotent: called every time occupancy recomputation finds a location
    over capacity, including on every rebuild after the first.

    * No row yet for this (kind, location) -> insert a fresh `pending` one.
    * A `pending` row already exists -> refresh its plan/score in place, so
      re-running the rebuild against the same unresolved problem never
      duplicates it.
    * A `dismissed` or `applied` row already exists -> **leave it alone**.
      That is the whole point of "dismissals stick": a human who dismissed
      this exact problem must not have it silently reappear as a new pending
      row the next time the rebuild runs and the location is, unsurprisingly,
      still overfull. `clear_overfull_suggestion` is what removes it, and only
      once the problem itself is actually gone.
    """
    existing = session.execute(
        select(LayoutSuggestion).where(
            LayoutSuggestion.kind == LayoutSuggestionKind.OVERFULL,
            LayoutSuggestion.location_id == location.id,
        )
    ).scalar_one_or_none()

    if existing is not None and existing.status != LayoutSuggestionStatus.PENDING:
        return existing

    plan: DefragPlan | None = None
    evictable = cheapest_lot_to_evict(session, location.id)
    if evictable is not None:
        inbox = get_inbox_location(session)
        plan = DefragPlan(
            steps=(
                MoveStep(
                    lot_id=evictable.id,
                    from_location_id=location.id,
                    to_location_id=inbox.id,
                    qty_milli=0,
                ),
            ),
            rationale=(
                f"relocate lot {evictable.id} to INBOX to bring location "
                f"{location.id} back under capacity"
            ),
        )

    move_plan_json = move_plan_to_json(plan) if plan is not None else None
    detail_json = json.dumps(
        {"capacity": snapshot.capacity, "used": snapshot.used, "unit": snapshot.unit}
    )

    if existing is not None:
        existing.score = snapshot.fill_ratio
        existing.move_plan_json = move_plan_json
        existing.detail_json = detail_json
        return existing

    suggestion = LayoutSuggestion(
        kind=LayoutSuggestionKind.OVERFULL,
        status=LayoutSuggestionStatus.PENDING,
        location_id=location.id,
        score=snapshot.fill_ratio,
        move_plan_json=move_plan_json,
        detail_json=detail_json,
    )
    session.add(suggestion)
    session.flush()
    return suggestion


def clear_overfull_suggestion(session: Session, location_id: int) -> None:
    """Drop any `overfull` suggestion — pending, dismissed or applied — once a
    location is no longer over capacity.

    This is *not* itself a human dismissal, it is the problem ceasing to
    exist: stock moved out, capacity was raised. Once that happens even a
    dismissed row is stale rather than a record worth preserving, since
    `upsert_overfull_suggestion` would treat any future recurrence as a fresh
    problem anyway once this row is gone.
    """
    session.execute(
        delete(LayoutSuggestion).where(
            LayoutSuggestion.kind == LayoutSuggestionKind.OVERFULL,
            LayoutSuggestion.location_id == location_id,
        )
    )
