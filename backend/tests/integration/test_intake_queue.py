"""The server-side intake queue.

The queue's whole purpose is *deferring* work, so the properties worth testing are
the ones that make deferral safe rather than the ones that make it work:

- re-posting is a no-op, because a phone at a shelf with bad wifi retries;
- the worklist order is scan order, and a wrong device clock cannot scramble it;
- resolved and dismissed entries are kept, and stay distinguishable;
- nothing here touches the ledger.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.scanning import PendingIntake
from app.models.stock import StockLedger
from tests.factories import make_part

#: A real ECIA payload keeps its separators — the bytes are the asset.
ECIA = "[)>\x1e06\x1d1PLM358N\x1dQ25\x1d1T2412A\x1e\x04"


def _park(client: TestClient, **overrides: object) -> dict:
    body: dict[str, object] = {
        "client_op_id": "0189d1c0-0000-4000-8000-000000000001",
        "raw_payload": ECIA,
        "symbology": "DataMatrix",
        "decoded_kind": "ecia",
        "mpn": "LM358N",
        "quantity_milli": 25_000,
        "lot_code": "2412A",
        **overrides,
    }
    response = client.post("/api/intake/pending", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _list(client: TestClient, **params: object) -> dict:
    response = client.get("/api/intake/pending", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Parking
# ---------------------------------------------------------------------------


def test_a_scan_can_be_parked_with_no_further_screens(client: TestClient) -> None:
    """One request, and the fields the label gave up come back as stored."""
    body = _park(client)

    assert body["already_queued"] is False
    entry = body["entry"]
    assert entry["mpn"] == "LM358N"
    assert entry["quantity_milli"] == 25_000
    assert entry["status"] == "pending"


def test_the_raw_payload_is_stored_verbatim(client: TestClient) -> None:
    """Separators and all. Stripping them is the lossy step that would make a
    vendor format unmineable later, which is the one thing this table is for."""
    entry = _park(client)["entry"]
    assert entry["raw_payload"] == ECIA
    assert "\x1d" in entry["raw_payload"]


def test_an_unrecognised_label_is_still_a_legal_entry(client: TestClient) -> None:
    """Every derived column is nullable on purpose — the entry nothing could be
    made of is exactly the one worth keeping."""
    entry = _park(
        client,
        decoded_kind="unknown",
        mpn=None,
        quantity_milli=None,
        lot_code=None,
        raw_payload="???UNPARSEABLE???",
    )["entry"]
    assert entry["mpn"] is None
    assert entry["decoded_kind"] == "unknown"


def test_reposting_the_same_scan_is_a_no_op(client: TestClient, db: Session) -> None:
    """**The load-bearing property.** A phone that posted and lost the response
    retries; that is the normal case at a shelf, not an edge case. Re-posting a
    whole synced queue must not duplicate it."""
    first = _park(client)
    second = _park(client)

    assert second["already_queued"] is True
    assert second["entry"]["id"] == first["entry"]["id"]
    assert db.execute(select(func.count()).select_from(PendingIntake)).scalar_one() == 1


def test_already_queued_is_observable(client: TestClient) -> None:
    """A sync that cannot tell "parked" from "already parked" either reports a
    false success or double-counts what it uploaded."""
    assert _park(client)["already_queued"] is False
    assert _park(client)["already_queued"] is True


def test_a_second_distinct_scan_is_a_second_entry(client: TestClient) -> None:
    _park(client)
    _park(client, client_op_id="0189d1c0-0000-4000-8000-000000000002")
    assert _list(client)["total"] == 2


def test_an_unknown_part_hint_is_422_not_a_dangling_reference(client: TestClient) -> None:
    response = client.post(
        "/api/intake/pending",
        json={"client_op_id": "x", "raw_payload": "LM358N", "part_id": 999_999},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_part"


def test_an_unknown_scan_event_is_422(client: TestClient) -> None:
    response = client.post(
        "/api/intake/pending",
        json={"client_op_id": "x", "raw_payload": "LM358N", "scan_event_id": 999_999},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_scan_event"


def test_an_oversized_payload_is_refused_rather_than_stored(client: TestClient) -> None:
    """The column is written straight from a request body, so an unbounded Text
    field would be a free upload endpoint."""
    response = client.post(
        "/api/intake/pending",
        json={"client_op_id": "x", "raw_payload": "A" * 5000},
    )
    assert response.status_code == 422


def test_parking_writes_nothing_to_the_ledger(client: TestClient, db: Session) -> None:
    """Parking records an intention, not stock. The desk pass commits, through
    the ordinary movement routes — one code path writes the ledger, not two."""
    _park(client)
    assert db.execute(select(func.count()).select_from(StockLedger)).scalar_one() == 0


# ---------------------------------------------------------------------------
# The worklist
# ---------------------------------------------------------------------------


def test_the_queue_is_oldest_first(client: TestClient) -> None:
    """A box of reels is walked in the order it was scanned."""
    for index in range(3):
        _park(client, client_op_id=f"op-{index}", mpn=f"PART-{index}")

    entries = _list(client)["entries"]
    assert [entry["mpn"] for entry in entries] == ["PART-0", "PART-1", "PART-2"]


def test_a_wrong_device_clock_cannot_scramble_the_worklist(client: TestClient) -> None:
    """Ordered by server-assigned `id`, not by the client's `queued_at`.

    A phone whose clock is a year out would otherwise pin its entries to one end
    of every queue forever — and a worklist a bad clock can reorder is a worklist
    nobody trusts. `queued_at` is still stored, because it is what the user
    experienced.
    """
    _park(client, client_op_id="op-a", mpn="FIRST", queued_at="2030-01-01T00:00:00Z")
    _park(client, client_op_id="op-b", mpn="SECOND", queued_at="2020-01-01T00:00:00Z")

    entries = _list(client)["entries"]
    assert [entry["mpn"] for entry in entries] == ["FIRST", "SECOND"]
    # Stored, not discarded, and not silently corrected either.
    assert entries[0]["queued_at"].startswith("2030")


def test_the_queue_can_be_filtered_to_one_device(client: TestClient) -> None:
    """A shared install: "the ones I just scanned" is a real question."""
    _park(client, client_op_id="op-a", device_id="phone")
    _park(client, client_op_id="op-b", device_id="bench")

    assert _list(client, device_id="phone")["total"] == 1
    assert _list(client)["total"] == 2


def test_the_pending_count_comes_back_from_any_listing(client: TestClient) -> None:
    """So a badge needs no second request."""
    _park(client, client_op_id="op-a")
    _park(client, client_op_id="op-b")
    client.post("/api/intake/pending/1/dismiss", json={})

    listing = _list(client, status="dismissed")
    assert listing["total"] == 1  # matching the filter
    assert listing["pending_total"] == 1  # still to do, regardless of it


def test_pagination_does_not_repeat_or_drop_entries(client: TestClient) -> None:
    """Ordered by a unique column, so pages cannot overlap — the same reason
    search carries a total tie-break."""
    for index in range(5):
        _park(client, client_op_id=f"op-{index}", mpn=f"PART-{index}")

    first = _list(client, limit=2, offset=0)["entries"]
    second = _list(client, limit=2, offset=2)["entries"]
    third = _list(client, limit=2, offset=4)["entries"]

    seen = [entry["mpn"] for entry in first + second + third]
    assert seen == [f"PART-{index}" for index in range(5)]
    assert len(set(seen)) == 5


def test_an_empty_queue_lists_cleanly(client: TestClient) -> None:
    listing = _list(client)
    assert (listing["total"], listing["pending_total"], listing["entries"]) == (0, 0, [])


# ---------------------------------------------------------------------------
# Working the queue
# ---------------------------------------------------------------------------


def test_resolving_records_what_the_entry_became(client: TestClient, db: Session) -> None:
    part = make_part(db, name="LM358N dual op-amp")
    db.commit()
    _park(client)

    response = client.post("/api/intake/pending/1/resolve", json={"resolved_part_id": part.id})
    assert response.status_code == 200, response.text

    entry = response.json()
    assert entry["status"] == "resolved"
    assert entry["resolved_part_id"] == part.id
    assert entry["resolved_at"] is not None


def test_the_hint_and_the_outcome_stay_separate(client: TestClient, db: Session) -> None:
    """`part_id` is what the scan looked like; `resolved_part_id` is what was
    decided. Conflating them would quietly promote a guess to a record."""
    guessed = make_part(db, name="a plausible match")
    actual = make_part(db, name="what it really was")
    db.commit()
    _park(client, part_id=guessed.id)

    entry = client.post(
        "/api/intake/pending/1/resolve", json={"resolved_part_id": actual.id}
    ).json()
    assert entry["part_id"] == guessed.id
    assert entry["resolved_part_id"] == actual.id


def test_resolving_with_no_part_is_allowed(client: TestClient) -> None:
    """Some intakes end without a part row — the label was for something already
    stocked under another MPN, or the box turned out to be empty."""
    _park(client)
    entry = client.post("/api/intake/pending/1/resolve", json={}).json()
    assert entry["status"] == "resolved"
    assert entry["resolved_part_id"] is None


def test_a_resolved_entry_is_kept_not_deleted(client: TestClient, db: Session) -> None:
    """The raw payload is the asset, and "what did I scan last Tuesday" is worth
    being able to answer."""
    _park(client)
    client.post("/api/intake/pending/1/resolve", json={})

    assert db.execute(select(func.count()).select_from(PendingIntake)).scalar_one() == 1
    assert _list(client)["total"] == 0  # off the worklist
    assert _list(client, status="resolved")["total"] == 1  # still on record


def test_dismissed_stays_distinguishable_from_resolved(client: TestClient) -> None:
    """The two say opposite things about whether the payload is worth mining: a
    pile of dismissed unknowns is noise, a pile of resolved ones is a parser
    worth writing."""
    _park(client, client_op_id="op-a")
    _park(client, client_op_id="op-b")

    client.post("/api/intake/pending/1/resolve", json={})
    client.post("/api/intake/pending/2/dismiss", json={"note": "shipping label"})

    assert _list(client, status="resolved")["total"] == 1
    dismissed = _list(client, status="dismissed")["entries"]
    assert len(dismissed) == 1
    assert dismissed[0]["note"] == "shipping label"


def test_a_mistake_can_be_reopened(client: TestClient) -> None:
    """The desk pass is where mistakes happen and dismissing the wrong row is one
    tap. Nothing here is historical record, so undo is a status change rather than
    a compensating row — which is exactly the difference from `stock_ledger`."""
    _park(client)
    client.post("/api/intake/pending/1/dismiss", json={})

    entry = client.post("/api/intake/pending/1/reopen").json()
    assert entry["status"] == "pending"
    assert entry["resolved_at"] is None
    assert entry["resolved_part_id"] is None
    assert _list(client)["total"] == 1


def test_reopening_clears_a_wrong_resolution(client: TestClient, db: Session) -> None:
    part = make_part(db)
    db.commit()
    _park(client)
    client.post("/api/intake/pending/1/resolve", json={"resolved_part_id": part.id})

    assert client.post("/api/intake/pending/1/reopen").json()["resolved_part_id"] is None


def test_working_a_missing_entry_is_404(client: TestClient) -> None:
    for path in ("resolve", "dismiss"):
        response = client.post(f"/api/intake/pending/999999/{path}", json={})
        assert response.status_code == 404, path
        assert response.json()["detail"]["reason"] == "not_found"
    assert client.post("/api/intake/pending/999999/reopen").status_code == 404


def test_resolving_onto_an_unknown_part_is_422(client: TestClient) -> None:
    _park(client)
    response = client.post("/api/intake/pending/1/resolve", json={"resolved_part_id": 999_999})
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_part"


def test_the_intake_routes_are_in_the_openapi_document(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/intake/pending" in paths
    assert "/api/intake/pending/{entry_id}/resolve" in paths


def test_history_is_reachable_by_asking_for_every_status(client: TestClient) -> None:
    """`status` is repeatable, so "the worklist" and "everything ever scanned" are
    both expressible from a browser.

    A single `status | None` could not have meant "all": a querystring carries no
    null, so the documented escape hatch would not have existed. Caught by writing
    the test for the sentence rather than trusting it.
    """
    _park(client, client_op_id="op-a")
    _park(client, client_op_id="op-b")
    _park(client, client_op_id="op-c")
    client.post("/api/intake/pending/1/resolve", json={})
    client.post("/api/intake/pending/2/dismiss", json={})

    assert _list(client)["total"] == 1  # the worklist, by default
    every = _list(client, status=["pending", "resolved", "dismissed"])
    assert every["total"] == 3
    assert [entry["status"] for entry in every["entries"]] == [
        "resolved",
        "dismissed",
        "pending",
    ]


def test_two_statuses_can_be_combined(client: TestClient) -> None:
    """ "Everything I have finished with", without listing the open ones."""
    _park(client, client_op_id="op-a")
    _park(client, client_op_id="op-b")
    _park(client, client_op_id="op-c")
    client.post("/api/intake/pending/1/resolve", json={})
    client.post("/api/intake/pending/2/dismiss", json={})

    assert _list(client, status=["resolved", "dismissed"])["total"] == 2


def test_an_unknown_status_is_refused_rather_than_ignored(client: TestClient) -> None:
    """Silently ignoring it would return the whole table to a client that asked
    for a subset."""
    assert client.get("/api/intake/pending", params={"status": "nonsense"}).status_code == 422
