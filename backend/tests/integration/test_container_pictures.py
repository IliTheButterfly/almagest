"""Containers get a picture — a glyph and, separately, a photo.

Iliana's request: "I want containers to have icons/pictures so I can easily
attribute them to how they look. This can be both put on a template and edited
per container." Two distinct things answer it, tested here in the order they
appear in the module docstrings that argue for keeping them apart:

1. **The glyph** (`app.models.enums.ContainerGlyph`, `app.services.glyphs`) —
   two rungs (instance override, else the container type's), no derivation,
   because nothing about a type's geometry implies what it looks like. Both
   rungs unset is "no glyph", a real terminal state.
2. **The photo** — no new column at all. A `document_links` row in
   `DocumentRole.PHOTO`, which already existed before this change, attached to
   a `container_type` or a `location`. "Override" falls out of the polymorphic
   link rather than a stored copy: a location's own primary photo wins over its
   type's, and detaching it is what "fall back to the type's" actually means.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.enums import ContainerGlyph, DocumentKind, EntityType
from app.models.storage import Location
from app.services import documents, glyphs
from tests.factories import make_container_type, make_location

PNG = "image/png"


def _png(body: bytes = b"one") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + body


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The glyph: schema shape and the two-rung resolution
# ---------------------------------------------------------------------------


def test_both_glyph_columns_are_plain_varchar(db: Session) -> None:
    """Adding a pictogram stays a one-line change in `enums.py`."""
    for table in ("container_types", "locations"):
        declared = {row[1]: row[2] for row in db.execute(text(f"PRAGMA table_info({table})")).all()}
        assert declared["glyph"].upper().startswith("VARCHAR")


def test_neither_rung_set_is_none_not_a_guess(db: Session) -> None:
    box = make_container_type(db, "unpictured-box")
    location = make_location(db, "Unpictured", container_type_id=box.id)
    db.commit()
    assert glyphs.resolve_glyph(location, box) is None


def test_the_type_default_is_used_when_the_instance_has_none(db: Session) -> None:
    reel_rack = make_container_type(db, "reel-rack", glyph=ContainerGlyph.RACK)
    location = make_location(db, "Rack by the bench", container_type_id=reel_rack.id)
    db.commit()
    assert glyphs.resolve_glyph(location, reel_rack) == ContainerGlyph.RACK


def test_the_instance_override_beats_the_type(db: Session) -> None:
    box = make_container_type(db, "assorted-box", glyph=ContainerGlyph.BOX)
    odd_one = make_location(
        db, "Actually a bag now", container_type_id=box.id, glyph=ContainerGlyph.BAG
    )
    db.commit()
    assert glyphs.resolve_glyph(odd_one, box) == ContainerGlyph.BAG


def test_there_is_no_derivation_unlike_child_view(db: Session) -> None:
    """A measured 42 mm-pitch baseplate derives `grid_cells` for `child_view`
    with no value stored anywhere. Nothing about that same geometry says what
    the container *looks like*, so an identically-shaped type gets no glyph at
    all unless one is chosen."""
    plate = make_container_type(
        db, "unpictured-plate", child_layout="grid", grid_rows=4, grid_cols=4, grid_pitch_mm=42.0
    )
    assert plate.glyph is None
    assert glyphs.resolve_glyph(None, plate) is None


def test_a_glyph_from_a_newer_build_passes_through_rather_than_raising(db: Session) -> None:
    location = make_location(db, "drawn by a future release")
    db.commit()
    db.execute(
        text("UPDATE locations SET glyph = 'isometric_hologram' WHERE id = :id"),
        {"id": location.id},
    )
    db.commit()
    db.expire_all()

    fresh = db.get(Location, location.id)
    assert fresh is not None
    assert glyphs.resolve_glyph(fresh, None) == "isometric_hologram"


def test_resolving_a_whole_tree_agrees_with_resolving_one_node(db: Session) -> None:
    rack = make_container_type(db, "batch-rack", glyph=ContainerGlyph.RACK)
    plain = make_location(db, "Rack one", container_type_id=rack.id)
    overridden = make_location(
        db, "Rack two, relabelled", container_type_id=rack.id, glyph=ContainerGlyph.BOX
    )
    untyped = make_location(db, "No type at all")
    db.commit()

    batched = glyphs.resolve_glyphs(db, [plain, overridden, untyped])
    assert batched == {
        plain.id: ContainerGlyph.RACK,
        overridden.id: ContainerGlyph.BOX,
        untyped.id: None,
    }


# ---------------------------------------------------------------------------
# The glyph, over HTTP
# ---------------------------------------------------------------------------


def test_a_type_reports_the_glyph_and_a_location_inherits_it(client: TestClient) -> None:
    created = client.post(
        "/api/container-types",
        json={"slug": "api-rack", "display_name": "Reel rack", "glyph": "rack"},
    )
    assert created.status_code == 201, created.text
    body = created.json()["container_type"]
    assert body["glyph"] == "rack"
    type_id = body["id"]

    location = client.post(
        "/api/locations", json={"name": "Rack by the door", "container_type_id": type_id}
    )
    assert location.status_code == 201, location.text
    loc_body = location.json()["location"]
    assert loc_body["glyph"] is None
    assert loc_body["effective_glyph"] == "rack"


def test_an_instance_can_override_and_unpin_its_glyph(client: TestClient) -> None:
    created = client.post(
        "/api/container-types", json={"slug": "api-box", "display_name": "Box", "glyph": "box"}
    )
    type_id = created.json()["container_type"]["id"]
    location = client.post(
        "/api/locations", json={"name": "Box that is actually a bag", "container_type_id": type_id}
    )
    location_id = location.json()["location"]["id"]

    overridden = client.put(
        f"/api/locations/{location_id}/glyph",
        json={"glyph": "bag", "client_op_id": "glyph-pin-1"},
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json() == {
        "location_id": location_id,
        "glyph": "bag",
        "effective_glyph": "bag",
        "replayed": False,
    }

    cleared = client.put(
        f"/api/locations/{location_id}/glyph",
        json={"glyph": None, "client_op_id": "glyph-clear-1"},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["glyph"] is None
    assert cleared.json()["effective_glyph"] == "box"


def test_a_type_can_clear_its_own_glyph_with_an_explicit_null(client: TestClient) -> None:
    created = client.post(
        "/api/container-types", json={"slug": "api-tray", "display_name": "Tray", "glyph": "tray"}
    )
    type_id = created.json()["container_type"]["id"]

    cleared = client.patch(
        f"/api/container-types/{type_id}", json={"glyph": None, "client_op_id": "type-glyph-clear"}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["container_type"]["glyph"] is None


def test_an_unknown_glyph_is_refused_at_the_edge(client: TestClient) -> None:
    response = client.post(
        "/api/locations", json={"name": "Nope", "glyph": "a-shape-nobody-picked"}
    )
    assert response.status_code == 422


def test_cloning_a_type_carries_its_glyph(client: TestClient) -> None:
    created = client.post(
        "/api/container-types",
        json={"slug": "api-shelf", "display_name": "Shelf", "glyph": "shelf"},
    )
    source_id = created.json()["container_type"]["id"]
    clone = client.post(
        f"/api/container-types/{source_id}/clone", json={"client_op_id": "clone-g-1"}
    )
    assert clone.status_code == 201, clone.text
    assert clone.json()["container_type"]["glyph"] == "shelf"


# ---------------------------------------------------------------------------
# The photo: a document, linked, never a column
# ---------------------------------------------------------------------------


def test_a_types_photo_is_inherited_by_its_locations(db: Session) -> None:
    box = make_container_type(db, "photographed-box")
    photo = documents.store_document(
        db, data=_png(b"box"), media_type=PNG, kind=DocumentKind.PHOTO
    ).document
    documents.attach(db, document=photo, entity_type=EntityType.CONTAINER_TYPE, entity_pk=box.id)

    location = make_location(db, "One of these boxes", container_type_id=box.id)
    db.commit()

    own = documents.primary_link(db, entity_type=EntityType.LOCATION, entity_pk=location.id)
    theirs = documents.primary_link(db, entity_type=EntityType.CONTAINER_TYPE, entity_pk=box.id)
    assert own is None
    assert theirs is not None and theirs.document_id == photo.id


def test_a_locations_own_photo_wins_over_its_types(db: Session) -> None:
    box = make_container_type(db, "photographed-box-2")
    type_photo = documents.store_document(
        db, data=_png(b"type"), media_type=PNG, kind=DocumentKind.PHOTO
    ).document
    documents.attach(
        db, document=type_photo, entity_type=EntityType.CONTAINER_TYPE, entity_pk=box.id
    )
    location = make_location(db, "This particular box", container_type_id=box.id)
    db.commit()

    own_photo = documents.store_document(
        db, data=_png(b"own"), media_type=PNG, kind=DocumentKind.PHOTO
    ).document
    documents.attach(db, document=own_photo, entity_type=EntityType.LOCATION, entity_pk=location.id)

    own = documents.primary_link(db, entity_type=EntityType.LOCATION, entity_pk=location.id)
    assert own is not None and own.document_id == own_photo.id
    assert own_photo.id != type_photo.id


def test_removing_a_locations_photo_falls_back_to_the_types(db: Session) -> None:
    box = make_container_type(db, "photographed-box-3")
    type_photo = documents.store_document(
        db, data=_png(b"type-3"), media_type=PNG, kind=DocumentKind.PHOTO
    ).document
    documents.attach(
        db, document=type_photo, entity_type=EntityType.CONTAINER_TYPE, entity_pk=box.id
    )
    location = make_location(db, "Box that had its own photo", container_type_id=box.id)
    own_photo = documents.store_document(
        db, data=_png(b"own-3"), media_type=PNG, kind=DocumentKind.PHOTO
    ).document
    documents.attach(db, document=own_photo, entity_type=EntityType.LOCATION, entity_pk=location.id)
    db.commit()

    documents.detach(db, document=own_photo, entity_type=EntityType.LOCATION, entity_pk=location.id)

    own_after = documents.primary_link(db, entity_type=EntityType.LOCATION, entity_pk=location.id)
    assert own_after is None
    theirs = documents.primary_link(db, entity_type=EntityType.CONTAINER_TYPE, entity_pk=box.id)
    assert theirs is not None and theirs.document_id == type_photo.id


def test_neither_a_type_nor_a_location_has_a_photo(db: Session) -> None:
    box = make_container_type(db, "never-photographed")
    location = make_location(db, "Nothing to see here", container_type_id=box.id)
    db.commit()

    no_own = documents.primary_link(db, entity_type=EntityType.LOCATION, entity_pk=location.id)
    no_type = documents.primary_link(db, entity_type=EntityType.CONTAINER_TYPE, entity_pk=box.id)
    assert no_own is None
    assert no_type is None


def test_cloning_a_type_does_not_carry_its_photo(db: Session) -> None:
    """Unlike `glyph`, a scalar column copied verbatim, the photo is a
    `document_links` row and stays with the original — a clone starts
    unpictured and can be given its own."""
    from app.services import layout_authoring as layout

    box = make_container_type(db, "photographed-to-clone")
    photo = documents.store_document(
        db, data=_png(b"clone-me-not"), media_type=PNG, kind=DocumentKind.PHOTO
    ).document
    documents.attach(db, document=photo, entity_type=EntityType.CONTAINER_TYPE, entity_pk=box.id)
    db.commit()

    clone = layout.clone_type(db, box, slug="photographed-to-clone-copy")
    db.commit()

    assert (
        documents.primary_link(db, entity_type=EntityType.CONTAINER_TYPE, entity_pk=clone.id)
        is None
    )


# ---------------------------------------------------------------------------
# The photo, over HTTP: upload-and-attach, the wire's effective_photo, and
# the three new routers (`container_types_router`, `locations_router`)
# ---------------------------------------------------------------------------


def _upload_png(client: TestClient, data: bytes, **params: object) -> dict[str, object]:
    response = client.post(
        "/api/documents",
        content=data,
        params={"media_type": PNG, "kind": "photo", "role": "photo", **params},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_uploading_with_two_attachment_targets_is_refused(client: TestClient) -> None:
    part = client.post("/api/parts", json={"name": "Ambiguous target"}).json()["part"]
    created = client.post(
        "/api/container-types", json={"slug": "ambiguous-type", "display_name": "Ambiguous"}
    )
    type_id = created.json()["container_type"]["id"]

    response = client.post(
        "/api/documents",
        content=_png(b"ambiguous"),
        params={"media_type": PNG, "part_id": part["id"], "container_type_id": type_id},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "ambiguous_attachment"


def test_uploading_a_photo_against_a_container_type_sets_its_effective_photo(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/container-types", json={"slug": "http-photographed", "display_name": "Box"}
    )
    type_id = created.json()["container_type"]["id"]
    assert created.json()["container_type"]["photo"] is None

    upload = _upload_png(client, _png(b"http-photo"), container_type_id=type_id)
    assert upload["link"]["role"] == "photo"
    assert upload["link"]["is_primary"] is True

    refreshed = client.get(f"/api/container-types/{type_id}")
    assert refreshed.json()["photo"]["sha256"] == upload["document"]["sha256"]

    location = client.post(
        "/api/locations", json={"name": "One instance", "container_type_id": type_id}
    )
    loc_body = location.json()["location"]
    assert loc_body["photo"] is None
    assert loc_body["effective_photo"]["sha256"] == upload["document"]["sha256"]


def test_a_locations_own_upload_overrides_and_detaching_it_falls_back(client: TestClient) -> None:
    created = client.post(
        "/api/container-types", json={"slug": "http-photographed-2", "display_name": "Box"}
    )
    type_id = created.json()["container_type"]["id"]
    type_upload = _upload_png(client, _png(b"http-type"), container_type_id=type_id)

    location = client.post(
        "/api/locations", json={"name": "Overridden instance", "container_type_id": type_id}
    )
    location_id = location.json()["location"]["id"]

    own_upload = _upload_png(client, _png(b"http-own"), location_id=location_id)
    own_sha = own_upload["document"]["sha256"]

    overridden = client.get(f"/api/locations/{location_id}")
    assert overridden.json()["photo"]["sha256"] == own_sha
    assert overridden.json()["effective_photo"]["sha256"] == own_sha

    detach = client.delete(f"/api/locations/{location_id}/documents/{own_sha}")
    assert detach.status_code == 200, detach.text

    fallen_back = client.get(f"/api/locations/{location_id}")
    assert fallen_back.json()["photo"] is None
    assert fallen_back.json()["effective_photo"]["sha256"] == type_upload["document"]["sha256"]


def test_container_type_documents_router_lists_attaches_and_detaches(client: TestClient) -> None:
    created = client.post(
        "/api/container-types", json={"slug": "router-check", "display_name": "Box"}
    )
    type_id = created.json()["container_type"]["id"]
    upload = _upload_png(client, _png(b"router"))  # not attached yet
    sha256 = upload["document"]["sha256"]

    empty = client.get(f"/api/container-types/{type_id}/documents")
    assert empty.status_code == 200
    assert empty.json() == {"container_type_id": type_id, "links": []}

    attach = client.post(
        f"/api/container-types/{type_id}/documents",
        json={"sha256": sha256, "role": "photo", "is_primary": True},
    )
    assert attach.status_code == 200, attach.text
    assert attach.json()["created"] is True

    listing = client.get(f"/api/container-types/{type_id}/documents")
    assert [link["document"]["sha256"] for link in listing.json()["links"]] == [sha256]

    detach = client.delete(f"/api/container-types/{type_id}/documents/{sha256}")
    assert detach.status_code == 200, detach.text
    assert detach.json()["detached"] == 1

    detach_again = client.delete(f"/api/container-types/{type_id}/documents/{sha256}")
    assert detach_again.status_code == 404
    assert detach_again.json()["detail"]["reason"] == "unknown_link"


def test_location_documents_router_lists_attaches_and_detaches(client: TestClient) -> None:
    location = client.post("/api/locations", json={"name": "Router-checked bin"})
    location_id = location.json()["location"]["id"]
    upload = _upload_png(client, _png(b"router-loc"))
    sha256 = upload["document"]["sha256"]

    attach = client.post(
        f"/api/locations/{location_id}/documents",
        json={"sha256": sha256, "role": "photo", "is_primary": True},
    )
    assert attach.status_code == 200, attach.text

    listing = client.get(f"/api/locations/{location_id}/documents")
    assert listing.json()["location_id"] == location_id
    assert len(listing.json()["links"]) == 1

    detach = client.delete(f"/api/locations/{location_id}/documents/{sha256}")
    assert detach.status_code == 200, detach.text
    assert detach.json()["detached"] == 1


def test_attaching_to_an_unknown_container_type_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/container-types/999999/documents",
        json={"sha256": "0" * 64, "role": "photo"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_container_type"


def test_uploading_against_an_unknown_location_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/documents",
        content=_png(b"nolocation"),
        params={"media_type": PNG, "location_id": 999999},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_location"
