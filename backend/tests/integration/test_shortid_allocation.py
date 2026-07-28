"""Binding short IDs to rows through the shared `object_ids` space."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import EntityType
from app.models.identity import ObjectId
from app.services import shortid
from app.services.shortid import ShortIdExhausted
from tests.factories import make_location, make_part


def test_allocate_binds_and_resolves(db: Session) -> None:
    location = make_location(db, name="Drawer A1")
    code = shortid.allocate(db, EntityType.LOCATION, location.id)
    db.commit()

    binding = shortid.resolve(db, code)
    assert binding is not None
    assert binding.entity_type == EntityType.LOCATION
    assert binding.entity_pk == location.id


def test_a_scan_resolves_without_knowing_the_type(db: Session) -> None:
    """One shared ID space is what lets a single endpoint resolve anything —
    the caller learns the type *from* the result instead of supplying it."""
    part = make_part(db)
    location = make_location(db)
    part_code = shortid.allocate(db, EntityType.PART, part.id)
    location_code = shortid.allocate(db, EntityType.LOCATION, location.id)
    db.commit()

    assert shortid.resolve(db, part_code).entity_type == EntityType.PART  # type: ignore[union-attr]
    assert shortid.resolve(db, location_code).entity_type == EntityType.LOCATION  # type: ignore[union-attr]


def test_resolution_accepts_the_printed_form(db: Session) -> None:
    location = make_location(db)
    code = shortid.allocate(db, EntityType.LOCATION, location.id)
    db.commit()

    printed = shortid.format_display(code, EntityType.LOCATION)
    assert shortid.resolve(db, printed) is not None
    assert shortid.resolve(db, printed.lower()) is not None


def test_unknown_or_malformed_codes_resolve_to_none(db: Session) -> None:
    assert shortid.resolve(db, "not a code") is None
    assert shortid.resolve(db, shortid.generate()) is None


def test_a_collision_costs_one_retry_not_corruption(db: Session) -> None:
    """`short_id` is the primary key, so a duplicate is *detected*. At ~5x10^4
    objects the birthday probability is about 3.6%, so this will happen."""
    location = make_location(db)
    other = make_location(db, name="Other")

    # A generator that returns the same value twice, then differs.
    sequence = iter([12345, 12345, 99999])

    def fixed(_bits: int) -> int:
        return next(sequence)

    first = shortid.allocate(db, EntityType.LOCATION, location.id, randbits=fixed)
    db.commit()

    second = shortid.allocate(db, EntityType.LOCATION, other.id, randbits=fixed)
    db.commit()

    assert first != second
    assert db.execute(select(func.count()).select_from(ObjectId)).scalar_one() == 2


def test_exhaustion_raises_rather_than_returning_a_duplicate(db: Session) -> None:
    location = make_location(db)
    shortid.allocate(db, EntityType.LOCATION, location.id, randbits=lambda _bits: 777)
    db.commit()

    with pytest.raises(ShortIdExhausted):
        shortid.allocate(
            db, EntityType.LOCATION, location.id, randbits=lambda _bits: 777, max_attempts=3
        )


def test_an_object_may_hold_several_codes(db: Session) -> None:
    """A relabelled bin keeps its old code resolvable, so a label still in the
    wild does not become a dead end."""
    location = make_location(db)
    rng = random.Random(7)
    old = shortid.allocate(
        db, EntityType.LOCATION, location.id, is_primary=False, randbits=rng.getrandbits
    )
    new = shortid.allocate(db, EntityType.LOCATION, location.id, randbits=rng.getrandbits)
    db.commit()

    assert shortid.resolve(db, old).entity_pk == location.id  # type: ignore[union-attr]
    assert shortid.resolve(db, new).entity_pk == location.id  # type: ignore[union-attr]


def test_most_locations_have_no_short_id_at_all(db: Session) -> None:
    """Auto-generated grid slots are addressed as parent + slot label. Nobody
    is sticking 96 labels on an 8x12 assortment box, and absence of an
    `object_ids` row is how "no printed label" is represented."""
    cabinet = make_location(db, name="Assortment box")
    for row in "ABCDEFGH":
        for col in range(1, 13):
            make_location(db, name=f"{row}{col}", parent_id=cabinet.id, slot_label=f"{row}{col}")
    shortid.allocate(db, EntityType.LOCATION, cabinet.id)
    db.commit()

    assert db.execute(select(func.count()).select_from(ObjectId)).scalar_one() == 1
