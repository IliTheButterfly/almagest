"""The stock write routes.

These matter more than the count suggests. Every rule asserted here is one that,
if broken, corrupts data *silently* — a ledger row without its balance movement,
a retry that posts twice, an undo that deletes. None of those raise; they just
leave the numbers wrong, so a test is the only thing standing between the
invariant and a slow-motion failure discovered months later.

The suite deliberately drives everything through HTTP rather than the service
layer. `tests/integration/test_ledger_invariants.py` already covers the service;
what is unproven until here is that the *routes* preserve the same guarantees,
including the transaction boundary they do not own.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.stock import StockLedger, StockLot


def _session() -> Session:
    return get_session_factory()()


def _make_part_and_locations(names: tuple[str, ...] = ("Bin A", "Bin B")) -> tuple[int, list[int]]:
    """A component part plus some empty locations, committed."""
    from app.models.catalog import Part, PartKind
    from app.models.storage import Location
    from app.services.tree import location_tree

    session = _session()
    try:
        kind = session.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()
        part = Part(name="Test resistor", part_kind_id=kind.id, mpn="TEST-RES-1")
        session.add(part)
        locations = [Location(name=name) for name in names]
        session.add_all(locations)
        session.flush()
        location_tree(session).rebuild_paths()
        ids = [location.id for location in locations]
        part_id = part.id
        session.commit()
        return part_id, ids
    finally:
        session.close()


def _key() -> str:
    return str(uuid.uuid4())


def _receive(client: TestClient, part_id: int, location_id: int, qty: int, **extra: object) -> dict:
    body = {"part_id": part_id, "location_id": location_id, "qty_milli": qty, **extra}
    response = client.post("/api/stock/receive", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _ledger_sum(lot_id: int) -> int:
    session = _session()
    try:
        return int(
            session.execute(
                select(func.coalesce(func.sum(StockLedger.delta_milli), 0)).where(
                    StockLedger.lot_id == lot_id
                )
            ).scalar_one()
        )
    finally:
        session.close()


def _cached(lot_id: int) -> int:
    session = _session()
    try:
        lot = session.get(StockLot, lot_id)
        assert lot is not None
        return lot.qty_milli_cached
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The core invariant: the row and the cache move together, over HTTP
# ---------------------------------------------------------------------------


def test_receive_creates_a_lot_and_moves_the_balance(client: TestClient) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    body = _receive(client, part_id, bin_a, 5000)

    assert body["lot"]["qty_milli"] == 5000
    assert body["lot"]["location_id"] == bin_a
    assert len(body["seqs"]) == 1
    assert _cached(body["lot"]["id"]) == _ledger_sum(body["lot"]["id"]) == 5000


def test_the_cache_matches_the_ledger_after_a_randomised_route_sequence(
    client: TestClient,
) -> None:
    """The invariant that makes it safe to never sum the ledger in an API path —
    asserted through the ROUTES, not the service helper, because the routes are
    where the transaction boundary lives."""
    import random

    rng = random.Random(20260728)
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_a = _receive(client, part_id, bin_a, 100_000)["lot"]["id"]
    lot_b = _receive(client, part_id, bin_b, 100_000)["lot"]["id"]

    for _ in range(60):
        lot = rng.choice([lot_a, lot_b])
        pick = rng.random()
        if pick < 0.4:
            client.post(f"/api/stock/lots/{lot}/consume", json={"qty_milli": rng.randint(1, 900)})
        elif pick < 0.6:
            client.post(f"/api/stock/lots/{lot}/return", json={"qty_milli": rng.randint(1, 900)})
        elif pick < 0.8:
            client.post(f"/api/stock/lots/{lot}/adjust", json={"delta_milli": rng.randint(1, 500)})
        else:
            client.post(
                f"/api/stock/lots/{lot}/recount", json={"counted_qty_milli": rng.randint(0, 5000)}
            )

    for lot in (lot_a, lot_b):
        assert _cached(lot) == _ledger_sum(lot), f"lot {lot} drifted"


def test_qty_after_milli_tracks_the_balance_on_every_row(client: TestClient) -> None:
    """Redundant with the running sum on purpose: it makes drift traceable to the
    row that broke it rather than only visible once totals disagree."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 250})
    client.post(f"/api/stock/lots/{lot_id}/return", json={"qty_milli": 100})

    session = _session()
    try:
        rows = list(
            session.execute(
                select(StockLedger).where(StockLedger.lot_id == lot_id).order_by(StockLedger.seq)
            ).scalars()
        )
        running = 0
        for row in rows:
            running += row.delta_milli
            assert row.qty_after_milli == running
    finally:
        session.close()


