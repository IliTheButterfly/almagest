"""Regressions for what adversarial review found in the container-authoring batch.

Two defects, one shape each, and both shapes are worth naming because the next
feature in this area gets the same opportunities.

* **A route that writes a seed without asking `ensure_editable` first.** Every
  other edit on a container type — `PATCH /api/container-types/{id}`,
  `PUT .../slot-template` — goes through
  `app.services.layout_authoring.ensure_editable`, so editing a seed clones it and
  leaves the row every fresh install starts with untouched. Attaching a *photo*
  did not, so the newest way to change what a type looks like was the one way to
  change the shared original. Nothing failed: the request returned 200 and the
  picture appeared, on everyone's copy of that type forever. The shape is "a new
  mutation path added beside the guarded ones rather than through them", and it
  is quiet by construction because the guard's whole job is to be invisible when
  it does not fire.

* **A promise derived from geometry the other tier could not see.**
  `app.services.views.derive_child_view` reads a type's declared canvas
  (`grid_rows`/`grid_cols`) to answer `cabinet_face` — ADR 0006 names the Raaco's
  30x1 canvas as the example — while the client had to recover positions from
  slot-label text alone. For the two seed types whose labels are a plain
  sequence, that made the derived drawing unreachable by construction: the server
  said "draw a face of drawer fronts" and the client had nothing to draw it on.
  The fix carries the canvas rather than weakening the derivation, so the fact
  that promises the picture is the same fact that makes it drawable.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.documents import DocumentLink
from app.models.enums import EntityType
from app.models.storage import ContainerType
from app.services import views
from tests.factories import make_container_type, make_location

PNG = "image/png"


def _png(body: bytes = b"seed-photo") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + body


def _links_for_type(db: Session, container_type_id: int) -> list[DocumentLink]:
    return list(
        db.execute(
            select(DocumentLink).where(
                DocumentLink.entity_type == EntityType.CONTAINER_TYPE,
                DocumentLink.entity_pk == container_type_id,
            )
        )
        .scalars()
        .all()
    )


def _a_seed(db: Session) -> ContainerType:
    """A seed row shaped like the ones the seed migration ships."""
    seed = make_container_type(
        db,
        "shipped-cabinet",
        display_name="Shipped cabinet",
        child_layout="list",
        grid_rows=4,
        grid_cols=2,
        is_seed=True,
    )
    db.commit()
    return seed


# ---------------------------------------------------------------------------
# 1. A seed type's photo
# ---------------------------------------------------------------------------


def test_uploading_a_photo_to_a_seed_clones_it_instead_of_dressing_the_original(
    client: TestClient, db: Session
) -> None:
    """The defect: `POST /api/documents?container_type_id=<a seed>&role=photo`
    returned 200 and attached the link to the seed itself.

    `PATCH` on the same row returns `cloned=true` and a new id; this route wrote
    straight through. The damage is not to one install's data — it is that
    "every Raaco C8-30 looks like this" became a statement about a row the
    project ships, so the next person to stamp containers from the seed library
    inherits somebody else's photo of their own bench.
    """
    seed = _a_seed(db)

    response = client.post(
        "/api/documents",
        params={
            "media_type": PNG,
            "kind": "photo",
            "role": "photo",
            "container_type_id": seed.id,
            "is_primary": True,
        },
        content=_png(),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The upload says where the link actually landed, because the client
    # navigated to `seed.id` and has to follow — otherwise its next save clones
    # the seed a second time, which is the same trap `ContainerTypeScreen`
    # already avoids for `PATCH`.
    assert body["cloned_container_type"] is True
    clone_id = body["container_type_id"]
    assert clone_id != seed.id

    db.expire_all()
    assert _links_for_type(db, seed.id) == []
    assert len(_links_for_type(db, clone_id)) == 1

    clone = db.get(ContainerType, clone_id)
    assert clone is not None
    assert clone.is_seed is False
    # A clone of the shape, not just of the name: the canvas came with it, so the
    # copy is usable rather than an empty row with a photo on it.
    assert (clone.grid_rows, clone.grid_cols) == (seed.grid_rows, seed.grid_cols)


def test_attaching_an_already_stored_photo_to_a_seed_clones_it_too(
    client: TestClient, db: Session
) -> None:
    """The same hole in the other door. `POST /api/container-types/{id}/documents`
    attaches a document that is already in the store, and a client that uploaded
    first and attached second would have walked through this one instead."""
    seed = _a_seed(db)
    data = _png(b"already-stored")
    stored = client.post(
        "/api/documents", params={"media_type": PNG, "kind": "photo"}, content=data
    )
    assert stored.status_code == 200, stored.text

    response = client.post(
        f"/api/container-types/{seed.id}/documents",
        json={"sha256": hashlib.sha256(data).hexdigest(), "role": "photo", "is_primary": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cloned"] is True
    assert body["container_type_id"] != seed.id

    db.expire_all()
    assert _links_for_type(db, seed.id) == []
    assert len(_links_for_type(db, body["container_type_id"])) == 1


def test_a_photo_on_an_ordinary_type_still_lands_on_that_type(
    client: TestClient, db: Session
) -> None:
    """The guard must only fire for a seed. A clone-on-every-upload would turn
    "replace this photo" into a new type per attempt, which is a worse failure
    than the one being fixed."""
    mine = make_container_type(db, "my-own-cabinet", display_name="My own cabinet")
    db.commit()

    response = client.post(
        "/api/documents",
        params={"media_type": PNG, "kind": "photo", "role": "photo", "container_type_id": mine.id},
        content=_png(b"mine"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cloned_container_type"] is False
    assert body["container_type_id"] == mine.id

    db.expire_all()
    assert len(_links_for_type(db, mine.id)) == 1


def test_a_seed_can_therefore_never_reach_the_detach_route_with_a_photo(
    client: TestClient, db: Session
) -> None:
    """Why *detaching* needed no guard of its own, asserted rather than argued.

    Detach only ever deletes `document_links` rows whose `entity_pk` is the
    type's id, and with both attach doors cloning, a seed can no longer acquire
    one — so the only answer detach can give for a seed is the 404 it already
    gave. Pinned here because the reasoning is load-bearing: add a third writer
    of `CONTAINER_TYPE` links without `ensure_editable` and this goes red, which
    is the point at which detach would need the guard too.
    """
    seed = _a_seed(db)
    data = _png(b"never-lands-on-a-seed")
    upload = client.post(
        "/api/documents",
        params={"media_type": PNG, "kind": "photo", "role": "photo", "container_type_id": seed.id},
        content=data,
    )
    sha = hashlib.sha256(data).hexdigest()
    clone_id = upload.json()["container_type_id"]

    stale = client.delete(f"/api/container-types/{seed.id}/documents/{sha}")
    assert stale.status_code == 404
    assert stale.json()["detail"]["reason"] == "unknown_link"

    # And the copy the photo did land on can drop it, so nothing is stranded.
    removed = client.delete(f"/api/container-types/{clone_id}/documents/{sha}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["detached"] == 1
    db.expire_all()
    assert _links_for_type(db, clone_id) == []


# ---------------------------------------------------------------------------
# 2. The canvas the derived drawing needs
# ---------------------------------------------------------------------------


def test_the_tree_carries_the_canvas_a_derived_cabinet_face_needs(
    client: TestClient, db: Session
) -> None:
    """The defect, end to end: a Raaco-shaped type derives `cabinet_face` from
    its 30x1 canvas, its instances' drawers are labelled `01`...`30`, and a
    plain-numbered label carries no column — so the client could not place one
    of them and drew the fallback flow instead of the face the server promised.

    The two columns the derivation reads are now on the node that declares them,
    so both tiers read the same fact. Asserted through the route rather than
    against `views.py` because the whole defect was that the *wire* dropped it.
    """
    cabinet_type = make_container_type(
        db,
        "sequenced-cabinet",
        display_name="Sequenced cabinet",
        child_layout="list",
        grid_rows=30,
        grid_cols=1,
        slot_label_scheme="sequential",
        slot_label_params_json='{"zero_pad": 2}',
    )
    room = make_location(db, "Room")
    cabinet = make_location(
        db, "Sequenced cabinet", parent_id=room.id, container_type_id=cabinet_type.id
    )
    db.commit()

    # The server's own promise, unchanged.
    assert views.resolve_child_view(cabinet, cabinet_type) == "cabinet_face"

    nodes = {node["id"]: node for node in client.get("/api/locations/tree").json()["nodes"]}
    assert nodes[cabinet.id]["effective_child_view"] == "cabinet_face"
    assert nodes[cabinet.id]["child_grid_rows"] == 30
    assert nodes[cabinet.id]["child_grid_cols"] == 1
    # A level that declares no canvas says so, rather than reporting a guess: the
    # client refuses to draw a face without one, on purpose.
    assert nodes[room.id]["child_grid_rows"] is None
    assert nodes[room.id]["child_grid_cols"] is None


def test_the_canvas_is_reported_for_every_seed_type_that_derives_a_face(
    client: TestClient, db: Session
) -> None:
    """The two Raaco seeds are the pair the review caught, so they are named.

    An instance of each is stamped through the real route, and the assertion is
    the conjunction that was false before: `cabinet_face` *and* a canvas to draw
    it on. Either half alone passed already.
    """
    room = make_location(db, "Workshop")
    db.commit()

    types = {row["slug"]: row for row in client.get("/api/container-types").json()}
    for slug, expected_rows in (("raaco-c8-30", 30), ("raaco-c10-40", 40)):
        created = client.post(
            f"/api/locations/{room.id}/instantiate",
            json={
                "container_type_id": types[slug]["id"],
                "count": 1,
                "naming_pattern": f"{slug} {{n}}",
                "client_op_id": f"instantiate-{slug}",
            },
        )
        assert created.status_code == 201, created.text
        instance_id = created.json()["locations"][0]["id"]

        nodes = {node["id"]: node for node in client.get("/api/locations/tree").json()["nodes"]}
        node = nodes[instance_id]
        assert node["effective_child_view"] == "cabinet_face"
        assert (node["child_grid_rows"], node["child_grid_cols"]) == (expected_rows, 1)
