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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.db import maintenance
from app.db.session import get_db
from app.models.system import CacheState
from app.services import documents, model_servers

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


class LeaseSweepRead(BaseModel):
    """One work queue's expired-lease sweep."""

    queue: str
    #: Claims whose lease ran out with no attempts left, moved to that queue's
    #: failure state. Repaired, because an expired lease is designed staleness.
    failed: int
    #: Expired leases with attempts left, deliberately untouched — already
    #: claimable. **The number worth watching**: a queue nothing is draining looks
    #: healthy from its own depth, because the rows read as `claimed`.
    stalled: int


class MaintenanceRun(BaseModel):
    """What the nightly pass repaired, and what it only reported."""

    #: Locations whose occupancy was recomputed because a trigger flagged them.
    occupancy_rebuilt: int
    #: Per queue, what the abandoned-lease sweep did. Repair, not report — see
    #: `app.db.maintenance.sweep_abandoned_leases` for why a lease belongs on this
    #: side of ADR 0013's split, and why the dispatch queue makes it necessary.
    lease_sweeps: list[LeaseSweepRead] = []
    #: True when any queue still holds an expired lease it could not repair. Not
    #: folded into `has_drift`: drift is a wrong number and this is a stopped
    #: worker, and a nightly Job that cannot tell them apart sends whoever reads it
    #: to the wrong place.
    has_stalled_leases: bool = False
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

    * **Abandoned queue leases are swept.** Also repair, and for the same reason:
      a lease is deliberately not a lock, it expires on its own so a worker may be
      killed without anybody noticing, and collecting the expired ones is the
      mechanism working. The queues already do this at the top of every `claim` —
      but that is a repair which happens *as a side effect of use*, and the
      dispatch queue is opt-in with no scheduled worker, so "the next claim will
      sweep it" can mean never. See
      `app.db.maintenance.sweep_abandoned_leases`.

    * **Balances and reservations are only checked.** Both are maintained
      incrementally on every write by `services/ledger.py` and
      `services/reservations.py`. Drift there is not expected staleness, it is a
      bug in a write path — and quietly rebuilding it every night would erase
      the symptom while leaving the cause, so the wrong numbers would come back
      the next day with nothing recorded. `POST /caches/rebuild` is the repair,
      run deliberately once the cause is known.
    """
    occupancy_rebuilt = maintenance.rebuild_location_occupancy(db, only_dirty=True)
    # Before the drift checks, deliberately. A sweep can move a claim into a
    # failure state, and a person reading this output should see the queue's
    # settled shape rather than one caught mid-repair.
    sweeps = maintenance.sweep_abandoned_leases(db)
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
        lease_sweeps=[
            LeaseSweepRead(queue=sweep.queue, failed=sweep.failed, stalled=sweep.stalled)
            for sweep in sweeps
        ],
        has_stalled_leases=any(sweep.stalled for sweep in sweeps),
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


class BlobScrubResponse(BaseModel):
    """What re-hashing the blob store found. Complete counts, not a sample."""

    checked: int
    #: Addresses whose file is gone. Recoverable: the row still records what the
    #: file was, so re-uploading the same bytes repairs it.
    missing: list[str]
    #: Present but not hashing to its own name. **The dangerous one** — it is
    #: served as authoritative, cached `immutable`, and every future upload of
    #: the correct bytes dedups onto it.
    corrupt: list[str]


class RebuildResponse(BaseModel):
    rebuilt: list[RebuildResult]


@router.post("/blobs/scrub", response_model=BlobScrubResponse)
def scrub_blobs(db: Session = Depends(get_db)) -> BlobScrubResponse:
    """Re-hash every stored blob against its own name. Reads only; repairs nothing.

    **Its own route, deliberately not part of `POST /maintenance`.** Every blob is
    read in full, so this is I/O-bound in the size of the store — putting a
    gigabyte of datasheet hashing inside the nightly pass would make the pass that
    rebuilds occupancy and checks two caches take as long as the slowest disk in
    the deployment. Separate call, separate schedule.

    It has to be a route at all for the same reason the nightly pass is: the
    CronJob has no volume mount and there is exactly one process holding the
    ReadWriteOnce disk. `app.scripts.maintenance --scrub` is what calls it.

    Nothing is deleted or rewritten. A blob is re-fetchable — it is a PDF on a
    manufacturer's website — while the row's metadata, its links and its extracted
    text are not, so turning one bad sector into a deleted document would trade a
    recoverable failure for an unrecoverable one.
    """
    report = documents.scrub(db)
    return BlobScrubResponse(
        checked=report.checked,
        missing=list(report.missing),
        corrupt=list(report.corrupt),
    )


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


# ---------------------------------------------------------------------------
# Model servers
#
# Here rather than under `/api/chat` deliberately. Chat is the loudest consumer
# of a model, but not the only one — datasheet extraction reads with the small
# rung — and what these routes control is a *deployment holding a GPU*, which is
# infrastructure in the same sense as the cache and backup doors above.
#
# `/api/chat/models` stays what it is: the picker's list, answering "what may I
# choose and is it ready". These answer "what is on, and turn it on or off".
# ---------------------------------------------------------------------------


class ModelHeldRead(BaseModel):
    """One model a server can serve."""

    id: str
    label: str
    size_b: int
    #: The server named this model when asked. False while weights are loading,
    #: and false for a model that was never pulled — which is not the same as the
    #: server being down, and is why this is per model rather than per server.
    loaded: bool


class ModelServerRead(BaseModel):
    """One model server: what it is doing, and what it holds."""

    id: str
    label: str
    #: The Kubernetes Deployment behind it, or None if this install has no name
    #: for it. Shown so `kubectl` and this screen are talking about the same thing.
    deployment: str | None
    state: model_servers.ServerState
    #: Replicas asked for, and ready. Both None when the cluster cannot be read,
    #: which is **not** the same as zero — see `state == "unknown"`.
    desired_replicas: int | None
    ready_replicas: int | None
    #: Whether this server is claiming the GPU, including while starting up. At
    #: most one server can, so this is also the answer to "why is the other one
    #: not coming up".
    holds_gpu: bool
    models: list[ModelHeldRead]


class ModelServerListRead(BaseModel):
    """Every model server, plus whether this install may change anything.

    `controllable` is false on a dev box and anywhere the API has no permission to
    scale. The list still renders — what is running is answered by asking the
    servers — but Start and Stop would only fail, so the UI hides them and shows
    `hint` instead.
    """

    servers: list[ModelServerRead]
    controllable: bool
    #: The command that does this from a laptop, when `controllable` is false.
    hint: str | None


class ModelSwitchResponse(BaseModel):
    """What a start or stop did. Always carries the fresh list, so one round trip
    both acts and refreshes — a UI that re-fetched separately would show the state
    from before its own click as often as not."""

    ok: bool
    #: One sentence to show the person: what is happening and roughly how long, or
    #: why nothing happened.
    detail: str
    #: Servers scaled to zero. A start releases the others to free the card; a stop
    #: names itself.
    released: list[str]
    servers: list[ModelServerRead]


def _model_server_read(status: model_servers.ServerStatus) -> ModelServerRead:
    return ModelServerRead(
        id=status.server.id,
        label=status.server.label,
        deployment=status.server.deployment,
        state=status.state,
        desired_replicas=status.desired_replicas,
        ready_replicas=status.ready_replicas,
        holds_gpu=status.holds_gpu,
        models=[
            ModelHeldRead(
                id=held.choice.id,
                label=held.choice.label,
                size_b=held.choice.size_b,
                loaded=held.loaded,
            )
            for held in status.models
        ],
    )


def _model_server_list() -> ModelServerListRead:
    controllable = model_servers.controllable()
    return ModelServerListRead(
        servers=[_model_server_read(status) for status in model_servers.statuses()],
        controllable=controllable,
        hint=None if controllable else "make k8s-model M=8b|27b|off",
    )


@router.get("/models", response_model=ModelServerListRead)
def read_model_servers() -> ModelServerListRead:
    """Which model servers exist and which are actually up.

    Costs one TCP handshake plus one small HTTP request per server, and one cluster
    read each. Slower than a static list on purpose: the alternative is a screen
    that says a model is running because it is configured, which is wrong most of
    the time — both servers default to zero and the reaper releases the GPU on idle.
    """
    return _model_server_list()


def _require_model_server(server_id: str) -> model_servers.ModelServer:
    server = model_servers.by_id(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail=f"No model server '{server_id}'")
    return server


@router.post("/models/{server_id}/start", response_model=ModelSwitchResponse)
def start_model_server(server_id: str) -> ModelSwitchResponse:
    """Bring a model server up, releasing the other one to free the GPU.

    Returns as soon as the cluster accepts the request; it does not wait for
    weights to load, which takes minutes for the 27B and would time out in every
    proxy between here and the browser. `state` then reads `starting` until the
    server answers, which is what a polling screen shows.

    Answers 200 with `ok: false` rather than an error status when scaling is not
    possible here: nothing broke, this install simply cannot do it, and `detail`
    carries the command that can.
    """
    result = model_servers.start(_require_model_server(server_id))
    return ModelSwitchResponse(
        ok=result.ok,
        detail=result.detail,
        released=list(result.released),
        servers=_model_server_list().servers,
    )


@router.post("/models/{server_id}/stop", response_model=ModelSwitchResponse)
def stop_model_server(server_id: str) -> ModelSwitchResponse:
    """Scale a model server to zero, freeing the GPU now.

    The reaper already does this on idle, but idle is tens of minutes and the reason
    to stop a model is usually that something else needs the card *now* — a
    co-tenant's build, or the other model.
    """
    result = model_servers.stop(_require_model_server(server_id))
    return ModelSwitchResponse(
        ok=result.ok,
        detail=result.detail,
        released=list(result.released),
        servers=_model_server_list().servers,
    )
