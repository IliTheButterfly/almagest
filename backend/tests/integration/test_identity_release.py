"""A deleted container gives up its names, whichever code deleted it.

`locations.id` is an `INTEGER PRIMARY KEY` with no `AUTOINCREMENT`, so SQLite
reuses a freed rowid, and the four tables that address a location by that
integer are all polymorphic and so cannot carry a foreign key. A row left behind
is therefore not a dangling reference — it is **adopted by the next container
created**, and a printed card silently starts meaning a different drawer.

Three separate paths delete a container and two of them forgot. This pins the
invariant at the layer that cannot forget, and names each path so a fourth is
caught by the first test that exercises it.

Real Alembic migrations, per `tests/conftest.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import EntityType
from app.models.identity import ObjectId
from app.models.storage import Location
from app.services import shortid
from app.services.tree import location_tree
from tests.factories import make_location


def _codes_for(db: Session, location_id: int) -> int:
    return db.execute(
        select(func.count())
        .select_from(ObjectId)
        .where(ObjectId.entity_type == EntityType.LOCATION, ObjectId.entity_pk == location_id)
    ).scalar_one()


def test_the_removal_path_releases_the_code(client: TestClient, db: Session) -> None:
    location = make_location(db, name="Doomed")
    location_tree(db).rebuild_paths()
    shortid.allocate(db, EntityType.LOCATION, location.id)
    db.commit()
    # Read before the delete: the instance cannot answer for its own id
    # afterwards, which is the same reason the listener takes `target.id` while
    # the row is still there.
    location_id = location.id
    assert _codes_for(db, location_id) == 1

    removed = client.delete(f"/api/locations/{location_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted_location_ids"] == [location_id]

    db.expire_all()
    assert _codes_for(db, location_id) == 0


def test_a_relaid_out_slot_releases_its_code(db: Session) -> None:
    """The second path. Found by review after a card printed for one cell
    resolved to a different cell following a merge and a re-split."""
    parent = make_location(db, name="Cabinet")
    slot = make_location(db, name="A1", parent_id=parent.id, row_idx=0, col_idx=0)
    location_tree(db).rebuild_paths()
    shortid.allocate(db, EntityType.LOCATION, slot.id)
    db.commit()
    slot_id = slot.id

    db.delete(db.get(Location, slot_id))
    db.flush()
    db.commit()

    assert _codes_for(db, slot_id) == 0


def test_deleting_a_project_staging_box_releases_its_code(client: TestClient, db: Session) -> None:
    """The third path, and the one that proved a helper is the wrong shape.

    `projects._remove_unreferenced_staging_boxes` deletes a project's boxes when
    the project goes. It never released anything, because nothing about writing
    `session.delete(location)` suggests an obligation is attached to it — which
    is exactly why the obligation now lives on the model.
    """
    created = client.post("/api/projects", json={"name": "Blinky"})
    assert created.status_code in {200, 201}, created.text
    project_id = created.json()["project"]["id"]

    box = make_location(db, name="Blinky parts")
    location_tree(db).rebuild_paths()
    shortid.allocate(db, EntityType.LOCATION, box.id)
    db.commit()
    box_id = box.id

    attached = client.put(
        f"/api/projects/{project_id}", json={"name": "Blinky", "staging_location_id": box_id}
    )
    assert attached.status_code in {200, 404, 405, 422}, attached.text

    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code in {200, 204}, deleted.text

    db.expire_all()
    gone = db.get(Location, box_id) is None
    if not gone:
        # The box survived (still referenced, or this install does not sweep
        # them) — then its code must still be there. The invariant is "no code
        # outlives its location", not "the box is always deleted".
        assert _codes_for(db, box_id) == 1
        return
    assert _codes_for(db, box_id) == 0, (
        "the staging box was deleted and its short id outlived it — the next "
        "container created will inherit the rowid and answer to that card"
    )


def test_the_code_is_not_inherited_by_the_next_container(db: Session) -> None:
    """The failure this exists to prevent, end to end.

    A dangling code that resolves to nothing is a nuisance. A code that resolves
    to *somebody else's drawer* is the one `removal.py` calls worse than no scan
    at all, and rowid reuse is what turns the first into the second.
    """
    doomed = make_location(db, name="Doomed")
    location_tree(db).rebuild_paths()
    code = shortid.allocate(db, EntityType.LOCATION, doomed.id)
    db.commit()
    doomed_id = doomed.id

    db.delete(db.get(Location, doomed_id))
    db.flush()
    db.commit()

    replacement = make_location(db, name="Innocent")
    location_tree(db).rebuild_paths()
    db.commit()

    resolved = db.execute(select(ObjectId).where(ObjectId.short_id == code)).scalar_one_or_none()
    assert resolved is None, (
        f"{code} still resolves, and now points at location {replacement.id}"
        if resolved is not None and resolved.entity_pk == replacement.id
        else f"{code} outlived the container it named"
    )


def test_no_bulk_delete_bypasses_the_listener() -> None:
    """`before_delete` fires for ORM deletes only.

    A `delete(Location).where(...)` Core statement would remove rows without it,
    silently reopening the whole class. There is no such statement in `app/`, and
    this says so rather than leaving it as an assumption somebody has to re-check
    by reading.
    """
    root = Path(__file__).resolve().parents[2] / "app"
    found = subprocess.run(
        ["grep", "-rn", "-E", r"delete\(\s*Location\s*\)", str(root)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    # `models/events.py` says the words in its own docstring, explaining why this
    # test exists. Excluded by path rather than by cleverness about comments.
    hits = "\n".join(
        line for line in found.splitlines() if "/models/events.py:" not in line
    ).strip()

    assert hits == "", f"a bulk delete of Location bypasses the identity listener:\n{hits}"
