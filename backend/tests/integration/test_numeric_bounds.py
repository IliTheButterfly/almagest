"""Every numeric request field is bounded.

Found by adversarial review and reproduced before fixing: a bare `int` in
Pydantic has no upper bound, so `10**30` validated cleanly, reached
`session.flush()`, and died in sqlite3's parameter binding with
``OverflowError: Python int too large to convert to SQLite INTEGER``. Nothing
caught it, so the client got a bare **500** for input that is obviously a 422.

The original bug was not one missing constraint. It was eight fields each
inventing their own, which is why this suite enumerates them: a ninth field
added without a bound should fail a test here rather than in production.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.limits import MASS_MG_MAX, MONEY_MICRO_MAX, QTY_MILLI_MAX
from app.db.session import get_session_factory

#: Comfortably past SQLite's signed 64-bit maximum, so it would fail at binding
#: rather than merely being an implausible quantity.
ABSURD = 10**30


def _fixtures() -> tuple[int, int]:
    from app.models.catalog import Part, PartKind
    from app.models.storage import Location
    from app.services.tree import location_tree

    session = get_session_factory()()
    try:
        kind = session.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()
        part = Part(name="bounds test part", part_kind_id=kind.id)
        location = Location(name="bounds test bin")
        session.add_all([part, location])
        session.flush()
        location_tree(session).rebuild_paths()
        ids = (part.id, location.id)
        session.commit()
        return ids
    finally:
        session.close()


def _lot(client: TestClient, part_id: int, location_id: int) -> int:
    response = client.post(
        "/api/stock/receive",
        json={"part_id": part_id, "location_id": location_id, "qty_milli": 1000},
    )
    assert response.status_code == 200, response.text
    return response.json()["lot"]["id"]


# ---------------------------------------------------------------------------
# The reproduction case
# ---------------------------------------------------------------------------


def test_an_absurd_receive_quantity_is_422_not_500(client: TestClient) -> None:
    """The exact input that produced an unhandled OverflowError before the fix."""
    part_id, location_id = _fixtures()
    response = client.post(
        "/api/stock/receive",
        json={"part_id": part_id, "location_id": location_id, "qty_milli": ABSURD},
    )
    assert response.status_code == 422, response.text


def test_nothing_was_written_by_the_rejected_request(client: TestClient) -> None:
    """Rejection has to happen before the ledger, not halfway through it."""
    from app.models.stock import StockLedger

    part_id, location_id = _fixtures()
    client.post(
        "/api/stock/receive",
        json={"part_id": part_id, "location_id": location_id, "qty_milli": ABSURD},
    )

    session = get_session_factory()()
    try:
        assert session.execute(select(StockLedger)).first() is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Every field that carries a number
# ---------------------------------------------------------------------------


def test_every_stock_movement_field_rejects_an_absurd_value(client: TestClient) -> None:
    part_id, location_id = _fixtures()
    lot_id = _lot(client, part_id, location_id)

    cases: list[tuple[str, dict[str, object]]] = [
        (f"/api/stock/lots/{lot_id}/consume", {"qty_milli": ABSURD}),
        (f"/api/stock/lots/{lot_id}/return", {"qty_milli": ABSURD}),
        (f"/api/stock/lots/{lot_id}/adjust", {"delta_milli": ABSURD}),
        (f"/api/stock/lots/{lot_id}/adjust", {"delta_milli": -ABSURD}),
        (f"/api/stock/lots/{lot_id}/recount", {"counted_qty_milli": ABSURD}),
        (
            f"/api/stock/lots/{lot_id}/recount",
            {"counted_qty_milli": 10, "measured_mass_mg": ABSURD},
        ),
        (
            f"/api/stock/lots/{lot_id}/move",
            {"to_location_id": location_id, "qty_milli": ABSURD},
        ),
    ]
    for path, body in cases:
        response = client.post(path, json=body)
        assert response.status_code == 422, f"{path} {body} -> {response.status_code}"


def test_receive_cost_and_location_tare_are_bounded(client: TestClient) -> None:
    part_id, location_id = _fixtures()

    over_cost = client.post(
        "/api/stock/receive",
        json={
            "part_id": part_id,
            "location_id": location_id,
            "qty_milli": 10,
            "unit_cost_micro": ABSURD,
        },
    )
    assert over_cost.status_code == 422

    over_tare = client.post("/api/locations", json={"name": "heavy", "tare_mg": ABSURD})
    assert over_tare.status_code == 422


@pytest.mark.parametrize("bad_dpi", [ABSURD, 0, -1, 1_000_000])
def test_label_sheet_dpi_is_bounded(client: TestClient, bad_dpi: int) -> None:
    """`LabelDpi` has both a floor and a ceiling, unlike a row id — 0/negative
    catch a typo, and the upper bound catches a client confusing dpi with
    dots-per-mm, well before either reaches a PIL image size calculation."""
    location = client.post("/api/locations", json={"name": "dpi bounds test"}).json()["location"]
    response = client.post(
        "/api/labels/sheets",
        json={"template": "cabinet_card", "root_location_id": location["id"], "dpi": bad_dpi},
    )
    assert response.status_code == 422, response.text


def test_the_alias_quantity_hint_is_bounded(client: TestClient) -> None:
    """It comes straight off a scanned barcode's Q field, which is exactly the
    untrusted path this was reported against."""
    response = client.post(
        "/api/scan/alias",
        json={
            "code": "SOME-VENDOR-PAYLOAD",
            "entity_type": "part",
            "entity_pk": _fixtures()[0],
            "hint_qty_milli": ABSURD,
        },
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# The bounds still admit anything real
# ---------------------------------------------------------------------------


def test_a_value_at_the_limit_is_accepted(client: TestClient) -> None:
    """The bound must reject nonsense without second-guessing a big reel."""
    part_id, location_id = _fixtures()
    response = client.post(
        "/api/stock/receive",
        json={"part_id": part_id, "location_id": location_id, "qty_milli": QTY_MILLI_MAX},
    )
    assert response.status_code == 200, response.text


def test_a_realistic_reel_is_nowhere_near_the_bound() -> None:
    """A 5000-piece reel is 5e6 milli-units — six orders of magnitude of slack."""
    assert QTY_MILLI_MAX / 100_000 > 5_000 * 1000


def test_the_bounds_leave_headroom_for_an_accumulating_balance() -> None:
    """The reason these are domain bounds rather than SQLite's own maximum:
    `qty_milli_cached` accumulates, so capping a single write at 2^63-1 would
    still let a few thousand writes overflow the cache — a silent corruption
    instead of a clean rejection."""
    sqlite_max = 2**63 - 1
    assert sqlite_max > QTY_MILLI_MAX * 1_000_000
    assert sqlite_max > MONEY_MICRO_MAX * 1_000
    assert sqlite_max > MASS_MG_MAX * 1_000_000


@pytest.mark.parametrize("bad", [0, -1])
def test_a_take_still_requires_a_positive_quantity(client: TestClient, bad: int) -> None:
    """Adding an upper bound must not have loosened the lower one."""
    part_id, location_id = _fixtures()
    lot_id = _lot(client, part_id, location_id)
    assert (
        client.post(f"/api/stock/lots/{lot_id}/consume", json={"qty_milli": bad}).status_code == 422
    )


# ---------------------------------------------------------------------------
# Row ids overflow the same way
# ---------------------------------------------------------------------------
#
# Not in the original report, found by probing while fixing it: a `part_id` of
# 10**30 reaches `Session.get()`, which binds it as a query parameter and raises
# OverflowError before any "not found" check can run. Six routes returned 500.


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/stock/receive", {"part_id": ABSURD, "location_id": 1, "qty_milli": 10}),
        ("post", "/api/stock/receive", {"part_id": 1, "location_id": ABSURD, "qty_milli": 10}),
        ("post", "/api/locations/suggest", {"part_id": ABSURD}),
        ("post", "/api/stock/undo", {"seq": ABSURD}),
        ("get", f"/api/parts/{ABSURD}", None),
        ("get", f"/api/locations/{ABSURD}", None),
        ("get", f"/api/stock/lots/{ABSURD}", None),
        ("get", f"/api/stock/lots/{ABSURD}/history", None),
        # The provisioning walk: session ids in the path, and the slot id a tap
        # jumps the cursor to. `tag_uid` needs no bound of this kind — it is a
        # string — but it is length-capped for the same reason.
        ("post", f"/api/locations/{ABSURD}/provisioning-sessions", {}),
        ("get", f"/api/locations/{ABSURD}/provisioning-sessions/current", None),
        ("post", f"/api/locations/{ABSURD}/verification-sessions", {}),
        ("post", f"/api/provisioning-sessions/{ABSURD}/bind", {"tag_uid": "04AABB"}),
        ("post", "/api/provisioning-sessions/1/bind", {"tag_uid": "04AABB", "location_id": ABSURD}),
        ("post", f"/api/provisioning-sessions/{ABSURD}/skip", {}),
        ("post", f"/api/provisioning-sessions/{ABSURD}/undo", {}),
        ("post", f"/api/verification-sessions/{ABSURD}/check", {"tag_uid": "04AABB"}),
        ("get", f"/api/verification-sessions/{ABSURD}", None),
        ("post", f"/api/location-tags/{ABSURD}/unbind", {}),
        (
            "post",
            "/api/labels/sheets",
            {"template": "drawer_card", "root_location_id": ABSURD},
        ),
        (
            "post",
            "/api/labels/sheets",
            {"template": "drawer_card", "root_location_id": 1, "slot_ids": [ABSURD]},
        ),
        ("get", f"/api/labels/sheets/{ABSURD}", None),
    ],
)
def test_an_absurd_row_id_is_422_not_500(
    client: TestClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    response = client.get(path) if method == "get" else client.post(path, json=body)
    assert response.status_code == 422, f"{method.upper()} {path} -> {response.status_code}"


@pytest.mark.parametrize("bad", [0, -1])
def test_a_nonsense_row_id_is_rejected_rather_than_looked_up(client: TestClient, bad: int) -> None:
    """0 and negative are never valid ids, so 422 is more honest than a 404 from a
    lookup that was always going to miss."""
    assert client.get(f"/api/parts/{bad}").status_code == 422


def test_a_valid_but_missing_row_id_is_still_404(client: TestClient) -> None:
    """Bounding ids must not turn "not found" into "malformed" for ordinary
    values — that distinction is what the UI branches on."""
    assert client.get("/api/parts/999999").status_code == 404
    assert client.get("/api/locations/999999").status_code == 404
