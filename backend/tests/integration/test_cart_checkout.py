"""The cart's three checkout doors (ADR 0007), over real migrations.

One rule is what these tests exist for, and it is the same rule in three places:
**a line whose stock has moved fails that line and not the batch.** A cart is
gathered at the shelf over minutes, so by the time it is checked out one of its
lines may name a lot somebody has emptied, moved or deleted. Rolling the batch
back would discard the user's other decisions; refusing it with a 4xx would leave
the client unable to see which row to fix.

Every assertion here therefore checks the *rows*, not only the status: an endpoint
that returned a tidy per-line report and wrote nothing — or wrote twice — would
pass a status-code test. `stock_ledger` is counted directly because it is the one
table that cannot be edited afterwards, so a double-applied movement is permanent.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.projects import StockAllocation
from app.models.stock import StockLedger, StockLot
from tests.factories import (
    make_bom_line,
    make_build,
    make_location,
    make_lot,
    make_part,
    make_project,
)


def _session() -> Session:
    """A session that has not seen the setup transaction, so every read below is
    of committed state rather than of a snapshot taken before the request."""
    return get_session_factory()()


def _key() -> str:
    return str(uuid.uuid4())


def _ledger_count() -> int:
    session = _session()
    try:
        return int(session.execute(select(func.count()).select_from(StockLedger)).scalar_one())
    finally:
        session.close()


def _balance(lot_id: int) -> int:
    session = _session()
    try:
        lot = session.get(StockLot, lot_id)
        assert lot is not None
        return lot.qty_milli_cached
    finally:
        session.close()


def _allocation_count() -> int:
    session = _session()
    try:
        return int(session.execute(select(func.count()).select_from(StockAllocation)).scalar_one())
    finally:
        session.close()


def _reserved(lot_id: int) -> int:
    session = _session()
    try:
        lot = session.get(StockLot, lot_id)
        assert lot is not None
        return lot.qty_reserved_milli_cached
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Plain stock: POST /api/stock/movements
# ---------------------------------------------------------------------------


def test_batch_movement_applies_every_line(client: TestClient, db: Session) -> None:
    bin_a = make_location(db, "Bin A")
    resistor = make_part(db, "10k")
    capacitor = make_part(db, "100n")
    lot_r = make_lot(db, resistor, bin_a, qty_milli=10_000)
    lot_c = make_lot(db, capacitor, bin_a, qty_milli=5_000)
    db.commit()

    response = client.post(
        "/api/stock/movements",
        json={
            "location_id": bin_a.id,
            "client_op_id": _key(),
            "lines": [
                {"lot_id": lot_r.id, "direction": "take", "qty_milli": 3_000},
                {"lot_id": lot_c.id, "direction": "return", "qty_milli": 1_000},
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["applied_count"], body["failed_count"]) == (2, 0)
    assert [result["applied"] for result in body["results"]] == [True, True]
    assert [result["reason"] for result in body["results"]] == [None, None]
    assert body["results"][0]["qty_milli_after"] == 7_000
    assert body["results"][1]["qty_milli_after"] == 6_000
    assert _balance(lot_r.id) == 7_000
    assert _balance(lot_c.id) == 6_000
    assert _ledger_count() == 2


def test_a_lot_that_has_moved_fails_its_line_and_the_rest_apply(
    client: TestClient, db: Session
) -> None:
    """The staleness the cart exists to survive: it captured this lot in the bin
    the user is holding, and somebody has since moved it elsewhere."""
    bin_a = make_location(db, "Bin A")
    bin_b = make_location(db, "Bin B")
    resistor = make_part(db, "10k")
    capacitor = make_part(db, "100n")
    stays = make_lot(db, resistor, bin_a, qty_milli=10_000)
    moved = make_lot(db, capacitor, bin_b, qty_milli=5_000)  # not in Bin A any more
    db.commit()

    response = client.post(
        "/api/stock/movements",
        json={
            "location_id": bin_a.id,
            "lines": [
                {
                    "client_line_id": "row-1",
                    "lot_id": stays.id,
                    "direction": "take",
                    "qty_milli": 3_000,
                },
                {
                    "client_line_id": "row-2",
                    "lot_id": moved.id,
                    "direction": "take",
                    "qty_milli": 1_000,
                },
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["applied_count"], body["failed_count"]) == (1, 1)
    good, bad = body["results"]
    assert (good["client_line_id"], good["applied"]) == ("row-1", True)
    assert (bad["client_line_id"], bad["applied"], bad["reason"]) == ("row-2", False, "lot_moved")
    assert bad["index"] == 1
    # The good line landed and the bad one wrote nothing.
    assert _balance(stays.id) == 7_000
    assert _balance(moved.id) == 5_000
    assert _ledger_count() == 1


def test_a_line_naming_stock_that_no_longer_exists_is_a_readable_failure(
    client: TestClient, db: Session
) -> None:
    """ADR 0007: a cart holding a part that has since been deleted degrades to a
    named, removable row — never a 500 and never a batch-wide error."""
    bin_a = make_location(db, "Bin A")
    resistor = make_part(db, "10k")
    lot = make_lot(db, resistor, bin_a, qty_milli=10_000)
    db.commit()

    response = client.post(
        "/api/stock/movements",
        json={
            "location_id": bin_a.id,
            "lines": [
                {"lot_id": 999_999, "direction": "take", "qty_milli": 1},
                {"part_id": 999_999, "direction": "take", "qty_milli": 1},
                {"lot_id": lot.id, "direction": "take", "qty_milli": 1_000},
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [result["reason"] for result in body["results"]] == [
        "unknown_lot",
        "unknown_part",
        None,
    ]
    assert all(result["message"] for result in body["results"][:2])
    assert body["applied_count"] == 1
    assert _ledger_count() == 1


def test_retrying_a_whole_batch_does_not_double_apply(client: TestClient, db: Session) -> None:
    bin_a = make_location(db, "Bin A")
    resistor = make_part(db, "10k")
    lot = make_lot(db, resistor, bin_a, qty_milli=10_000)
    db.commit()

    body = {
        "location_id": bin_a.id,
        "client_op_id": _key(),
        "lines": [{"lot_id": lot.id, "direction": "take", "qty_milli": 3_000}],
    }
    first = client.post("/api/stock/movements", json=body)
    second = client.post("/api/stock/movements", json=body)

    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.json()["results"][0]["seq"] == first.json()["results"][0]["seq"]
    assert _balance(lot.id) == 7_000
    assert _ledger_count() == 1


def test_resubmitting_a_cart_replays_only_the_lines_that_already_landed(
    client: TestClient, db: Session
) -> None:
    """The retry that a *partial* failure actually produces.

    The user fixes the one bad row and sends the cart again — with a fresh batch
    key, because the request body changed. Only the per-line keys can stop the
    nineteen good lines being applied a second time, which is the whole reason
    they exist.
    """
    bin_a = make_location(db, "Bin A")
    bin_b = make_location(db, "Bin B")
    resistor = make_part(db, "10k")
    capacitor = make_part(db, "100n")
    good = make_lot(db, resistor, bin_a, qty_milli=10_000)
    elsewhere = make_lot(db, capacitor, bin_b, qty_milli=5_000)
    db.commit()

    good_key, bad_key = _key(), _key()
    lines = [
        {"lot_id": good.id, "direction": "take", "qty_milli": 3_000, "client_op_id": good_key},
        {"lot_id": elsewhere.id, "direction": "take", "qty_milli": 1_000, "client_op_id": bad_key},
    ]
    first = client.post(
        "/api/stock/movements",
        json={"location_id": bin_a.id, "client_op_id": _key(), "lines": lines},
    ).json()
    assert (first["applied_count"], first["failed_count"]) == (1, 1)

    # Somebody puts the second lot back where the cart thought it was.
    client.post(
        f"/api/stock/lots/{elsewhere.id}/move", json={"to_location_id": bin_a.id}
    ).raise_for_status()

    second = client.post(
        "/api/stock/movements",
        json={"location_id": bin_a.id, "client_op_id": _key(), "lines": lines},
    ).json()

    assert second["applied_count"] == 2
    assert second["results"][0]["replayed"] is True
    assert second["results"][1]["replayed"] is False
    assert _balance(good.id) == 7_000  # not 4_000
    assert _balance(elsewhere.id) == 4_000
    # Two takes, plus the whole-lot move between them.
    assert _ledger_count() == 3


def test_two_lines_sharing_one_line_key_is_the_second_line_s_failure(
    client: TestClient, db: Session
) -> None:
    bin_a = make_location(db, "Bin A")
    resistor = make_part(db, "10k")
    lot = make_lot(db, resistor, bin_a, qty_milli=10_000)
    db.commit()

    shared = _key()
    response = client.post(
        "/api/stock/movements",
        json={
            "location_id": bin_a.id,
            "lines": [
                {"lot_id": lot.id, "direction": "take", "qty_milli": 1_000, "client_op_id": shared},
                {"lot_id": lot.id, "direction": "take", "qty_milli": 2_000, "client_op_id": shared},
            ],
        },
    )

    body = response.json()
    assert body["results"][0]["applied"] is True
    assert body["results"][1]["reason"] == "duplicate_client_op_id"
    assert _ledger_count() == 1


def test_a_cart_checkout_is_one_group_so_one_undo_reverses_it(
    client: TestClient, db: Session
) -> None:
    bin_a = make_location(db, "Bin A")
    resistor = make_part(db, "10k")
    capacitor = make_part(db, "100n")
    lot_r = make_lot(db, resistor, bin_a, qty_milli=10_000)
    lot_c = make_lot(db, capacitor, bin_a, qty_milli=5_000)
    db.commit()

    checkout = client.post(
        "/api/stock/movements",
        json={
            "location_id": bin_a.id,
            "lines": [
                {"lot_id": lot_r.id, "direction": "take", "qty_milli": 3_000},
                {"lot_id": lot_c.id, "direction": "take", "qty_milli": 1_000},
            ],
        },
    ).json()

    undone = client.post("/api/stock/undo", json={"group_uuid_to_undo": checkout["group_uuid"]})
    assert undone.status_code == 200, undone.text
    assert len(undone.json()["reversed_seqs"]) == 2
    assert _balance(lot_r.id) == 10_000
    assert _balance(lot_c.id) == 5_000
    # Compensating rows, never deletions.
    assert _ledger_count() == 4


def test_a_return_may_create_the_lot_but_a_take_may_not(client: TestClient, db: Session) -> None:
    bin_a = make_location(db, "Bin A")
    resistor = make_part(db, "10k")
    db.commit()

    response = client.post(
        "/api/stock/movements",
        json={
            "location_id": bin_a.id,
            "lines": [
                {"part_id": resistor.id, "direction": "take", "qty_milli": 500},
                {"part_id": resistor.id, "direction": "return", "qty_milli": 500},
            ],
        },
    )

    body = response.json()
    assert body["results"][0]["reason"] == "no_lot_for_part"
    assert body["results"][1]["applied"] is True
    created = body["results"][1]["lot_id"]
    assert created is not None
    assert _balance(created) == 500


def test_two_packages_of_one_part_in_a_bin_are_not_guessed_between(
    client: TestClient, db: Session
) -> None:
    bin_a = make_location(db, "Bin A")
    resistor = make_part(db, "10k")
    make_lot(db, resistor, bin_a, qty_milli=5_000)
    make_lot(db, resistor, bin_a, qty_milli=200)
    db.commit()

    response = client.post(
        "/api/stock/movements",
        json={
            "location_id": bin_a.id,
            "lines": [{"part_id": resistor.id, "direction": "take", "qty_milli": 100}],
        },
    )

    assert response.json()["results"][0]["reason"] == "ambiguous_lot"
    assert _ledger_count() == 0


def test_a_part_line_without_a_container_is_refused_per_line(
    client: TestClient, db: Session
) -> None:
    resistor = make_part(db, "10k")
    db.commit()

    response = client.post(
        "/api/stock/movements",
        json={"lines": [{"part_id": resistor.id, "direction": "take", "qty_milli": 1}]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["reason"] == "no_container"


def test_a_line_must_name_either_a_lot_or_a_part(client: TestClient) -> None:
    """A malformed line *is* a whole-request 422: nothing about it depends on
    state, so there is no partial success to preserve."""
    both = client.post(
        "/api/stock/movements",
        json={"lines": [{"lot_id": 1, "part_id": 1, "direction": "take", "qty_milli": 1}]},
    )
    neither = client.post(
        "/api/stock/movements",
        json={"lines": [{"direction": "take", "qty_milli": 1}]},
    )
    assert both.status_code == 422
    assert neither.status_code == 422


# ---------------------------------------------------------------------------
# A build: POST /api/builds/{id}/allocate-batch
# ---------------------------------------------------------------------------


def test_allocate_batch_places_every_hold(client: TestClient, db: Session) -> None:
    project = make_project(db)
    build = make_build(db, project)
    resistor = make_part(db, "10k")
    capacitor = make_part(db, "100n")
    bin_a = make_location(db, "Bin A")
    lot_r = make_lot(db, resistor, bin_a, qty_milli=10_000)
    lot_c = make_lot(db, capacitor, bin_a, qty_milli=5_000)
    line_r = make_bom_line(db, project, qty_per_assembly_milli=1_000, part_id=resistor.id)
    db.commit()

    response = client.post(
        f"/api/builds/{build.id}/allocate-batch",
        json={
            "client_op_id": _key(),
            "lines": [
                {"lot_id": lot_r.id, "qty_milli": 3_000, "bom_line_id": line_r.id},
                {"lot_id": lot_c.id, "qty_milli": 1_000},
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["applied_count"], body["failed_count"]) == (2, 0)
    assert body["results"][0]["allocation"]["bom_line_id"] == line_r.id
    assert body["results"][0]["available_milli"] == 7_000
    assert _allocation_count() == 2
    assert _reserved(lot_r.id) == 3_000


def test_one_refused_hold_does_not_lose_the_others(client: TestClient, db: Session) -> None:
    project = make_project(db)
    build = make_build(db, project)
    resistor = make_part(db, "10k")
    capacitor = make_part(db, "100n")
    bin_a = make_location(db, "Bin A")
    plenty = make_lot(db, resistor, bin_a, qty_milli=10_000)
    thin = make_lot(db, capacitor, bin_a, qty_milli=100)
    db.commit()

    body = client.post(
        f"/api/builds/{build.id}/allocate-batch",
        json={
            "lines": [
                {"client_line_id": "a", "lot_id": plenty.id, "qty_milli": 3_000},
                {"client_line_id": "b", "lot_id": thin.id, "qty_milli": 9_000},
                {"client_line_id": "c", "lot_id": 999_999, "qty_milli": 1},
            ]
        },
    ).json()

    assert (body["applied_count"], body["failed_count"]) == (1, 2)
    assert [result["reason"] for result in body["results"]] == [
        None,
        "insufficient_available",
        "unknown_lot",
    ]
    assert [result["client_line_id"] for result in body["results"]] == ["a", "b", "c"]
    assert _allocation_count() == 1
    assert _reserved(thin.id) == 0


def test_two_lines_of_one_cart_compete_for_the_same_lot_in_order(
    client: TestClient, db: Session
) -> None:
    """Applied in order, each in its own SAVEPOINT, so the second sees what the
    first left — exactly as two separate requests would."""
    project = make_project(db)
    build = make_build(db, project)
    resistor = make_part(db, "10k")
    bin_a = make_location(db, "Bin A")
    lot = make_lot(db, resistor, bin_a, qty_milli=1_000)
    db.commit()

    body = client.post(
        f"/api/builds/{build.id}/allocate-batch",
        json={
            "lines": [
                {"lot_id": lot.id, "qty_milli": 600},
                {"lot_id": lot.id, "qty_milli": 600},
            ]
        },
    ).json()

    assert body["results"][0]["applied"] is True
    assert body["results"][0]["available_milli"] == 400
    assert body["results"][1]["reason"] == "insufficient_available"
    assert _reserved(lot.id) == 600


def test_retrying_an_allocate_batch_does_not_double_reserve(
    client: TestClient, db: Session
) -> None:
    project = make_project(db)
    build = make_build(db, project)
    resistor = make_part(db, "10k")
    bin_a = make_location(db, "Bin A")
    lot = make_lot(db, resistor, bin_a, qty_milli=10_000)
    db.commit()

    body = {"client_op_id": _key(), "lines": [{"lot_id": lot.id, "qty_milli": 3_000}]}
    first = client.post(f"/api/builds/{build.id}/allocate-batch", json=body)
    second = client.post(f"/api/builds/{build.id}/allocate-batch", json=body)

    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert _allocation_count() == 1
    assert _reserved(lot.id) == 3_000


def test_resubmitting_a_cart_of_holds_replays_the_ones_already_placed(
    client: TestClient, db: Session
) -> None:
    """`stock_allocations` has no UNIQUE to fall back on, so without the per-line
    key this resubmission would double the hold that had already landed."""
    project = make_project(db)
    build = make_build(db, project)
    resistor = make_part(db, "10k")
    capacitor = make_part(db, "100n")
    bin_a = make_location(db, "Bin A")
    plenty = make_lot(db, resistor, bin_a, qty_milli=10_000)
    thin = make_lot(db, capacitor, bin_a, qty_milli=100)
    db.commit()

    lines = [
        {"lot_id": plenty.id, "qty_milli": 3_000, "client_op_id": _key()},
        {"lot_id": thin.id, "qty_milli": 9_000, "client_op_id": _key()},
    ]
    first = client.post(
        f"/api/builds/{build.id}/allocate-batch", json={"client_op_id": _key(), "lines": lines}
    ).json()
    assert (first["applied_count"], first["failed_count"]) == (1, 1)

    # More arrived for the thin lot; the user sends the same cart again.
    client.post(
        "/api/stock/receive",
        json={"part_id": capacitor.id, "location_id": bin_a.id, "qty_milli": 20_000},
    ).raise_for_status()

    second = client.post(
        f"/api/builds/{build.id}/allocate-batch", json={"client_op_id": _key(), "lines": lines}
    ).json()

    assert second["applied_count"] == 2
    assert second["results"][0]["replayed"] is True
    assert second["results"][1]["replayed"] is False
    assert _allocation_count() == 2
    assert _reserved(plenty.id) == 3_000  # not 6_000


# ---------------------------------------------------------------------------
# A project BOM: PUT /api/projects/{id}/bom, the door that already existed
# ---------------------------------------------------------------------------


def test_bom_partial_applies_the_good_edits_and_reports_the_bad(
    client: TestClient, db: Session
) -> None:
    project = make_project(db)
    existing = make_bom_line(db, project, qty_per_assembly_milli=1_000)
    db.commit()

    response = client.put(
        f"/api/projects/{project.id}/bom",
        json={
            "partial": True,
            "edits": [
                {"client_line_id": "a", "qty_per_assembly_milli": 2_000, "mpn_raw": "RC0402-10K"},
                {"client_line_id": "b", "qty_per_assembly_milli": 5_000, "part_id": 999_999},
                {"client_line_id": "c", "id": existing.id, "qty_per_assembly_milli": 4_000},
                {"client_line_id": "d", "id": 999_999, "is_dnp": True},
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [result["applied"] for result in body["results"]] == [True, False, True, False]
    assert [result["reason"] for result in body["results"]] == [
        None,
        "unknown_part",
        None,
        "unknown_bom_line",
    ]
    listing = client.get(f"/api/projects/{project.id}/bom").json()
    assert listing["total"] == 2  # the original plus one added, not two added
    quantities = {line["qty_per_assembly_milli"] for line in listing["lines"]}
    assert quantities == {2_000, 4_000}


def test_bom_without_partial_still_refuses_the_whole_batch(client: TestClient, db: Session) -> None:
    """The default is unchanged on purpose: a curation pass is one considered set
    of changes, and half of it landing is worse than being told it was wrong."""
    project = make_project(db)
    db.commit()

    response = client.put(
        f"/api/projects/{project.id}/bom",
        json={
            "edits": [
                {"qty_per_assembly_milli": 2_000},
                {"qty_per_assembly_milli": 5_000, "part_id": 999_999},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_part"
    assert client.get(f"/api/projects/{project.id}/bom").json()["total"] == 0


def test_resubmitting_a_bom_cart_does_not_add_the_same_line_twice(
    client: TestClient, db: Session
) -> None:
    project = make_project(db)
    resistor = make_part(db, "10k")
    db.commit()

    edits = [
        {"qty_per_assembly_milli": 2_000, "designators": "R1", "client_op_id": _key()},
        {"qty_per_assembly_milli": 5_000, "part_id": 999_999, "client_op_id": _key()},
    ]
    first = client.put(
        f"/api/projects/{project.id}/bom",
        json={"partial": True, "client_op_id": _key(), "edits": edits},
    ).json()
    assert [result["applied"] for result in first["results"]] == [True, False]

    fixed = [edits[0], {**edits[1], "part_id": resistor.id}]
    second = client.put(
        f"/api/projects/{project.id}/bom",
        json={"partial": True, "client_op_id": _key(), "edits": fixed},
    ).json()

    assert [result["applied"] for result in second["results"]] == [True, True]
    assert second["results"][0]["replayed"] is True
    assert second["results"][0]["bom_line_id"] == first["results"][0]["bom_line_id"]
    assert client.get(f"/api/projects/{project.id}/bom").json()["total"] == 2


def test_a_bom_line_of_another_project_fails_only_its_own_line(
    client: TestClient, db: Session
) -> None:
    mine = make_project(db, "Mine")
    theirs = make_project(db, "Theirs")
    other_line = make_bom_line(db, theirs, qty_per_assembly_milli=1_000)
    db.commit()

    body = client.put(
        f"/api/projects/{mine.id}/bom",
        json={
            "partial": True,
            "edits": [
                {"id": other_line.id, "is_dnp": True},
                {"qty_per_assembly_milli": 7_000},
            ],
        },
    ).json()

    assert body["results"][0]["reason"] == "line_not_in_project"
    assert body["results"][1]["applied"] is True
    assert client.get(f"/api/projects/{mine.id}/bom").json()["total"] == 1


# ---------------------------------------------------------------------------
# The numbers ADR 0007 asks the UI to show
# ---------------------------------------------------------------------------


def test_the_roster_carries_accounted_and_needed(client: TestClient, db: Session) -> None:
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    resistor = make_part(db, "10k")
    bin_a = make_location(db, "Bin A")
    lot = make_lot(db, resistor, bin_a, qty_milli=10_000)
    line = make_bom_line(db, project, qty_per_assembly_milli=3_000, part_id=resistor.id)
    db.commit()

    client.post(
        f"/api/builds/{build.id}/allocate",
        json={"lot_id": lot.id, "qty_milli": 3_000, "bom_line_id": line.id},
    ).raise_for_status()

    roster = client.get(f"/api/builds/{build.id}/roster").json()
    row = next(entry for entry in roster["lines"] if entry["bom_line_id"] == line.id)
    assert (row["required_milli"], row["accounted_milli"], row["needed_milli"]) == (
        3_000,
        3_000,
        0,
    )

    # "Request parts for three more boards": one column, nothing backfilled.
    client.patch(f"/api/builds/{build.id}", json={"assembly_count": 3}).raise_for_status()

    after = client.get(f"/api/builds/{build.id}/roster").json()
    row = next(entry for entry in after["lines"] if entry["bom_line_id"] == line.id)
    assert (row["required_milli"], row["accounted_milli"], row["needed_milli"]) == (
        9_000,
        3_000,
        6_000,
    )
    assert _allocation_count() == 1

    shortages = client.get(f"/api/builds/{build.id}/shortages").json()
    short_line = next(entry for entry in shortages["lines"] if entry["bom_line_id"] == line.id)
    assert short_line["needed_milli"] == row["needed_milli"]
    assert short_line["allocated_milli"] == row["accounted_milli"]