def test_a_take_may_drive_the_balance_negative(client: TestClient) -> None:
    """Refusing here would block the record of what physically happened in order
    to protect a number that is meant to raise an alarm."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 100)["lot"]["id"]

    response = client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 500})
    assert response.status_code == 200
    assert response.json()["lot"]["qty_milli"] == -400


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_a_replayed_key_posts_exactly_one_row(client: TestClient) -> None:
    """A phone on flaky wifi retries a request whose response was lost. On an
    append-only ledger a second movement can only be corrected by a third."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]

    key = _key()
    body = {"qty_milli": 300, "client_op_id": key}
    first = client.post(f"/api/stock/lots/{lot_id}/consume", json=body)
    second = client.post(f"/api/stock/lots/{lot_id}/consume", json=body)

    assert first.status_code == second.status_code == 200
    assert first.json()["seqs"] == second.json()["seqs"]
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert _cached(lot_id) == 700


def test_replay_is_observable_so_the_undo_window_is_not_misreported(
    client: TestClient,
) -> None:
    """'Recorded just now' and 'recorded a minute ago' are otherwise identical
    responses, and a UI that cannot tell them apart offers an undo for a
    movement whose window closed."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    body = {"qty_milli": 10, "client_op_id": _key()}

    client.post(f"/api/stock/lots/{lot_id}/consume", json=body)
    assert client.post(f"/api/stock/lots/{lot_id}/consume", json=body).json()["replayed"] is True


def test_the_same_key_with_a_different_body_is_refused(client: TestClient) -> None:
    """Conflating this with a genuine retry would silently apply one of two
    different writes, and neither choice is defensible."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    key = _key()

    client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100, "client_op_id": key})
    clash = client.post(
        f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 999, "client_op_id": key}
    )

    assert clash.status_code == 409
    assert clash.json()["detail"]["reason"] == "request_mismatch"
    assert _cached(lot_id) == 900  # the second write did not happen


def test_the_same_key_on_a_different_endpoint_is_refused(client: TestClient) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    key = _key()

    client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100, "client_op_id": key})
    clash = client.post(
        f"/api/stock/lots/{lot_id}/return", json={"qty_milli": 100, "client_op_id": key}
    )
    assert clash.status_code == 409


def test_without_a_key_each_request_is_a_separate_movement(client: TestClient) -> None:
    """At-least-once is the honest reading of a request carrying no way to
    recognise its own retry."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]

    client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100})
    client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100})
    assert _cached(lot_id) == 800


def test_a_failed_operation_leaves_its_key_reusable(client: TestClient) -> None:
    """A refusal must not burn the key — the client would have no way to retry
    the corrected request."""
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    key = _key()

    same = client.post(
        f"/api/stock/lots/{lot_id}/move", json={"to_location_id": bin_a, "client_op_id": key}
    )
    assert same.status_code == 409

    good = client.post(
        f"/api/stock/lots/{lot_id}/move", json={"to_location_id": bin_b, "client_op_id": key}
    )
    assert good.status_code == 200, good.text


# ---------------------------------------------------------------------------
# Move semantics
# ---------------------------------------------------------------------------


def test_a_whole_lot_move_is_one_row_with_zero_delta(client: TestClient) -> None:
    """Minting a new lot per shelf change would destroy lot identity and per-lot
    cost continuity."""
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 5000, unit_cost_micro=42_000)["lot"]["id"]

    body = client.post(f"/api/stock/lots/{lot_id}/move", json={"to_location_id": bin_b}).json()

    assert len(body["seqs"]) == 1
    assert body["lot"]["id"] == lot_id  # same lot, not a new one
    assert body["lot"]["location_id"] == bin_b
    assert body["lot"]["qty_milli"] == 5000
    assert body["lot"]["unit_cost_micro"] == 42_000

    session = _session()
    try:
        row = session.get(StockLedger, body["seqs"][0])
        assert row is not None
        assert row.delta_milli == 0
        assert row.from_location_id == bin_a
        assert row.to_location_id == bin_b
    finally:
        session.close()


def test_a_partial_move_is_two_rows_that_sum_to_zero(client: TestClient) -> None:
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 5000)["lot"]["id"]

    body = client.post(
        f"/api/stock/lots/{lot_id}/move", json={"to_location_id": bin_b, "qty_milli": 1200}
    ).json()

    assert len(body["seqs"]) == 2
    assert body["group_uuid"]
    assert body["lot"]["qty_milli"] == 3800
    assert body["counterpart_lot"]["qty_milli"] == 1200
    assert body["counterpart_lot"]["location_id"] == bin_b

    session = _session()
    try:
        total = session.execute(
            select(func.sum(StockLedger.delta_milli)).where(
                StockLedger.group_uuid == body["group_uuid"]
            )
        ).scalar_one()
        assert total == 0  # conservative: moves stock without creating any
    finally:
        session.close()


def test_moving_to_the_same_location_is_refused(client: TestClient) -> None:
    """Almost always a double scan of one label, and a no-op row would put a
    movement in the history that never happened."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]

    response = client.post(f"/api/stock/lots/{lot_id}/move", json={"to_location_id": bin_a})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "same_location"


