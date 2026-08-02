"""Renaming a container where it stands — the write half of the edit mode.

Iliana: *"I don't like the multiple pages per container to edit them. I'd much
prefer a page and an edit mode... Use pop up panels to edit details like name,
description and such."* Before this route there was **no way to change a
location's name at all** — it was settable only at `POST /api/locations`.

The two properties worth pinning are both about a rename's reach:

* `label_path` is a cache of the names down the chain, so renaming a cabinet has
  to restate the path of every drawer inside it. That runs through
  `TreeRepository.rebuild_paths`, and this asserts the descendants, not the row.
* Nothing physical moves. The printed code carries no name and no path (which is
  the whole reason renaming is allowed to be this cheap), so a rename must leave
  the `short_id`, the tag binding and `last_printed_at` exactly as they were.

Real Alembic migrations, per `tests/conftest.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EntityType
from app.models.storage import Location, LocationTag
from app.services import shortid
from app.services.tree import location_tree
from tests.factories import make_container_type, make_location, make_lot, make_part


def _chain(db: Session, *names: str) -> list[Location]:
    rows: list[Location] = []
    parent_id: int | None = None
    for name in names:
        row = make_location(db, name=name, parent_id=parent_id)
        rows.append(row)
        parent_id = row.id
    location_tree(db).rebuild_paths()
    db.commit()
    return rows


def _details(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Renamed",
        "description": None,
        "esd_safe": None,
        "is_placeable": None,
    }
    body.update(overrides)
    return body


def test_a_rename_restates_every_descendant_path(client: TestClient, db: Session) -> None:
    """The cache, not the row, is the thing that can be silently wrong here."""
    workshop, cabinet, drawer = _chain(db, "Workshop", "Cabinet A", "Drawer B2")

    response = client.put(
        f"/api/locations/{cabinet.id}/details", json=_details(name="Cabinet Alpha")
    )
    assert response.status_code == 200, response.text
    assert response.json()["location"]["label_path"] == "Workshop / Cabinet Alpha"

    db.expire_all()
    assert db.get(Location, drawer.id).label_path == "Workshop / Cabinet Alpha / Drawer B2"
    # And nothing above it moved.
    assert db.get(Location, workshop.id).label_path == "Workshop"


def test_a_rename_touches_nothing_physical(client: TestClient, db: Session) -> None:
    """A label carries the opaque code, never the name — so renaming must not
    re-mint, re-print or unbind anything. If it did, every rename would be a trip
    to the drawer with a label printer."""
    (drawer,) = _chain(db, "Drawer")
    code = shortid.allocate(db, EntityType.LOCATION, drawer.id)
    db.add(
        LocationTag(
            location_id=drawer.id,
            tag_uid="04AABBCCDDEE80",
            ndef_url=f"https://almagest.lan/s/{code}",
            written_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    drawer.last_printed_at = datetime(2026, 7, 1, tzinfo=UTC)
    db.commit()

    response = client.put(f"/api/locations/{drawer.id}/details", json=_details(name="Drawer, ESD"))
    assert response.status_code == 200, response.text
    body = response.json()["location"]
    assert body["short_id"] == code
    assert body["last_printed_at"] is not None

    db.expire_all()
    tag = db.execute(select(LocationTag).where(LocationTag.location_id == drawer.id)).scalar_one()
    assert tag.tag_uid == "04AABBCCDDEE80"


def test_a_cleared_description_box_means_no_description(client: TestClient, db: Session) -> None:
    """A panel that was emptied has to store null and not "", or the read side
    has two falsy values meaning the same thing."""
    (bin_,) = _chain(db, "Bin")
    bin_.description = "Assorted"
    db.commit()

    body = client.put(
        f"/api/locations/{bin_.id}/details", json=_details(name="Bin", description="   ")
    ).json()
    assert body["location"]["description"] is None
    db.expire_all()
    assert db.get(Location, bin_.id).description is None


def test_esd_safe_can_be_set_and_handed_back_to_the_ancestor(
    client: TestClient, db: Session
) -> None:
    """Null is a real edit, not an omission: it stops this container answering
    for itself and inherits again — which is what makes marking a whole cabinet
    ESD-safe one edit rather than ninety-six."""
    cabinet, drawer = _chain(db, "Cabinet", "Drawer")
    cabinet.esd_safe = True
    db.commit()

    pinned = client.put(
        f"/api/locations/{drawer.id}/details", json=_details(name="Drawer", esd_safe=False)
    ).json()["location"]
    assert pinned["esd_safe"] is False
    assert pinned["effective_esd_safe"] is False

    cleared = client.put(
        f"/api/locations/{drawer.id}/details", json=_details(name="Drawer", esd_safe=None)
    ).json()["location"]
    assert cleared["esd_safe"] is None
    assert cleared["effective_esd_safe"] is True


def test_is_placeable_null_hands_the_answer_back_to_the_type(
    client: TestClient, db: Session
) -> None:
    """The same tri-state, one rung down: a room that only holds cabinets is
    marked unplaceable here, and clearing it defers to the container type."""
    kind = make_container_type(db, slug="rack", is_placeable=False)
    row = make_location(db, name="Rack", container_type_id=kind.id, is_placeable=True)
    location_tree(db).rebuild_paths()
    db.commit()

    body = client.put(
        f"/api/locations/{row.id}/details", json=_details(name="Rack", is_placeable=None)
    ).json()["location"]
    assert body["is_placeable"] is None


def test_a_replayed_edit_is_not_applied_twice(client: TestClient, db: Session) -> None:
    """Same `client_op_id`, same answer — a flaky phone connection must not turn
    one rename into a rename plus a stale overwrite of whatever came after it."""
    (bin_,) = _chain(db, "Bin")
    payload = _details(name="Bin 1", client_op_id="11111111-2222-3333-4444-555555555555")

    first = client.put(f"/api/locations/{bin_.id}/details", json=payload)
    second = client.put(f"/api/locations/{bin_.id}/details", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["location"]["name"] == "Bin 1"


def test_a_blank_name_is_refused(client: TestClient, db: Session) -> None:
    """Every screen in the system identifies a container by its name."""
    (bin_,) = _chain(db, "Bin")
    assert (
        client.put(f"/api/locations/{bin_.id}/details", json=_details(name="")).status_code == 422
    )


def test_an_unknown_container_is_a_404(client: TestClient) -> None:
    assert client.put("/api/locations/9999/details", json=_details()).status_code == 404


def test_a_drawer_names_the_parts_it_holds(client: TestClient, db: Session) -> None:
    """The landing screen for every tag tap has to say what is in the drawer.

    It rendered "part 4" — a primary key — because the payload carried no name,
    so a bin holding two different parts could not be told apart without opening
    each one and coming back. The names come from the lot rows themselves; one
    request per row is not an option on a screen reached by tapping a sticker.
    """
    drawer = make_location(db, name="Drawer 01")
    resistor = make_part(db, name="4k7 0.25W resistor, axial", mpn="CFR-25JB-52-4K7")
    capacitor = make_part(db, name="100nF X7R capacitor", mpn="C0603C104K5RAC")
    make_lot(db, resistor, drawer, qty_milli=250_000)
    make_lot(db, capacitor, drawer, qty_milli=500_000)
    db.commit()

    body = client.get(f"/api/locations/{drawer.id}").json()

    named = {lot["part_name"] for lot in body["lots"]}
    assert named == {"4k7 0.25W resistor, axial", "100nF X7R capacitor"}
    # The MPN too: two parts can share a description and never share this.
    assert {lot["part_mpn"] for lot in body["lots"]} == {
        "CFR-25JB-52-4K7",
        "C0603C104K5RAC",
    }


def test_naming_the_parts_does_not_cost_a_query_per_lot(client: TestClient, db: Session) -> None:
    """The control for handing `lot_read` its parts instead of trusting the map.

    Asserted as *"does not grow"* rather than *"is exactly one"*: the route
    touches `parts` for its own reasons too, and pinning an absolute number would
    fail the next time something unrelated is added — the kind of test people
    delete. What must never change is the slope. Revert to `Session.get` per lot
    and a six-part drawer costs six queries, on the screen every tag tap lands
    on, invisibly until the database is big.
    """
    from sqlalchemy import event

    def part_queries(location: Location) -> int:
        seen: list[str] = []

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if statement.lstrip().upper().startswith("SELECT") and "parts" in statement.lower():
                seen.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            body = client.get(f"/api/locations/{location.id}").json()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert all(lot["part_name"] is not None for lot in body["lots"])
        return len(seen)

    small = make_location(db, name="Drawer S")
    for index in range(2):
        make_lot(db, make_part(db, name=f"Small {index}"), small, qty_milli=1_000)
    big = make_location(db, name="Drawer L")
    for index in range(8):
        make_lot(db, make_part(db, name=f"Big {index}"), big, qty_milli=1_000)
    db.commit()

    assert part_queries(big) == part_queries(small)
