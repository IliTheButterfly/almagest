"""`/s/{short_id}` — the URL stamped into every NFC tag and printed QR.

These tests are worth more than they look. This path is written into physical
objects: getting it wrong is not a deploy away from being fixed, it is a
re-tagging session with a phone and 300 drawers.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_session_factory
from app.models.enums import EntityType
from app.services import shortid


def _bind(entity_type: EntityType, *, name: str = "Drawer A1") -> tuple[str, int]:
    """Create a row, give it a short ID, return (code, pk)."""
    session: Session = get_session_factory()()
    try:
        if entity_type == EntityType.LOCATION:
            from app.models.storage import Location

            row = Location(name=name)
        else:
            from sqlalchemy import select

            from app.models.catalog import Part, PartKind

            kind = session.execute(
                select(PartKind).where(PartKind.slug == "component")
            ).scalar_one()
            row = Part(name=name, part_kind_id=kind.id, mpn="MPN-123")
        session.add(row)
        session.flush()
        code = shortid.allocate(session, entity_type, row.id)
        session.commit()
        return code, row.id
    finally:
        session.close()


def test_resolves_a_location(client: TestClient) -> None:
    code, pk = _bind(EntityType.LOCATION)
    body = client.get(f"/api/resolve/{code}").json()

    assert body["status"] == "resolved"
    assert body["target"]["entity_type"] == "location"
    assert body["target"]["entity_pk"] == pk
    assert body["target"]["label"] == "Drawer A1"


def test_resolves_a_part_without_being_told_the_type(client: TestClient) -> None:
    """One shared ID space: a scan resolves with no prior context about what
    was scanned, which is what lets a single endpoint handle everything."""
    code, pk = _bind(EntityType.PART, name="A resistor")
    body = client.get(f"/api/resolve/{code}").json()

    assert body["target"]["entity_type"] == "part"
    assert body["target"]["entity_pk"] == pk
    assert body["target"]["label"] == "MPN-123"


def test_the_display_form_carries_the_cosmetic_prefix(client: TestClient) -> None:
    code, _ = _bind(EntityType.LOCATION)
    body = client.get(f"/api/resolve/{code}").json()
    assert body["target"]["display"].startswith("BIN ")
    assert "-" in body["target"]["display"]


def test_the_path_is_derived_never_stored(client: TestClient) -> None:
    """A container's path is computed at read time. Encoding it in the payload
    would make the tag a lie the moment the drawer changed cabinet."""
    session: Session = get_session_factory()()
    try:
        from app.models.storage import Location
        from app.services.tree import location_tree

        cabinet = Location(name="Cabinet A")
        session.add(cabinet)
        session.flush()
        drawer = Location(name="Drawer 3", parent_id=cabinet.id)
        session.add(drawer)
        session.flush()
        location_tree(session).rebuild_paths()
        code = shortid.allocate(session, EntityType.LOCATION, drawer.id)
        session.commit()
        drawer_id = drawer.id
    finally:
        session.close()

    assert client.get(f"/api/resolve/{code}").json()["target"]["label_path"] == (
        "Cabinet A / Drawer 3"
    )

    # Move the drawer; the same unchanged tag must now report the new path.
    session = get_session_factory()()
    try:
        from app.models.storage import Location
        from app.services.tree import location_tree

        other = Location(name="Cabinet B")
        session.add(other)
        session.flush()
        location_tree(session).rebuild_paths()
        drawer = session.get(Location, drawer_id)
        assert drawer is not None
        location_tree(session).move(drawer, other.id)
        session.commit()
    finally:
        session.close()

    assert client.get(f"/api/resolve/{code}").json()["target"]["label_path"] == (
        "Cabinet B / Drawer 3"
    )


def test_a_well_formed_but_unknown_code_is_the_provisioning_case(client: TestClient) -> None:
    """A blank tag is not an error — it is a tag waiting to be bound."""
    body = client.get(f"/api/resolve/{shortid.generate()}").json()
    assert body["status"] == "unknown"
    assert body["target"] is None
    assert body["normalized"]


def test_a_malformed_code_is_404_with_a_reason(client: TestClient) -> None:
    response = client.get("/api/resolve/NOTACODE")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] in {"check", "alphabet", "length"}


def test_a_transposed_code_is_caught(client: TestClient) -> None:
    """The check symbol earning its keep against a mistyped label."""
    code, _ = _bind(EntityType.LOCATION)
    swapped = code[1] + code[0] + code[2:]
    if swapped != code:
        assert client.get(f"/api/resolve/{swapped}").status_code == 404


# ---------------------------------------------------------------------------
# The physical entry point
# ---------------------------------------------------------------------------


def test_tapping_a_tag_redirects_into_the_pwa(client: TestClient) -> None:
    code, pk = _bind(EntityType.LOCATION)
    response = client.get(f"/s/{code}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == f"/locations/{pk}"


def test_the_printed_hyphenated_form_works(client: TestClient) -> None:
    """What is printed on a label must be typable into the address bar."""
    code, pk = _bind(EntityType.LOCATION)
    printed = shortid.format_display(code)
    response = client.get(f"/s/{printed}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == f"/locations/{pk}"


def test_an_unknown_tag_lands_on_provisioning(client: TestClient) -> None:
    code = shortid.generate()
    response = client.get(f"/s/{code}", follow_redirects=False)
    assert response.headers["location"] == f"/provision?code={code}"


def test_a_garbage_tag_lands_somewhere_useful(client: TestClient) -> None:
    """Never a stack trace on a phone held against a drawer."""
    response = client.get("/s/GARBAGE!", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/scan?unknown=")


def test_the_landing_route_is_not_part_of_the_api_contract(client: TestClient) -> None:
    """`/s/` is a human-facing URL, not something a generated client calls."""
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/s/") for path in paths)
    assert "/api/resolve/{short_id}" in paths


def test_the_tag_payload_matches_the_configured_base_url(client: TestClient) -> None:
    """What gets written to a tag is `{base_url}/s/{short_id}`. This asserts
    the two halves of that string agree with the route that actually exists —
    a mismatch would only be discovered after tags were provisioned."""
    code, pk = _bind(EntityType.LOCATION)
    payload = f"{get_settings().base_url}/s/{code}"

    path = payload.removeprefix(get_settings().base_url)
    assert client.get(path, follow_redirects=False).headers["location"] == f"/locations/{pk}"
