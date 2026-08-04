"""The abort guarantee, proved against the real routes, a real socket and real rows.

`tests/test_session.py` proves the state machine's half of this against a fake API,
where "nothing was written" is an empty list. Here it is a **`SELECT count(*)` over
`stock_ledger`** — the only form of that assertion that cannot be satisfied by a
fake with the wrong idea of what a commit does.

What is real: a temp SQLite database with **Alembic migrations applied** (so the
append-only triggers exist), the FastAPI app served by uvicorn on a loopback port,
and `HttpStationApi` talking to it over TCP with `urllib`. What is fake: the reader,
because no PN532 exists.

This is the one test in the agent that costs a server start-up, and it is worth it:
it is simultaneously the contract test for the hand-written HTTP client, the proof
that `client_op_id` really does make a retry idempotent, and the proof that a
removed container writes nothing.
"""

from __future__ import annotations

import threading
import time
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from app.db.session import get_session_factory, reset_engine_for_testing
from app.models.catalog import Part, PartKind
from app.models.enums import LedgerSource, NdefState, ProvisioningKind
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location, LocationTag
from app.models.system import ClientOperation
from app.models.types import utcnow
from app.services import ledger, provisioning, shortid
from app.services.ledger import Attribution
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent import events
from agent.api import HttpStationApi
from agent.tags import TagRead
from tests.conftest import Station
from tests.test_session import body, types

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"

#: The tag physically on the drawer in this test. Written with separators, the way
#: a PN532 library prints one, so the normalisation that makes it match
#: `location_tags.tag_uid` is exercised rather than assumed.
TAG_UID_RAW = "04:1A:2B:3C:4D:5E:6F"
TAG_UID = "041A2B3C4D5E6F"

STARTING_QTY_MILLI = 250_000
TAKE_MILLI = 5_000


def _alembic_config(database_url: str) -> Config:
    """Mirrors `backend/tests/conftest.py`: real migrations, never `create_all`.

    `create_all` builds the schema the models describe, so it cannot catch
    model/migration drift — and the ledger's append-only triggers live in a
    migration and are invisible to the models, which makes them the reason this
    test is meaningful at all.

    The URL goes through `-x url=`, which is the *only* override `alembic/env.py`
    honours: it reads `app.config` otherwise, so a `sqlalchemy.url` main option is
    ignored and the migration silently runs against the developer's own database
    while the test then talks to an empty temp file. That failure looks exactly
    like a broken migration, so it is worth naming here.
    """
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.cmd_opts = Namespace(x=[f"url={database_url}"])
    return cfg


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Session]:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'station-test.db'}"
    command.upgrade(_alembic_config(database_url), "head")
    engine = reset_engine_for_testing(database_url)
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def seeded(db: Session) -> tuple[Location, StockLot]:
    """One tagged drawer holding one lot of 250 units.

    Seeded through `app.services.ledger`, which is the sole ledger writer — a test
    that inserted the row itself would be the second writer this design forbids,
    and its starting balance would not have gone through the cache update the
    assertions below depend on.
    """
    kind = db.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()
    part = Part(name="1k 0805 resistor", part_kind_id=kind.id)
    location = Location(name="A3")
    db.add_all([part, location])
    db.flush()

    lot, _ = ledger.find_or_create_lot(db, part_id=part.id, location=location)
    ledger.receive(
        db,
        lot,
        STARTING_QTY_MILLI,
        attribution=Attribution(source=LedgerSource.IMPORT, note="test seed"),
    )
    short_id = shortid.allocate(db, "location", location.id)
    db.add(
        LocationTag(
            location_id=location.id,
            tag_uid=TAG_UID,
            ndef_url=f"https://almagest.aether.lan/s/{short_id}",
            written_at=utcnow(),
        )
    )
    db.commit()
    return location, lot


def _api_app() -> FastAPI:
    """The three real routers the station calls, mounted with their own prefixes.

    **Not `app.main`**, and the reason is a dependency: `app.main` includes the
    label routes, which import `reportlab` and `PIL` from the backend's optional
    `labels` extra. The Pi's venv deliberately does not install it — CI syncs the
    agent without `--all-extras` for the same reason — so importing the whole app
    here would make this test require a compiled wheel the agent never ships.

    The routers themselves are the real thing, prefixes included, so every path,
    request model, response model and idempotency guard under test is production
    code. That `app.main` mounts these same routers is covered from the other side
    by `tests/test_api_contract.py`, which reads the committed `openapi.json`.
    """
    from app.api.routes import location_tags, locations, stock

    app = FastAPI()
    app.include_router(stock.router)
    app.include_router(locations.router)
    app.include_router(location_tags.router)
    return app