def test_a_move_to_an_unknown_location_is_404(client: TestClient) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    assert (
        client.post(f"/api/stock/lots/{lot_id}/move", json={"to_location_id": 999_999}).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Bulk empty — workflow 4
# ---------------------------------------------------------------------------


def test_emptying_a_bin_moves_every_lot(client: TestClient) -> None:
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    for batch in ("L1", "L2", "L3"):
        _receive(client, part_id, bin_a, 1000, batch_code=batch)

    body = client.post(f"/api/stock/locations/{bin_a}/empty", json={"to_location_id": bin_b}).json()

    assert len(body["moved_lot_ids"]) == 3
    assert body["failures"] == []

    session = _session()
    try:
        left = session.execute(
            select(func.count()).select_from(StockLot).where(StockLot.location_id == bin_a)
        ).scalar_one()
        assert left == 0
    finally:
        session.close()


def test_one_bad_lot_commits_the_rest_and_reports_just_that_failure(
    client: TestClient,
) -> None:
    """The user has already tipped the other bags into the new bin. Refusing the
    whole batch would leave the database describing a world that no longer
    exists.

    A quarantined lot is the failure case: quarantine exists to stop a lot being
    relocated without someone deciding about it, and a bulk operation is the
    easiest way for that decision to get skipped by accident.
    """
    from app.models.enums import LotStatus

    part_id, (bin_a, bin_b) = _make_part_and_locations()
    _receive(client, part_id, bin_a, 1000, batch_code="good-1")
    _receive(client, part_id, bin_a, 1000, batch_code="good-2")
    held = _receive(client, part_id, bin_a, 1000, batch_code="held")["lot"]["id"]

    session = _session()
    try:
        lot = session.get(StockLot, held)
        assert lot is not None
        lot.status = LotStatus.QUARANTINED
        session.commit()
    finally:
        session.close()

    body = client.post(f"/api/stock/locations/{bin_a}/empty", json={"to_location_id": bin_b}).json()

    assert len(body["moved_lot_ids"]) == 2
    assert held not in body["moved_lot_ids"]
    assert [failure["lot_id"] for failure in body["failures"]] == [held]
    assert body["failures"][0]["reason"] == "quarantined"

    # The two good moves committed; the held lot stayed put.
    session = _session()
    try:
        stayed = session.get(StockLot, held)
        assert stayed is not None and stayed.location_id == bin_a
        remaining = session.execute(
            select(func.count()).select_from(StockLot).where(StockLot.location_id == bin_a)
        ).scalar_one()
        assert remaining == 1
    finally:
        session.close()


def test_emptying_into_the_same_bin_is_refused(client: TestClient) -> None:
    _, (bin_a, _) = _make_part_and_locations()
    response = client.post(f"/api/stock/locations/{bin_a}/empty", json={"to_location_id": bin_a})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "same_location"


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def test_undo_is_a_compensating_row_not_a_deletion(client: TestClient) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    key = _key()
    take = client.post(
        f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 250, "client_op_id": key}
    ).json()

    undo = client.post("/api/stock/undo", json={"client_op_id_to_undo": key})
    assert undo.status_code == 200, undo.text
    body = undo.json()

    assert body["reversed_seqs"] == take["seqs"]
    assert _cached(lot_id) == 1000

    session = _session()
    try:
        # Both rows survive: "this happened, then it was undone" is not the same
        # statement as "this never happened".
        count = session.execute(
            select(func.count()).select_from(StockLedger).where(StockLedger.lot_id == lot_id)
        ).scalar_one()
        assert count == 3  # receive, consume, compensation
        compensation = session.get(StockLedger, body["seqs"][0])
        assert compensation is not None
        assert compensation.reversal_of_seq == take["seqs"][0]
        assert compensation.delta_milli == 250
    finally:
        session.close()


