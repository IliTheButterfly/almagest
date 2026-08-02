"""What marks a cabinet's stored fill stale, and whether the nightly pass sees it.

Three rounds of review have now landed on the same defect, and it survived twice
because the test that was supposed to pin it never reproduced the sequence that
breaks. This module exists to reproduce that sequence and nothing else.

**The sequence matters more than the assertion.** `trg_locations_seed_occupancy`
marks every newly inserted `locations` row dirty, so a test that creates a
cabinet, creates its drawers and *then* runs the pass is picked up by the seed
flag whatever the production code does — it passes on the broken code and proves
nothing. The real shape is:

1. create the cabinet and whatever it starts with,
2. run the pass, which clears every seeded dirty flag,
3. **then** change what is inside it,
4. run the pass again.

Step 3 is the Tuesday that follows Monday's step 2, and step 4 is the only thing
that can notice. `mark_location_occupancy_dirty` is what connects them; with it
removed the pass uses `only_dirty=True` and skips the cabinet for ever, the
storage map reads 0% for a full cabinet while its own page reads 100%, and
`is_overfull` can never be set because only the bulk path writes it.

Every test here therefore drives `/api/system/maintenance` — the pass that
actually runs on a schedule — and never `/api/system/caches/rebuild`, which is
the escape hatch for a cache already believed to be wrong. Each one also changes
the tree through the **production** path (the HTTP route, or `TreeRepository`
where no route reaches), because `tests.factories.make_location` inserts a
`Location` directly and so bypasses the very call being pinned.

Each test was mutation-checked by stubbing out `TreeRepository._mark_occupancy_dirty`
and `removal._mark_parent_occupancy_dirty`; all of them go red.

Real Alembic migrations, per `tests/conftest.py`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.storage import Location
from app.services.tree import location_tree
from tests.factories import make_container_type, make_lot, make_part


def _cabinet(client: TestClient, db: Session, *, slug: str, slots: int, drawers: int) -> int:
    """A cabinet with `drawers` children, its occupancy already settled.

    Returns after the pass has run, so every seeded dirty flag is clear and the
    next change has to mark itself.
    """
    kind = make_container_type(db, slug, capacity_model="slots", capacity_slots=slots)
    db.commit()

    created = client.post(
        "/api/locations", json={"name": f"Cabinet {slug}", "container_type_id": kind.id}
    )
    assert created.status_code == 201, created.text
    cabinet_id = int(created.json()["location"]["id"])

    for index in range(drawers):
        child = client.post(
            "/api/locations", json={"name": f"Drawer {index}", "parent_id": cabinet_id}
        )
        assert child.status_code == 201, child.text

    assert client.post("/api/system/maintenance").status_code == 200
    return cabinet_id


def _live_ratio(client: TestClient, cabinet_id: int) -> float | None:
    ratio = client.get(f"/api/locations/{cabinet_id}").json()["capacity"]["fill_ratio"]
    return None if ratio is None else float(ratio)


def _node_ratio(client: TestClient, cabinet_id: int) -> float | None:
    node = next(
        n for n in client.get("/api/locations/tree").json()["nodes"] if n["id"] == cabinet_id
    )
    ratio = node["fill_ratio"]
    return None if ratio is None else float(ratio)


def _assert_agree(client: TestClient, cabinet_id: int, expected: float) -> None:
    """The map and the container's own page, about the same cabinet.

    Both are asserted against `expected` rather than only against each other: two
    reads that agree on a wrong number would satisfy a bare equality, and the
    live read is the one that was always right.
    """
    live = _live_ratio(client, cabinet_id)
    node = _node_ratio(client, cabinet_id)
    assert live == expected, f"live read {live}, expected {expected}"
    assert node == expected, f"map read {node}, page read {live}"


def test_adding_a_drawer_later_restales_the_cabinet(client: TestClient, db: Session) -> None:
    """Monday: a cabinet with three of four slots filled, pass runs. Tuesday: a
    fourth drawer goes in.

    Without a production caller for `mark_location_occupancy_dirty`, nothing has
    told the pass the cabinet changed, so `only_dirty=True` skips it and the map
    keeps drawing Monday's meter for ever.
    """
    cabinet_id = _cabinet(client, db, slug="cab-add", slots=4, drawers=3)
    _assert_agree(client, cabinet_id, 0.75)

    added = client.post("/api/locations", json={"name": "Drawer 3", "parent_id": cabinet_id})
    assert added.status_code == 201, added.text

    assert client.post("/api/system/maintenance").status_code == 200
    _assert_agree(client, cabinet_id, 1.0)


def test_moving_a_drawer_restales_both_cabinets(client: TestClient, db: Session) -> None:
    """A reparent empties a slot at one end and fills one at the other.

    Driven through `TreeRepository.move` rather than a route: it is the only
    reparent path in the codebase, and today it is reachable over HTTP for
    `part_categories` only. Pinning it at the service layer is what stops the
    location half rotting before the route that will use it arrives.
    """
    source_id = _cabinet(client, db, slug="cab-from", slots=4, drawers=2)
    target_id = _cabinet(client, db, slug="cab-to", slots=4, drawers=1)
    _assert_agree(client, source_id, 0.5)
    _assert_agree(client, target_id, 0.25)

    drawer = next(d for d in db.query(Location).filter(Location.parent_id == source_id).all())
    location_tree(db).move(drawer, target_id)
    db.commit()

    assert client.post("/api/system/maintenance").status_code == 200
    # Both ends: the old parent lost a slot and the new one gained it. A fix that
    # marked only the destination would leave the source reading 0.5 for ever.
    _assert_agree(client, source_id, 0.25)
    _assert_agree(client, target_id, 0.5)


def test_retiring_a_drawer_restales_its_cabinet(client: TestClient, db: Session) -> None:
    """A drawer that ever held stock cannot be deleted, so it retires — and a
    retired container is in no parent's slot count.

    The empty lot is load-bearing, not scenery. Without something referencing it,
    `plan_removal` hard-*deletes* the drawer and this test silently becomes a
    second copy of `test_deleting_a_drawer_restales_its_cabinet` — which is
    precisely what the first draft of it did, caught by mutating the delete
    branch alone and watching this test fail with it. `retired_location_ids` is
    asserted so that can never go unnoticed again.
    """
    cabinet_id = _cabinet(client, db, slug="cab-retire", slots=4, drawers=2)
    _assert_agree(client, cabinet_id, 0.5)

    drawer = db.query(Location).filter(Location.parent_id == cabinet_id).all()[0]
    drawer_id = drawer.id
    make_lot(db, make_part(db, name="Was here"), drawer, qty_milli=0)
    db.commit()

    removed = client.delete(f"/api/locations/{drawer_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["retired_location_ids"] == [drawer_id], (
        "this test only covers the retire branch if the drawer was actually retired"
    )

    assert client.post("/api/system/maintenance").status_code == 200
    _assert_agree(client, cabinet_id, 0.25)


def test_deleting_a_drawer_restales_its_cabinet(client: TestClient, db: Session) -> None:
    """The path round 8 missed: a hard delete, not a retire.

    `apply_removal` picks between the two per node, and only the retire branch
    marked the parent. A drawer nothing references is deleted outright, which
    empties a slot exactly as a retire does — so the cabinet went stale with
    nothing to say so, and stayed that way until somebody ran the manual rebuild.

    The drawer here is created and never used, which is what makes `plan_removal`
    choose `delete`; the assertion below would hold for a retire too, so
    `deleted_location_ids` is checked to prove which branch ran.
    """
    cabinet_id = _cabinet(client, db, slug="cab-delete", slots=4, drawers=2)
    _assert_agree(client, cabinet_id, 0.5)

    drawer_id = db.query(Location).filter(Location.parent_id == cabinet_id).all()[0].id
    removed = client.delete(f"/api/locations/{drawer_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted_location_ids"] == [drawer_id], (
        "this test only covers the delete branch if the drawer was actually deleted"
    )

    assert client.post("/api/system/maintenance").status_code == 200
    _assert_agree(client, cabinet_id, 0.25)


def test_restoring_a_drawer_restales_its_cabinet(client: TestClient, db: Session) -> None:
    """Coming back into the tree fills the slot again."""
    cabinet_id = _cabinet(client, db, slug="cab-restore", slots=4, drawers=2)

    drawer = db.query(Location).filter(Location.parent_id == cabinet_id).all()[0]
    drawer_id = drawer.id
    # An empty lot still makes the drawer un-deletable — `stock_lots.location_id`
    # is `RESTRICT` against a table nothing deletes from — so removal retires it
    # instead, which is the only state a restore can start from. Empty rather
    # than stocked because a container holding actual stock is refused outright.
    make_lot(db, make_part(db, name="Something"), drawer, qty_milli=0)
    db.commit()

    removed = client.delete(f"/api/locations/{drawer_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["retired_location_ids"] == [drawer_id], removed.text
    assert client.post("/api/system/maintenance").status_code == 200
    _assert_agree(client, cabinet_id, 0.25)

    restored = client.post(f"/api/locations/{drawer_id}/restore")
    assert restored.status_code == 200, restored.text

    assert client.post("/api/system/maintenance").status_code == 200
    _assert_agree(client, cabinet_id, 0.5)
