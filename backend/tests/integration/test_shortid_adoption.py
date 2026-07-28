"""Adopting an already-printed short ID, and promoting a slot to a printed one.

`allocate` is right whenever the database exists before the physical artifact.
These are the reverse order — pre-printed label stock, pre-encoded tags, a tag
that outlived the backup it was bound in — where the code is already on the
object and the database has to take it.

The property under test throughout is that **nothing here can quietly disagree
with the physical world.** A substituted code, or a mistyped one bound as
itself, both produce a binding that resolves perfectly while pointing at the
wrong drawer, and no later scan can detect either.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EntityType
from app.models.identity import ObjectId
from app.services import provisioning, shortid
from app.services.shortid import InvalidShortId, ShortIdTaken
from tests.factories import make_location, make_part

# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def test_adopt_binds_the_code_it_was_given(db: Session) -> None:
    location = make_location(db, name="Drawer A1")
    printed = shortid.generate()

    assert shortid.adopt(db, EntityType.LOCATION, location.id, printed) == printed
    db.commit()

    binding = shortid.resolve(db, printed)
    assert binding is not None
    assert binding.entity_pk == location.id


def test_adopt_accepts_the_code_as_a_human_would_read_it_off_a_label(db: Session) -> None:
    """Hyphenated, lower case, with the cosmetic display prefix — the three
    forms someone copying a card actually types."""
    location = make_location(db)
    printed = shortid.generate()
    display = shortid.format_display(printed, EntityType.LOCATION)

    assert shortid.adopt(db, EntityType.LOCATION, location.id, display.lower()) == printed


def test_a_mistyped_code_is_refused_rather_than_bound_as_itself(db: Session) -> None:
    """**The most dangerous input on this path.** A wrong code bound as itself
    resolves happily forever, pointing at the wrong bin, and nothing downstream
    can tell. The check symbol is the only thing standing there."""
    location = make_location(db)
    printed = shortid.generate()
    typo = printed[:-1] + shortid.ALPHABET[(shortid.ALPHABET.index(printed[-1]) + 1) % 32]

    with pytest.raises(InvalidShortId) as excinfo:
        shortid.adopt(db, EntityType.LOCATION, location.id, typo)
    assert excinfo.value.reason == "check"
    assert db.get(ObjectId, typo) is None


def test_a_collision_is_refused_and_never_substituted(db: Session) -> None:
    """`allocate` retries on collision, which is right when it owns the choice.
    Adoption must not: the code is already printed, so minting a different one
    would put the label and the database permanently out of step."""
    first = make_location(db, name="Drawer A1")
    second = make_location(db, name="Drawer B2")
    printed = shortid.adopt(db, EntityType.LOCATION, first.id, shortid.generate())
    db.commit()

    with pytest.raises(ShortIdTaken) as excinfo:
        shortid.adopt(db, EntityType.LOCATION, second.id, printed)
    assert excinfo.value.short_id == printed
    assert excinfo.value.entity_pk == first.id

    db.rollback()
    assert shortid.primary_short_id(db, EntityType.LOCATION, second.id) is None


def test_a_collision_across_entity_types_is_still_a_collision(db: Session) -> None:
    """One shared ID space is the whole point — a code taken by a part is taken."""
    part = make_part(db)
    location = make_location(db)
    printed = shortid.adopt(db, EntityType.PART, part.id, shortid.generate())
    db.commit()

    with pytest.raises(ShortIdTaken):
        shortid.adopt(db, EntityType.LOCATION, location.id, printed)


def test_readopting_the_same_code_for_the_same_row_is_a_no_op(db: Session) -> None:
    """A retried request, or the same label scanned twice, must succeed — and
    must not leave two rows behind."""
    location = make_location(db)
    printed = shortid.adopt(db, EntityType.LOCATION, location.id, shortid.generate())
    db.commit()

    assert shortid.adopt(db, EntityType.LOCATION, location.id, printed) == printed
    db.commit()

    codes = list(
        db.execute(
            select(ObjectId.short_id).where(
                ObjectId.entity_type == EntityType.LOCATION,
                ObjectId.entity_pk == location.id,
            )
        ).scalars()
    )
    assert codes == [printed]


# ---------------------------------------------------------------------------
# Relabelling keeps the old code resolvable
# ---------------------------------------------------------------------------


def test_relabelling_keeps_the_superseded_code_resolvable(db: Session) -> None:
    """The label still stuck to the drawer, and the one already in your hand,
    both have to keep working — otherwise relabelling is destructive and nobody
    will risk it."""
    location = make_location(db, name="Drawer A1")
    old = shortid.allocate(db, EntityType.LOCATION, location.id)
    db.commit()

    new = shortid.adopt(db, EntityType.LOCATION, location.id, shortid.generate())
    db.commit()

    for code in (old, new):
        binding = shortid.resolve(db, code)
        assert binding is not None
        assert binding.entity_pk == location.id


def test_exactly_one_code_is_primary_after_relabelling(db: Session) -> None:
    """No constraint can express "exactly one", so the invariant is asserted."""
    location = make_location(db)
    shortid.allocate(db, EntityType.LOCATION, location.id)
    new = shortid.adopt(db, EntityType.LOCATION, location.id, shortid.generate())
    db.commit()

    primary = list(
        db.execute(
            select(ObjectId.short_id).where(
                ObjectId.entity_type == EntityType.LOCATION,
                ObjectId.entity_pk == location.id,
                ObjectId.is_primary,
            )
        ).scalars()
    )
    assert primary == [new]
    assert shortid.primary_short_id(db, EntityType.LOCATION, location.id) == new


def test_the_printed_card_follows_the_new_label(db: Session) -> None:
    """`provisioning.printed_short_id` delegates to `primary_short_id`, so the
    card, the tag payload and this route cannot disagree about which code is
    current. They used to be two copies of the same query."""
    location = make_location(db)
    shortid.allocate(db, EntityType.LOCATION, location.id)
    new = shortid.adopt(db, EntityType.LOCATION, location.id, shortid.generate())
    db.commit()

    assert provisioning.printed_short_id(db, location.id) == new
    assert provisioning.ndef_url_for(db, location).endswith(f"/s/{new}")


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def _location(client: TestClient, name: str, **extra: object) -> int:
    response = client.post("/api/locations", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return int(response.json()["location"]["id"])


def test_minting_promotes_a_slot_that_had_no_printed_id(client: TestClient) -> None:
    """What "any cell can be promoted later" on `POST /api/locations` means."""
    location_id = _location(client, "Cell C7", mint_short_id=False)
    assert client.get(f"/api/locations/{location_id}").json()["short_id"] is None

    response = client.post(f"/api/locations/{location_id}/short-id", json={})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["adopted"] is False
    assert shortid.is_valid(body["short_id"])
    assert body["display"].startswith("BIN ")
    assert client.get(f"/api/locations/{location_id}").json()["short_id"] == body["short_id"]


def test_minting_twice_returns_the_same_code(client: TestClient) -> None:
    """Safe to call from a print button without checking first — which is the
    only way a print button can be written, since printing is what needs the id."""
    location_id = _location(client, "Cell C8", mint_short_id=False)
    first = client.post(f"/api/locations/{location_id}/short-id", json={}).json()["short_id"]
    second = client.post(f"/api/locations/{location_id}/short-id", json={}).json()["short_id"]
    assert first == second


def test_adopting_binds_the_caller_s_code(client: TestClient) -> None:
    location_id = _location(client, "Drawer with a pre-printed card", mint_short_id=False)
    printed = shortid.format_display(shortid.generate())

    response = client.post(f"/api/locations/{location_id}/short-id", json={"short_id": printed})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["adopted"] is True
    assert body["short_id"] == shortid.normalize(printed)
    assert body["previous_short_id"] is None
    assert client.get(f"/api/resolve/{body['short_id']}").status_code == 200


def test_adopting_reports_the_code_it_superseded(client: TestClient) -> None:
    location_id = _location(client, "Relabelled drawer")
    original = client.get(f"/api/locations/{location_id}").json()["short_id"]
    assert original is not None

    body = client.post(
        f"/api/locations/{location_id}/short-id",
        json={"short_id": shortid.generate()},
    ).json()
    assert body["previous_short_id"] == original
    # Reported, not removed: the old card still resolves.
    assert client.get(f"/api/resolve/{original}").status_code == 200


def test_a_mistyped_code_is_422_with_the_reason(client: TestClient) -> None:
    location_id = _location(client, "Drawer typo", mint_short_id=False)
    printed = shortid.generate()
    typo = printed[:-1] + shortid.ALPHABET[(shortid.ALPHABET.index(printed[-1]) + 1) % 32]

    response = client.post(f"/api/locations/{location_id}/short-id", json={"short_id": typo})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "check"


@pytest.mark.parametrize("bad", ["", "4K7T", "4K7T92M8XX", "4K7T92MU"])
def test_malformed_codes_are_422_not_500(client: TestClient, bad: str) -> None:
    location_id = _location(client, f"Drawer {bad or 'empty'}", mint_short_id=False)
    response = client.post(f"/api/locations/{location_id}/short-id", json={"short_id": bad})
    assert response.status_code == 422, response.text


def test_a_taken_code_is_409_naming_the_drawer_that_holds_it(client: TestClient) -> None:
    """ "Already bound to Cabinet A / Drawer B2" tells you which drawer to go
    look at. "Already bound to location 41" makes you go and query for it."""
    holder = _location(client, "Drawer B2")
    printed = client.post(f"/api/locations/{holder}/short-id", json={}).json()["short_id"]
    other = _location(client, "Drawer C3", mint_short_id=False)

    response = client.post(f"/api/locations/{other}/short-id", json={"short_id": printed})
    assert response.status_code == 409, response.text

    detail = response.json()["detail"]
    assert detail["reason"] == "short_id_taken"
    assert detail["short_id"] == printed
    assert detail["held_by"] is not None
    assert "Drawer B2" in detail["held_by"]

    # And the refusal left nothing behind.
    assert client.get(f"/api/locations/{other}").json()["short_id"] is None


def test_a_refused_adoption_does_not_consume_the_client_op_id(client: TestClient) -> None:
    """A 409 is not a stored outcome, so correcting the code and retrying with
    the same `client_op_id` must work rather than replaying the failure."""
    holder = _location(client, "Drawer D1")
    taken = client.post(f"/api/locations/{holder}/short-id", json={}).json()["short_id"]
    other = _location(client, "Drawer D2", mint_short_id=False)

    op = "0189d1c0-0000-4000-8000-000000000abc"
    assert (
        client.post(
            f"/api/locations/{other}/short-id",
            json={"short_id": taken, "client_op_id": op},
        ).status_code
        == 409
    )

    free = shortid.generate()
    retry = client.post(
        f"/api/locations/{other}/short-id",
        json={"short_id": free, "client_op_id": op},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["short_id"] == free


def test_the_same_request_replays_rather_than_binding_twice(client: TestClient) -> None:
    location_id = _location(client, "Drawer E1", mint_short_id=False)
    payload = {
        "short_id": shortid.generate(),
        "client_op_id": "0189d1c0-0000-4000-8000-0000000abcd",
    }

    first = client.post(f"/api/locations/{location_id}/short-id", json=payload).json()
    second = client.post(f"/api/locations/{location_id}/short-id", json=payload).json()

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["short_id"] == first["short_id"]


def test_an_unknown_location_is_404(client: TestClient) -> None:
    assert client.post("/api/locations/999999/short-id", json={}).status_code == 404