def test_undoing_a_partial_move_reverses_both_halves(client: TestClient) -> None:
    """One tap must undo both rows, or stock ends up duplicated across two bins."""
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 5000)["lot"]["id"]
    move = client.post(
        f"/api/stock/lots/{lot_id}/move",
        json={"to_location_id": bin_b, "qty_milli": 1200, "client_op_id": _key()},
    ).json()

    undo = client.post("/api/stock/undo", json={"group_uuid_to_undo": move["group_uuid"]})
    assert undo.status_code == 200, undo.text
    assert len(undo.json()["seqs"]) == 2
    assert _cached(lot_id) == 5000
    assert _cached(move["counterpart_lot"]["id"]) == 0


def test_undoing_a_partial_move_by_its_key_still_reverses_both_halves(
    client: TestClient,
) -> None:
    """The atomic half of `group_kind`, and the regression this must never cause.

    Narrowing `client_op_id_to_undo` so one line of a committed work-panel tab
    reverses alone must not narrow it here: a partial move's `split_out -N` and
    `split_in +N` are one statement, and reversing the key-carrying half alone
    would leave 1200 milli existing in both bins at once. The move's group is
    minted without a kind, which reads as atomic, so the expansion still happens.
    """
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 5000)["lot"]["id"]
    key = _key()
    move = client.post(
        f"/api/stock/lots/{lot_id}/move",
        json={"to_location_id": bin_b, "qty_milli": 1200, "client_op_id": key},
    ).json()

    undo = client.post("/api/stock/undo", json={"client_op_id_to_undo": key})
    assert undo.status_code == 200, undo.text

    assert len(undo.json()["reversed_seqs"]) == 2
    assert _cached(lot_id) == 5000
    assert _cached(move["counterpart_lot"]["id"]) == 0


def test_undoing_a_whole_lot_move_sends_it_back(client: TestClient) -> None:
    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    move = client.post(f"/api/stock/lots/{lot_id}/move", json={"to_location_id": bin_b}).json()

    client.post("/api/stock/undo", json={"seq": move["seqs"][0]})

    session = _session()
    try:
        lot = session.get(StockLot, lot_id)
        assert lot is not None
        assert lot.location_id == bin_a
    finally:
        session.close()


def test_a_row_cannot_be_undone_twice(client: TestClient) -> None:
    """Or a double-tapped undo button doubles the correction."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    take = client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100}).json()

    assert client.post("/api/stock/undo", json={"seq": take["seqs"][0]}).status_code == 200
    second = client.post("/api/stock/undo", json={"seq": take["seqs"][0]})
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "already_reversed"
    assert _cached(lot_id) == 1000


def test_a_compensating_row_cannot_itself_be_undone(client: TestClient) -> None:
    """A redo is a fresh movement with its own reason, not the negation of a
    negation."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    take = client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100}).json()
    undo = client.post("/api/stock/undo", json={"seq": take["seqs"][0]}).json()

    again = client.post("/api/stock/undo", json={"seq": undo["seqs"][0]})
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "is_a_reversal"


def test_a_compensation_keeps_the_original_kind(client: TestClient) -> None:
    """Per-kind aggregation is the whole reason `kind` exists. An `adjust`
    compensation would leave "how much did I consume this month" permanently
    overstated by the takes that were undone seconds later."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    take = client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100}).json()
    undo = client.post("/api/stock/undo", json={"seq": take["seqs"][0]}).json()

    session = _session()
    try:
        row = session.get(StockLedger, undo["seqs"][0])
        assert row is not None
        assert row.kind == "consume"
        assert row.delta_milli == 100
    finally:
        session.close()


def test_undo_needs_exactly_one_handle(client: TestClient) -> None:
    assert client.post("/api/stock/undo", json={}).status_code == 422
    assert (
        client.post("/api/stock/undo", json={"seq": 1, "group_uuid_to_undo": "x"}).status_code
        == 422
    )


def test_undoing_an_unknown_handle_is_404(client: TestClient) -> None:
    assert client.post("/api/stock/undo", json={"seq": 999_999}).status_code == 404


# ---------------------------------------------------------------------------
# Lots and packaging
# ---------------------------------------------------------------------------


def test_a_second_receipt_of_the_same_package_lands_on_the_same_lot(
    client: TestClient,
) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    first = _receive(client, part_id, bin_a, 1000, batch_code="B1")
    second = _receive(client, part_id, bin_a, 500, batch_code="B1")

    assert first["lot"]["id"] == second["lot"]["id"]
    assert second["lot"]["qty_milli"] == 1500


def test_different_batches_are_different_lots(client: TestClient) -> None:
    """A 5000-piece reel and a cut-tape strip of the same MPN in the same bin are
    two lots, independently costed."""
    part_id, (bin_a, _) = _make_part_and_locations()
    first = _receive(client, part_id, bin_a, 1000, batch_code="B1")
    second = _receive(client, part_id, bin_a, 1000, batch_code="B2")
    assert first["lot"]["id"] != second["lot"]["id"]


def test_force_new_lot_splits_an_otherwise_identical_package(client: TestClient) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    first = _receive(client, part_id, bin_a, 1000)
    second = _receive(client, part_id, bin_a, 1000, force_new_lot=True)
    assert first["lot"]["id"] != second["lot"]["id"]


def test_a_recount_records_what_was_counted_alongside_the_delta(client: TestClient) -> None:
    """ "The ledger said 500, I counted 480" is a different fact from "someone
    adjusted by -20", and only the first can be argued with later."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 500)["lot"]["id"]

    body = client.post(f"/api/stock/lots/{lot_id}/recount", json={"counted_qty_milli": 480}).json()

    session = _session()
    try:
        row = session.get(StockLedger, body["seqs"][0])
        assert row is not None
        assert row.counted_qty_milli == 480
        assert row.delta_milli == -20
    finally:
        session.close()


