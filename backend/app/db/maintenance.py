"""Cache rebuilds and drift checks.

Every derived value in this schema is reconstructible from its source of truth,
and this module is where that promise is kept. The property matters more than
it looks: it means a cache bug is a stale number a nightly job repairs, never
lost data, and it is what makes it safe to read balances from a cache instead
of computing them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.projects import RESERVED_CACHE_REBUILD_SQL, RESERVED_SUM_SQL
from app.models.storage import ContainerType, Location, LocationOccupancy
from app.models.types import utcnow
from app.services import capacity
from app.services.tree import TreeRepository

#: Correlated single-statement rebuild. Deliberately one statement rather than
#: a Python loop over lots: at 200k ledger rows the loop is the difference
#: between a sub-second maintenance job and one that needs a progress bar.
_REBUILD_LOT_BALANCES = text(
    """
    UPDATE stock_lots
    SET qty_milli_cached = COALESCE(
        (SELECT SUM(delta_milli) FROM stock_ledger WHERE stock_ledger.lot_id = stock_lots.id),
        0
    )
    """
)

_COUNT_LOT_BALANCE_DRIFT = text(
    """
    SELECT COUNT(*)
    FROM stock_lots AS l
    WHERE l.qty_milli_cached <> COALESCE(
        (SELECT SUM(delta_milli) FROM stock_ledger AS sl WHERE sl.lot_id = l.id),
        0
    )
    """
)

_DRIFTING_LOT_IDS = text(
    """
    SELECT l.id
    FROM stock_lots AS l
    WHERE l.qty_milli_cached <> COALESCE(
        (SELECT SUM(delta_milli) FROM stock_ledger AS sl WHERE sl.lot_id = l.id),
        0
    )
    ORDER BY l.id
    LIMIT :limit
    """
)

LOT_BALANCES = "lot_balances"


@dataclass(frozen=True)
class DriftReport:
    cache_name: str
    drift_count: int
    #: Bounded sample, so a systemic failure does not produce a 200k-row log line.
    sample_ids: tuple[int, ...]

    @property
    def is_clean(self) -> bool:
        return self.drift_count == 0


def rebuild_lot_balances(session: Session) -> int:
    """Recompute every `stock_lots.qty_milli_cached` from the ledger.

    Returns the number of lots touched. This is the escape hatch referenced
    throughout the design: whenever the cache is suspect, this restores it from
    the append-only record, which cannot itself have been edited.
    """
    # `Session.execute` is typed as returning `Result`, which does not declare
    # `rowcount`; a DML statement always yields a `CursorResult`, which does.
    result = cast(CursorResult[Any], session.execute(_REBUILD_LOT_BALANCES))
    _mark_rebuilt(session, LOT_BALANCES)
    return result.rowcount


def check_lot_balance_drift(session: Session, *, sample_limit: int = 20) -> DriftReport:
    """Compare cached balances against the ledger without changing anything.

    Run nightly. A non-zero result is a **correctness alert**, not a
    performance note: it means some write path updated the ledger and the cache
    inconsistently, and the numbers the UI has been showing were wrong.
    """
    drift_count = session.execute(_COUNT_LOT_BALANCE_DRIFT).scalar_one()
    sample: tuple[int, ...] = ()
    if drift_count:
        rows = session.execute(_DRIFTING_LOT_IDS, {"limit": sample_limit}).scalars().all()
        sample = tuple(rows)

    report = DriftReport(cache_name=LOT_BALANCES, drift_count=drift_count, sample_ids=sample)
    _record_check(session, report)
    return report


def _mark_rebuilt(session: Session, cache_name: str) -> None:
    session.execute(
        text("UPDATE cache_state SET is_dirty = 0, last_rebuilt_at = :now WHERE name = :name"),
        {"now": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "name": cache_name},
    )


LOCATION_OCCUPANCY = "location_occupancy"


def mark_location_occupancy_dirty(session: Session, location_ids: Iterable[int]) -> None:
    """Flag a set of locations, and every one of their ancestors, dirty.

    Normal writes never need this: `AFTER INSERT ON stock_ledger` and `AFTER
    UPDATE OF location_id ON stock_lots` already do exactly this via triggers
    (see the migration that introduces `location_occupancy`), which is what
    PLAN.md means by "marked dirty by triggers on ledger insert and lot
    relocation". This Python equivalent exists for the one case a trigger
    cannot reach: a maintenance script or a future write path that mutates
    occupancy-relevant state without going through the ledger.
    """
    ids = {i for i in location_ids if i is not None}
    if not ids:
        return
    locations = session.execute(select(Location).where(Location.id.in_(ids))).scalars().all()
    affected: set[int] = set()
    for location in locations:
        affected.update(TreeRepository.path_ids(location))
    if not affected:
        return
    session.execute(
        update(LocationOccupancy)
        .where(LocationOccupancy.location_id.in_(affected))
        .values(is_dirty=True)
    )


def rebuild_location_occupancy(session: Session, *, only_dirty: bool = False) -> int:
    """Recompute occupancy for every location, or only the ones flagged dirty.

    One bulk read of locations/container types, one bulk read of every
    occupying lot (`capacity.load_all_occupants`), then a single Python pass
    through the capacity strategies and one batched upsert — never one query
    per location. At the ~10^3-location scale this design targets elsewhere
    (the tree rebuild, the lot-balance rebuild), that keeps even the
    unconditional full rebuild comfortably sub-second, which is the property
    that matters: it is the escape hatch, mirrored on `rebuild_lot_balances`.

    Also updates `locations.is_overfull` and keeps the `overfull`
    `layout_suggestions` row in sync with it — creating one the moment a
    location tips over capacity, dropping it the moment it no longer is.
    """
    query = select(Location, LocationOccupancy.is_dirty).outerjoin(
        LocationOccupancy, LocationOccupancy.location_id == Location.id
    )
    if only_dirty:
        query = query.where(LocationOccupancy.is_dirty.is_(True))
    rows = session.execute(query.order_by(Location.id)).all()
    if not rows:
        return 0

    container_types = {ct.id: ct for ct in session.execute(select(ContainerType)).scalars()}
    occupants_by_location = capacity.load_all_occupants(session)
    # grid_units measures child containers, not lots, so it needs data the
    # occupant load does not carry. Fetched in bulk here so this path and the
    # single-location read agree — see all_consumed_grid_units on why.
    grid_units_by_location = capacity.all_consumed_grid_units(session)
    # The same, for a container whose slots hold containers. Without it the map
    # and the container's own page disagree about the same cabinet, and only
    # this path writes `is_overfull`.
    child_slots_by_location = capacity.all_occupied_child_slots(session)
    now = utcnow()

    payload: list[dict[str, Any]] = []
    for location, _is_dirty in rows:
        container_type = (
            container_types.get(location.container_type_id)
            if location.container_type_id is not None
            else None
        )
        inputs = capacity.enrich(
            capacity.container_inputs(location, container_type),
            grid_units=grid_units_by_location.get(location.id, 0),
            child_slots=child_slots_by_location.get(location.id),
        )
        occupants = occupants_by_location.get(location.id, [])
        try:
            snapshot = capacity.get_strategy(inputs.capacity_model).snapshot(inputs, occupants)
        except NotImplementedError:
            # `mass` is reserved for later (see app.services.capacity) — treat
            # as "no data" rather than aborting the whole bulk rebuild over
            # one unsupported location.
            snapshot = capacity.CapacitySnapshot(
                model=inputs.capacity_model,
                capacity=None,
                used=0.0,
                fill_ratio=None,
                is_full=False,
                unit="unsupported",
            )

        was_overfull = location.is_overfull
        location.is_overfull = snapshot.is_overfull
        if snapshot.is_overfull:
            capacity.upsert_overfull_suggestion(session, location, snapshot)
        elif was_overfull:
            capacity.clear_overfull_suggestion(session, location.id)

        payload.append(
            {
                "location_id": location.id,
                "capacity_model": str(inputs.capacity_model),
                "capacity": snapshot.capacity,
                "used": snapshot.used,
                "fill_ratio": snapshot.fill_ratio,
                "is_full": snapshot.is_full,
                "is_dirty": False,
                "computed_at": now,
            }
        )

    stmt = sqlite_insert(LocationOccupancy).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[LocationOccupancy.location_id],
        set_={
            "capacity_model": stmt.excluded.capacity_model,
            "capacity": stmt.excluded.capacity,
            "used": stmt.excluded.used,
            "fill_ratio": stmt.excluded.fill_ratio,
            "is_full": stmt.excluded.is_full,
            "is_dirty": stmt.excluded.is_dirty,
            "computed_at": stmt.excluded.computed_at,
        },
    )
    session.execute(stmt)
    _mark_rebuilt(session, LOCATION_OCCUPANCY)
    return len(payload)


RESERVATIONS = "reservations"

#: The string the initial migration already seeded into `cache_state`, so the
#: nightly check has a row to write to. Straight from the model, so the rebuild
#: this module runs is byte-identical to the one the design documents.
_REBUILD_RESERVED_QUANTITIES = text(RESERVED_CACHE_REBUILD_SQL)

_COUNT_RESERVED_DRIFT = text(
    "SELECT COUNT(*) FROM stock_lots AS l"
    f" WHERE l.qty_reserved_milli_cached <> {RESERVED_SUM_SQL.format(lot='l.id')}"
)

_DRIFTING_RESERVED_LOT_IDS = text(
    "SELECT l.id FROM stock_lots AS l"
    f" WHERE l.qty_reserved_milli_cached <> {RESERVED_SUM_SQL.format(lot='l.id')}"
    " ORDER BY l.id LIMIT :limit"
)


def rebuild_reserved_quantities(session: Session) -> int:
    """Recompute every `stock_lots.qty_reserved_milli_cached` from allocations.

    The exact counterpart of `rebuild_lot_balances`, and load-bearing for the
    same reason: `app.services.reservations` maintains this counter
    incrementally on every reserve/release/consume, and an incrementally
    maintained counter is only safe *because* one statement can reconstruct it.
    A half-applied cancel or a crashed pick leaves a number that is wrong in a
    way no user can see, and this is what makes that a stale value a nightly job
    repairs rather than lost state.

    Returns the number of lots touched — every lot, deliberately: the case that
    needs repairing most is a lot whose cache is *too high* because a release
    never decremented it, and that lot has no allocations left to find.
    """
    result = cast(CursorResult[Any], session.execute(_REBUILD_RESERVED_QUANTITIES))
    _mark_rebuilt(session, RESERVATIONS)
    return result.rowcount


def check_reserved_quantity_drift(session: Session, *, sample_limit: int = 20) -> DriftReport:
    """Compare cached reservations against `stock_allocations`, changing nothing.

    Run nightly beside `check_lot_balance_drift`. Non-zero is a correctness
    alert: an over-stated reservation reads as missing stock, so it hides parts
    the user owns, and an under-stated one lets two builds promise the same
    parts.
    """
    drift_count = session.execute(_COUNT_RESERVED_DRIFT).scalar_one()
    sample: tuple[int, ...] = ()
    if drift_count:
        rows = session.execute(_DRIFTING_RESERVED_LOT_IDS, {"limit": sample_limit}).scalars().all()
        sample = tuple(rows)

    report = DriftReport(cache_name=RESERVATIONS, drift_count=drift_count, sample_ids=sample)
    _record_check(session, report)
    return report


TAG_BINDINGS = "tag_bindings"

_COUNT_DUPLICATE_TAG_UIDS = text(
    "SELECT COUNT(*) FROM ("
    "  SELECT tag_uid FROM location_tags"
    "  WHERE tag_uid IS NOT NULL"
    "  GROUP BY tag_uid HAVING COUNT(*) > 1"
    ")"
)

_LOCATIONS_SHARING_A_TAG_UID = text(
    "SELECT location_id FROM location_tags"
    " WHERE tag_uid IN ("
    "   SELECT tag_uid FROM location_tags"
    "   WHERE tag_uid IS NOT NULL"
    "   GROUP BY tag_uid HAVING COUNT(*) > 1"
    " )"
    " ORDER BY tag_uid, location_id LIMIT :limit"
)


def check_duplicate_tag_uids(session: Session, *, sample_limit: int = 20) -> DriftReport:
    """Count physical tags bound to more than one container. Repairs nothing.

    **Not a cache check — the only one here that isn't.** `location_tags` is the
    record, not a derivation of anything, so there is nothing to rebuild it from
    and `POST /api/system/caches/rebuild` deliberately cannot touch it. It is
    reported through the same machinery because this is where an operator already
    looks, and because ADR 0013's rule applies with more force here than anywhere
    else: an automatic repair would have to *choose* which drawer keeps the tag,
    and getting that wrong silently is exactly the harm being detected.

    One UID on two rows means `tag_with_uid` — which resolves by lowest `id` —
    hands the station a container the tag is not on, so stock is committed into
    the wrong drawer while both containers' pages look correct. `bind` has always
    refused to create one and `undo` stopped being able to (see
    `provisioning.undo`'s `prior_tag_bound_elsewhere`), but neither of those
    heals a row already written, and the fix landed after the bug had been live.

    This is also the **missing precondition for a unique index** on
    `location_tags.tag_uid`. A `CREATE UNIQUE INDEX` migration fails outright on a
    database that already holds a duplicate, so somebody has to know whether one
    exists — and, if it does, decide which binding is the real one — before that
    migration can be written. `sample_ids` are the location ids sharing a UID, so
    the answer names the drawers to go and look at.
    """
    drift_count = session.execute(_COUNT_DUPLICATE_TAG_UIDS).scalar_one()
    sample: tuple[int, ...] = ()
    if drift_count:
        rows = (
            session.execute(_LOCATIONS_SHARING_A_TAG_UID, {"limit": sample_limit}).scalars().all()
        )
        sample = tuple(rows)

    report = DriftReport(cache_name=TAG_BINDINGS, drift_count=drift_count, sample_ids=sample)
    _record_check(session, report)
    return report


def _record_check(session: Session, report: DriftReport) -> None:
    detail = ",".join(str(i) for i in report.sample_ids) if report.sample_ids else None
    session.execute(
        text(
            "UPDATE cache_state"
            " SET last_checked_at = :now, drift_count = :drift, detail = :detail"
            " WHERE name = :name"
        ),
        {
            "now": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "drift": report.drift_count,
            "detail": detail,
            "name": report.cache_name,
        },
    )