@pytest.fixture
def api_url(db: Session) -> Iterator[str]:
    """The real routes on a real loopback port, in a thread.

    A thread rather than a subprocess so the server shares this process's engine —
    which is how it sees the temp database the `db` fixture pointed it at. The
    start-up spin is bounded and is the only wall-clock wait in the suite; it is a
    server binding a socket, not a state machine being given time to settle.
    """
    server = uvicorn.Server(
        uvicorn.Config(_api_app(), host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - a wedged server
            raise RuntimeError("the test API server never started")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


@pytest.fixture
def bench(api_url: str) -> Iterator[Station]:
    """A station whose API is the real one, reached over HTTP."""
    station = Station(HttpStationApi(api_url, device_id="station-under-test", timeout_s=10.0))
    yield station
    station.close()


def ledger_rows(db: Session) -> int:
    # A fresh transaction every time: the API committed in another thread, and this
    # session would otherwise answer from a snapshot taken before it did.
    db.rollback()
    return db.execute(select(func.count()).select_from(StockLedger)).scalar_one()


def balance(db: Session, lot: StockLot) -> int:
    db.rollback()
    return db.execute(select(StockLot.qty_milli_cached).where(StockLot.id == lot.id)).scalar_one()


def tag() -> TagRead:
    return TagRead(uid=TAG_UID_RAW, ndef_url=None)


# ---------------------------------------------------------------------------


def test_a_placement_identifies_the_drawer_and_writes_nothing(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """Identification is two reads. PLAN.md: nothing touches the ledger before
    COMMIT, and that has to be true of the identify step too."""
    location, lot = seeded
    before = ledger_rows(db)

    ready = body(bench.place(tag()), events.STATION_READY)
    assert ready["location_id"] == location.id
    assert ready["name"] == "A3"
    assert ready["label_path"] == location.label_path
    assert ready["matched_by"] == "uid"
    assert ready["total_qty_milli"] == STARTING_QTY_MILLI
    assert ready["lots"][0]["lot_id"] == lot.id
    assert ledger_rows(db) == before


def test_removing_the_container_before_commit_leaves_the_ledger_untouched(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """**The guarantee.** A half-finished session that commits is stock that moved
    without anyone saying so, so this asserts the two things that would show it:
    the row count, and the cached balance."""
    _, lot = seeded
    before = ledger_rows(db)

    bench.place(tag())
    bench.send(
        events.STATION_PROPOSE,
        action={"kind": "take", "lot_id": lot.id, "qty_milli": TAKE_MILLI},
    )
    stale = bench.session.session_id

    aborted = body(bench.lift(), events.STATION_ABORTED)
    assert aborted["reason"] == "removed"
    assert aborted["discarded"] == {"kind": "take", "lot_id": lot.id, "qty_milli": TAKE_MILLI}

    # And the tap that raced the lift lands nowhere.
    bench.send(events.STATION_CONFIRM, session_id=stale)
    assert ledger_rows(db) == before
    assert balance(db, lot) == STARTING_QTY_MILLI


def test_a_confirmed_take_writes_exactly_one_row_through_the_stock_routes(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """The agent is not a second ledger writer: this row was appended by
    `app/services/ledger.py` behind `POST /api/stock/lots/{id}/consume`."""
    _, lot = seeded
    before = ledger_rows(db)

    ready = body(bench.place(tag()), events.STATION_READY)
    key = ready["client_op_id"]
    bench.send(
        events.STATION_PROPOSE,
        action={"kind": "take", "lot_id": lot.id, "qty_milli": TAKE_MILLI},
    )
    emitted = bench.send(events.STATION_CONFIRM)
    assert types(emitted) == [events.STATION_COMMITTED, events.STATION_READY]

    committed = body(emitted, events.STATION_COMMITTED)
    assert committed["replayed"] is False
    assert ledger_rows(db) == before + 1
    assert balance(db, lot) == STARTING_QTY_MILLI - TAKE_MILLI
    assert body(emitted, events.STATION_READY)["total_qty_milli"] == (
        STARTING_QTY_MILLI - TAKE_MILLI
    )

    db.rollback()
    row = db.execute(
        select(StockLedger).where(StockLedger.seq == committed["seqs"][0])
    ).scalar_one()
    assert row.client_op_id == key
    assert row.delta_milli == -TAKE_MILLI
    # `scan`, because the container identified itself by tag. `manual` would erase
    # that, and `scale` would claim a measurement ADR 0003 says does not exist.
    assert row.source == LedgerSource.SCAN
    operation = db.get(ClientOperation, key)
    assert operation is not None and operation.device_id == "station-under-test"


def test_the_same_key_committed_twice_moves_stock_once(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """The reason the key is minted at identify time. Sent by hand a second time —
    what a client retry looks like from the API's side — and the API replays the
    stored response rather than appending a second row, which on an append-only
    ledger could only be corrected by writing a third."""
    _, lot = seeded
    key = str(body(bench.place(tag()), events.STATION_READY)["client_op_id"])
    bench.send(
        events.STATION_PROPOSE,
        action={"kind": "take", "lot_id": lot.id, "qty_milli": TAKE_MILLI},
    )
    bench.send(events.STATION_CONFIRM)
    after_first = ledger_rows(db)

    replayed = bench.commit_directly(
        kind="take", lot_id=lot.id, qty_milli=TAKE_MILLI, client_op_id=key
    )
    assert replayed.replayed is True
    assert ledger_rows(db) == after_first
    assert balance(db, lot) == STARTING_QTY_MILLI - TAKE_MILLI


def test_a_take_that_would_go_negative_is_still_accepted(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """PLAN.md: a movement that drives the balance negative is a dashboard anomaly
    to investigate, not a reason to refuse the record of what physically happened.
    Asserted here because the station is the most likely place to produce one."""
    _, lot = seeded
    bench.place(tag())
    bench.send(
        events.STATION_PROPOSE,
        action={"kind": "take", "lot_id": lot.id, "qty_milli": STARTING_QTY_MILLI + 1_000},
    )
    emitted = bench.send(events.STATION_CONFIRM)
    assert events.STATION_COMMITTED in types(emitted)
    assert balance(db, lot) == -1_000


def test_a_lot_in_another_drawer_cannot_be_named_by_a_command(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """The command vocabulary is "the lot you told me about". Proved against real
    rows: a second lot exists, is perfectly movable through the API, and is
    invisible to this socket."""
    location, _ = seeded
    kind = db.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()
    other_part = Part(name="10k 0805 resistor", part_kind_id=kind.id)
    elsewhere = Location(name="B7")
    db.add_all([other_part, elsewhere])
    db.flush()
    other_lot, _ = ledger.find_or_create_lot(db, part_id=other_part.id, location=elsewhere)
    ledger.receive(db, other_lot, 1_000, attribution=Attribution(source=LedgerSource.IMPORT))
    db.commit()
    before = ledger_rows(db)

    bench.place(tag())
    emitted = bench.send(
        events.STATION_PROPOSE,
        action={"kind": "take", "lot_id": other_lot.id, "qty_milli": 500},
    )
    assert body(emitted, events.STATION_REJECTED)["reason"] == "unknown_lot"
    assert ledger_rows(db) == before
    assert location.id != elsewhere.id


def test_an_unprovisioned_tag_falls_through_to_provisioning(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """A blank tag against the real resolver: `status: unknown`, which the station
    turns into an offer rather than a dead end."""
    unbound = body(
        bench.place(TagRead(uid="0455555555555555", ndef_url=None)),
        events.STATION_UNIDENTIFIED,
    )
    assert unbound["reason"] == "unknown_tag"
    assert unbound["tag_uid"] == "0455555555555555"
    assert unbound["offers"] == ["manual_search", "provision"]


def test_a_drawer_provisioned_from_a_phone_identifies_itself_at_the_station(
    db: Session, seeded: tuple[Location, StockLot], bench: Station
) -> None:
    """The seam between the two halves of the tag story, closed.

    Every other test here seeds `location_tags` directly, which proves the station
    reads a binding but says nothing about whether the *provisioning walk* writes
    the binding the station will look for. Those are different code paths in
    different processes — a phone running the walk, a Pi running the agent — and
    the only thing that makes them agree is that both fold a UID through
    `idcodec.tagpayload.normalize_tag_uid`.

    So this binds through `app.services.provisioning`, exactly as
    `POST /api/provisioning-sessions/{id}/bind` does, and then places the drawer on
    the bench. The UID is written in the colon-separated form a PN532 library
    prints and bound in the bare form a phone reports, because that mismatch is
    precisely what a second, subtly different normalisation would hide: the
    binding would look perfect in the database and the station would report an
    unknown tag forever.
    """
    location, lot = seeded
    existing = db.execute(
        select(LocationTag).where(LocationTag.location_id == location.id)
    ).scalar_one()
    db.delete(existing)
    db.flush()

    cabinet = Location(name="Bench cabinet")
    db.add(cabinet)
    db.flush()
    location.parent_id = cabinet.id
    db.flush()

    walk = provisioning.open_session(db, cabinet, kind=ProvisioningKind.PROVISION)
    outcome = provisioning.bind(db, walk, tag_uid="041a2b3c4d5e6f", location_id=location.id)
    db.commit()

    assert outcome.status == "bound"
    assert outcome.tag is not None
    # Nothing has written the sticker yet, and the record says so rather than
    # assuming the phone succeeded.
    assert outcome.tag.ndef_state == NdefState.UNVERIFIED

    ready = body(bench.place(tag()), events.STATION_READY)
    assert ready["location_id"] == location.id
    assert ready["matched_by"] == "uid"
    assert ready["total_qty_milli"] == STARTING_QTY_MILLI
    assert ready["lots"][0]["lot_id"] == lot.id
