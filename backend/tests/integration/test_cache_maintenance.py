"""The nightly maintenance pass, through the routes that run it.

Against real Alembic migrations (see `tests/conftest.py`), which is the whole
point here: the triggers that flag occupancy dirty are created in a migration and
invisible to the models, so `create_all()` would test a schema where the thing
under test does not exist.

The load-bearing assertion in this file is
`test_maintenance_reports_balance_drift_without_repairing_it`. Everything else
is scaffolding around it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.enums import CapacityModel, LedgerKind
from app.models.stock import StockLot
from app.models.storage import Location, LocationOccupancy
from app.models.system import CacheState
from app.scripts import maintenance as script
from app.services.tree import location_tree
from tests.factories import make_container_type, make_location, make_lot, make_part, post


def _cache(db: Session, name: str) -> CacheState:
    db.expire_all()
    return db.execute(select(CacheState).where(CacheState.name == name)).scalar_one()


def _occupancy(db: Session, location_id: int) -> LocationOccupancy:
    db.expire_all()
    return db.execute(
        select(LocationOccupancy).where(LocationOccupancy.location_id == location_id)
    ).scalar_one()


def _drift_by_name(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["cache_name"]: entry for entry in body["drift"]}


def _lot_with_a_broken_cache(db: Session) -> StockLot:
    """A lot whose ledger says 1000 and whose cache says 7000.

    Built by posting correctly and *then* corrupting the cache, rather than by
    inserting a ledger row and skipping the cache update. Same end state, but this
    way the test never hand-writes ledger SQL — which is what the append-only
    triggers exist to prevent anything from doing.
    """
    lot = make_lot(db, make_part(db), make_location(db))
    post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()
    db.execute(update(StockLot).where(StockLot.id == lot.id).values(qty_milli_cached=7000))
    db.commit()
    return lot


def _overfull_location(db: Session) -> Location:
    """A 1mm^3 tray holding one 10mm^3 part — over capacity by 10x."""
    container_type = make_container_type(
        db,
        "tiny-tray",
        capacity_model=CapacityModel.VOLUME,
        inner_length_mm=1.0,
        inner_width_mm=1.0,
        inner_height_mm=1.0,
        default_fill_factor=1.0,
    )
    location = make_location(db, "Tiny tray", container_type_id=container_type.id)
    part = make_part(db)
    part.unit_volume_mm3 = 10.0
    db.flush()
    lot = make_lot(db, part, location)
    post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()
    return location


# ---------------------------------------------------------------------------
# Reading cache state
# ---------------------------------------------------------------------------


def test_caches_route_reports_every_seeded_cache(client: TestClient) -> None:
    response = client.get("/api/system/caches")
    assert response.status_code == 200
    names = {row["name"] for row in response.json()}
    # The five the core-schema migration seeds. A cache added later without a
    # `cache_state` row has nowhere to record its drift, so this is worth pinning.
    assert names == {
        "lot_balances",
        "location_tree",
        "category_tree",
        "location_occupancy",
        "reservations",
    }


def test_caches_route_exposes_what_the_last_check_found(client: TestClient, db: Session) -> None:
    lot = _lot_with_a_broken_cache(db)

    client.post("/api/system/maintenance")

    row = next(r for r in client.get("/api/system/caches").json() if r["name"] == "lot_balances")
    assert row["drift_count"] == 1
    assert row["last_checked_at"] is not None
    assert row["detail"] == str(lot.id)


# ---------------------------------------------------------------------------
# What the nightly pass repairs
# ---------------------------------------------------------------------------


def test_maintenance_rebuilds_occupancy_flagged_dirty_by_a_trigger(
    client: TestClient, db: Session
) -> None:
    tree = location_tree(db)
    cabinet = tree.insert_and_index(Location(name="Cabinet"))
    drawer = tree.insert_and_index(Location(name="Drawer", parent_id=cabinet.id))
    db.commit()

    # Every location starts dirty from the seeding trigger.
    assert _occupancy(db, drawer.id).is_dirty is True

    response = client.post("/api/system/maintenance")
    assert response.status_code == 200
    assert response.json()["occupancy_rebuilt"] >= 2

    assert _occupancy(db, drawer.id).is_dirty is False
    assert _occupancy(db, cabinet.id).is_dirty is False
    assert _cache(db, "location_occupancy").last_rebuilt_at is not None


def test_maintenance_is_what_makes_a_location_overfull(client: TestClient, db: Session) -> None:
    """The reason this job's absence was a live defect, not a missing nicety.

    `locations.is_overfull` is written *only* by the occupancy rebuild, and
    `services/assignment.py` scores against it. With nothing running the rebuild
    the flag stayed false forever, so "accept the put-away, flag the location,
    suggest a defrag" could never fire.
    """
    location = _overfull_location(db)
    assert location.is_overfull is False

    client.post("/api/system/maintenance")

    db.expire_all()
    refreshed = db.get(Location, location.id)
    assert refreshed is not None
    assert refreshed.is_overfull is True
    assert _occupancy(db, location.id).fill_ratio == pytest.approx(10.0)


def test_maintenance_serves_a_fill_ratio_the_storage_map_can_show(
    client: TestClient, db: Session
) -> None:
    """`GET /api/locations/tree` reads `fill_ratio` from `location_occupancy`, so
    with no rebuild ever run the map had no fill data for any container."""
    location = _overfull_location(db)

    client.post("/api/system/maintenance")

    tree = client.get("/api/locations/tree").json()
    node = next(n for n in tree["nodes"] if n["id"] == location.id)
    assert node["fill_ratio"] == pytest.approx(10.0)
    assert node["is_overfull"] is True


# ---------------------------------------------------------------------------
# What the nightly pass only reports
# ---------------------------------------------------------------------------


def test_maintenance_reports_balance_drift_without_repairing_it(
    client: TestClient, db: Session
) -> None:
    """Drift is recorded and left alone.

    Repairing it on a schedule would erase the only evidence that a write path is
    broken, and the wrong numbers would come back the next day with nothing to
    show where from. The repair is a separate, deliberate call.
    """
    lot = _lot_with_a_broken_cache(db)

    body = client.post("/api/system/maintenance").json()

    assert body["has_drift"] is True
    assert _drift_by_name(body)["lot_balances"]["drift_count"] == 1
    assert _drift_by_name(body)["lot_balances"]["sample_ids"] == [lot.id]

    db.expire_all()
    stored = db.get(StockLot, lot.id)
    assert stored is not None
    assert stored.qty_milli_cached == 7000, "the nightly pass must not have repaired it"


def test_maintenance_on_a_consistent_database_finds_nothing(
    client: TestClient, db: Session
) -> None:
    lot = make_lot(db, make_part(db), make_location(db))
    post(db, lot, 4000, LedgerKind.RECEIVE)  # ledger and cache together
    db.commit()

    body = client.post("/api/system/maintenance").json()

    assert body["has_drift"] is False
    assert all(entry["drift_count"] == 0 for entry in body["drift"])
    assert _drift_by_name(body).keys() == {"lot_balances", "reservations"}


# ---------------------------------------------------------------------------
# The explicit repair
# ---------------------------------------------------------------------------


def test_rebuild_restores_a_balance_from_the_ledger(client: TestClient, db: Session) -> None:
    lot = _lot_with_a_broken_cache(db)

    response = client.post("/api/system/caches/rebuild", json={"caches": ["lot_balances"]})
    assert response.status_code == 200
    assert response.json()["rebuilt"] == [{"cache_name": "lot_balances", "rows_touched": 1}]

    db.expire_all()
    stored = db.get(StockLot, lot.id)
    assert stored is not None
    assert stored.qty_milli_cached == 1000
    assert client.post("/api/system/maintenance").json()["has_drift"] is False


def test_rebuild_with_no_body_rebuilds_everything(client: TestClient, db: Session) -> None:
    make_lot(db, make_part(db), make_location(db))
    db.commit()

    body = client.post("/api/system/caches/rebuild").json()
    names = [entry["cache_name"] for entry in body["rebuilt"]]
    assert names == ["lot_balances", "reservations", "location_occupancy"]


def test_rebuild_rejects_a_cache_it_cannot_reconstruct(client: TestClient) -> None:
    """`location_tree` has no drift check to pair with, so offering it would imply
    one that does not exist."""
    response = client.post("/api/system/caches/rebuild", json={"caches": ["location_tree"]})
    assert response.status_code == 422


def test_rebuild_named_twice_runs_once(client: TestClient, db: Session) -> None:
    make_lot(db, make_part(db), make_location(db))
    db.commit()
    body = client.post(
        "/api/system/caches/rebuild", json={"caches": ["lot_balances", "lot_balances"]}
    ).json()
    assert body["rebuilt"] == [{"cache_name": "lot_balances", "rows_touched": 1}]


# ---------------------------------------------------------------------------
# The CronJob's client, driven against the real routes
# ---------------------------------------------------------------------------


@pytest.fixture
def through_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the script's one HTTP call at `TestClient`.

    So the exit-code logic is exercised against the real routes rather than a
    hand-written stub of them — the thing that would rot is the shape of the
    response, and a stub cannot notice that.
    """

    def post_json(base_url: str, path: str, payload: dict[str, Any], *, timeout: float) -> Any:
        response = client.post(f"/{path}", json=payload)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(script, "post_json", post_json)