def test_a_confirming_recount_still_writes_a_row(client: TestClient) -> None:
    """Otherwise a verified bin is indistinguishable from one nobody has opened."""
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 500)["lot"]["id"]

    body = client.post(f"/api/stock/lots/{lot_id}/recount", json={"counted_qty_milli": 500}).json()
    assert len(body["seqs"]) == 1

    session = _session()
    try:
        row = session.get(StockLedger, body["seqs"][0])
        assert row is not None and row.delta_milli == 0
    finally:
        session.close()


def test_a_zero_adjustment_is_refused(client: TestClient) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 500)["lot"]["id"]
    response = client.post(f"/api/stock/lots/{lot_id}/adjust", json={"delta_milli": 0})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "zero_delta"


@pytest.mark.parametrize("qty", [0, -5])
def test_a_non_positive_quantity_is_rejected_by_validation(client: TestClient, qty: int) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 500)["lot"]["id"]
    assert (
        client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": qty}).status_code == 422
    )


def test_history_reads_newest_first(client: TestClient) -> None:
    part_id, (bin_a, _) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 1000)["lot"]["id"]
    client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 100})

    history = client.get(f"/api/stock/lots/{lot_id}/history").json()
    assert [entry["kind"] for entry in history] == ["consume", "receive"]
    assert history[0]["seq"] > history[1]["seq"]


# ---------------------------------------------------------------------------
# The triggers still hold with all these paths in place
# ---------------------------------------------------------------------------


def test_the_append_only_triggers_still_reject_direct_mutation(client: TestClient) -> None:
    """None of the new code may have introduced a path that edits history."""
    part_id, (bin_a, _) = _make_part_and_locations()
    seq = _receive(client, part_id, bin_a, 1000)["seqs"][0]

    session = _session()
    try:
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("UPDATE stock_ledger SET delta_milli = 1 WHERE seq = :s"), {"s": seq}
            )
        session.rollback()
        with pytest.raises(Exception, match="append-only"):
            session.execute(text("DELETE FROM stock_ledger WHERE seq = :s"), {"s": seq})
        session.rollback()
    finally:
        session.close()


def test_no_drift_after_exercising_every_route(client: TestClient) -> None:
    """The nightly drift check must be clean after a realistic day's traffic."""
    from app.db.maintenance import check_lot_balance_drift

    part_id, (bin_a, bin_b) = _make_part_and_locations()
    lot_id = _receive(client, part_id, bin_a, 10_000)["lot"]["id"]
    client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 500})
    client.post(f"/api/stock/lots/{lot_id}/return", json={"qty_milli": 200})
    client.post(f"/api/stock/lots/{lot_id}/adjust", json={"delta_milli": -50})
    client.post(f"/api/stock/lots/{lot_id}/recount", json={"counted_qty_milli": 9000})
    client.post(f"/api/stock/lots/{lot_id}/move", json={"to_location_id": bin_b, "qty_milli": 1000})
    take = client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": 25}).json()
    client.post("/api/stock/undo", json={"seq": take["seqs"][0]})

    session = _session()
    try:
        assert check_lot_balance_drift(session).is_clean
    finally:
        session.close()
