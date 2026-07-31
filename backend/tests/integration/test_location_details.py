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
from tests.factories import make_container_type, make_location


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
