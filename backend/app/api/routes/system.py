"""System routes: health, cache maintenance, and (later) backup/restore.

## Why maintenance is an API route and not a script

`app.db.maintenance` has done the work since the core schema landed; nothing
ever called it outside tests. The obvious fix — a CronJob running
`python -m app.db.maintenance` against the volume — is the one shape this
deployment cannot have. The datastore is SQLite on a ReadWriteOnce volume with
**exactly one writer**, and a rebuild writes. A second pod opening the same file
to write is corruption, which is why `deploy/base/backup.yaml` goes out of its
way to open the database `mode=ro`.

So the writer stays the API and the schedule reaches it over HTTP, the same
division ADR 0005 draws for the extraction worker. `app/scripts/maintenance.py`
is a client of these routes, not a second writer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.db import maintenance
from app.db.session import get_db
from app.models.system import CacheState

router = APIRouter(prefix="/api/system", tags=["system"])


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    #: Current Alembic revision, or None when migrations have never been applied.
    #: A running API on `None` means `alembic upgrade head` has not been run.
    schema_revision: str | None


@router.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return HealthResponse(
            status="degraded",
            version=__version__,
            database="unreachable",
            schema_revision=None,
        )

    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except SQLAlchemyError:
        # Table absent: a database that exists but has never been migrated.
        revision = None

    return HealthResponse(
        status="ok",
        version=__version__,
        database="ok",
        schema_revision=revision,
    )


class CacheStateRead(BaseModel):
    """One row of `cache_state` — what the last check and rebuild found."""

    name: str
    is_dirty: bool
    last_rebuilt_at: datetime | None
    last_checked_at: datetime | None
    #: Non-zero is a correctness alert, not a performance note: a write path
    #: updated the source of truth and the cache inconsistently, so numbers the
    #: UI has been showing were wrong.
    drift_count: int
    #: Bounded sample of the disagreeing row ids, comma-separated.
    detail: str | None


@router.get("/caches", response_model=list[CacheStateRead])
def read_caches(db: Session = Depends(get_db)) -> list[CacheStateRead]:
    """Report every derived cache's freshness and last drift result.

    Read-only, and the reason `cache_state` exists: the design's claim is that
    drift is "a visible number rather than a mystery", and until something served
    the table the number was recorded where nobody could see it.
    """
    rows = db.execute(select(CacheState).order_by(CacheState.name)).scalars().all()
    return [
        CacheStateRead(
            name=row.name,
            is_dirty=row.is_dirty,
            last_rebuilt_at=row.last_rebuilt_at,
            last_checked_at=row.last_checked_at,
            drift_count=row.drift_count,
            detail=row.detail,
        )
        for row in rows
    ]


class DriftRead(BaseModel):
    cache_name: str
    drift_count: int
    #: Bounded, so a systemic failure does not return a 200k-element array.
    sample_ids: list[int]


class MaintenanceRun(BaseModel):
    """What the nightly pass repaired, and what it only reported."""

    #: Locations whose occupancy was recomputed because a trigger flagged them.
    occupancy_rebuilt: int
    #: One entry per drift-checked cache. Reported, never silently repaired.
    drift: list[DriftRead]
    #: True when any checked cache disagreed with its source of truth. The
    #: caller is expected to treat this as a failure — see
    #: `app/scripts/maintenance.py` on why that is the alerting channel.
    has_drift: bool


@router.post("/maintenance", response_model=MaintenanceRun)
def run_maintenance(db: Session = Depends(get_db)) -> MaintenanceRun:
    """The nightly pass: repair routine staleness, report suspected bugs.

    The two halves are deliberately not the same operation, and conflating them
    would destroy the only evidence that matters:

    * **Occupancy is rebuilt.** `location_occupancy` is *designed* to go stale —
      `AFTER INSERT ON stock_ledger` and `AFTER UPDATE OF location_id ON
      stock_lots` mark rows dirty and leave the recompute to a batch pass,
      because doing it inline would put a tree walk on every ledger write. A
      dirty row is the mechanism working, so rebuilding it is routine. It also
      matters more than it sounds: `locations.is_overfull` is written *only*
      here, so until something ran this, the "capacity is advisory, flag the
      location, suggest a defrag" behaviour could never fire, and the storage
      map's fill ratios were served from a table with no rows in it.

    * **Balances and reservations are only checked.** Both are maintained
      incrementally on every write by `services/ledger.py` and
      `services/reservations.py`. Drift there is not expected staleness, it is a
      bug in a write path — and quietly rebuilding it every night would erase
      the symptom while leaving the cause, so the wrong numbers would come back
      the next day with nothing recorded. `POST /caches/rebuild` is the repair,
      run deliberately once the cause is known.
    """
    occupancy_rebuilt = maintenance.rebuild_location_occupancy(db, only_dirty=True)
    reports = [
        maintenance.check_lot_balance_drift(db),
        maintenance.check_reserved_quantity_drift(db),
        # Not a cache, and reported here anyway: one physical tag bound to two
        # containers sends the station to the wrong drawer, and nothing else
        # looks for it. `caches/rebuild` cannot repair it — deciding which
        # drawer keeps the tag is a person's call.
        maintenance.check_duplicate_tag_uids(db),
    ]
    db.commit()
    return MaintenanceRun(
        occupancy_rebuilt=occupancy_rebuilt,
        drift=[
            DriftRead(
                cache_name=report.cache_name,
                drift_count=report.drift_count,
                sample_ids=list(report.sample_ids),
            )
            for report in reports
        ],
        has_drift=any(not report.is_clean for report in reports),
    )


#: The caches a full rebuild can reconstruct. Deliberately not every name in
#: `cache_state`: `location_tree` and `category_tree` are rebuilt through
#: `services/tree.py` on the writes that invalidate them and have no drift check
#: to pair with, so offering them here would imply a check that does not exist.
RebuildableCache = Literal["lot_balances", "reservations", "location_occupancy"]

_REBUILDS: dict[str, Callable[[Session], int]] = {
    maintenance.LOT_BALANCES: maintenance.rebuild_lot_balances,
    maintenance.RESERVATIONS: maintenance.rebuild_reserved_quantities,
    # Unconditional here, not `only_dirty`: this route is what you reach for when
    # the cache is *suspect*, and a row that is wrong without being flagged is
    # exactly the case a dirty-only pass cannot reach.
    maintenance.LOCATION_OCCUPANCY: maintenance.rebuild_location_occupancy,
}


class RebuildRequest(BaseModel):
    #: Empty means all of them.
    caches: list[RebuildableCache] = []


class RebuildResult(BaseModel):
    cache_name: str
    #: Rows recomputed. Every row, deliberately, not only the wrong ones — the
    #: case most needing repair is a counter that is *too high* because a
    #: decrement never happened, and that row has nothing left to find.
    rows_touched: int


class RebuildResponse(BaseModel):
    rebuilt: list[RebuildResult]


@router.post("/caches/rebuild", response_model=RebuildResponse)
def rebuild_caches(
    body: RebuildRequest | None = None, db: Session = Depends(get_db)
) -> RebuildResponse:
    """Reconstruct a derived cache from its source of truth.

    The escape hatch the whole three-tier stock model rests on. Reading balances
    from `stock_lots.qty_milli_cached` instead of summing the ledger is only safe
    because one statement can rebuild the cache from the append-only record,
    which cannot itself have been edited — so a cache bug is a stale number, never
    lost data.

    Explicit rather than nightly on purpose. See `run_maintenance`: repairing
    drift on a schedule hides the write-path bug that caused it.
    """
    requested = list(body.caches) if body is not None and body.caches else list(_REBUILDS)
    # dict order, not request order, so the response is stable and a caller
    # naming a cache twice does not rebuild it twice.
    results = [
        RebuildResult(cache_name=name, rows_touched=_REBUILDS[name](db))
        for name in _REBUILDS
        if name in requested
    ]
    db.commit()
    return RebuildResponse(rebuilt=results)