@pytest.mark.usefixtures("through_client")
def test_script_exits_zero_on_a_clean_database() -> None:
    assert script.main(["--base-url", "http://testserver"]) == 0


@pytest.mark.usefixtures("through_client")
def test_script_exits_nonzero_on_drift(db: Session) -> None:
    """The alerting channel: with no metrics stack, a failed Job is the only thing
    that surfaces a nightly correctness problem."""
    _lot_with_a_broken_cache(db)

    assert script.main(["--base-url", "http://testserver"]) == 1
    # Same finding, but the operator has said they are already looking at it.
    assert script.main(["--base-url", "http://testserver", "--allow-drift"]) == 0


@pytest.mark.usefixtures("through_client")
def test_script_rebuild_flag_repairs_and_reports(db: Session) -> None:
    lot = _lot_with_a_broken_cache(db)

    assert script.main(["--base-url", "http://testserver", "--rebuild", "lot_balances"]) == 0

    db.expire_all()
    stored = db.get(StockLot, lot.id)
    assert stored is not None
    assert stored.qty_milli_cached == 1000


def test_script_exits_two_when_the_api_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinguished from drift deliberately: 2 is "the check could not run", 1 is
    "it ran and found something". Only one of those is a data problem."""
    import urllib.error

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(script, "post_json", unreachable)
    assert script.main(["--base-url", "http://nowhere:8000"]) == 2
