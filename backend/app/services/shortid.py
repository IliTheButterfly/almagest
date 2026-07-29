"""Short IDs: the codec, plus the half that needs a database.

The codec — Crockford base32, 7 data symbols and a mod-37 check symbol, rendered
``4K7T-92M8`` — lives in the dependency-free `idcodec.shortid` package, because
the station agent needs the same rules on a Raspberry Pi and must not install
FastAPI and SQLAlchemy to get them. See `idcodec/README.md`.

**It is re-exported here rather than imported at each call site.** Binding a code
to a row and computing a code are the same subject from a caller's point of view,
and `app.services.shortid` is where that subject already lives. So
``shortid.validate(...)``, ``shortid.generate(...)`` and ``shortid.allocate(...)``
all keep working, and nothing in `app.api` had to change when the codec moved.

What is *here* is everything that takes a `Session`:

* `allocate` — mint a code and bind it, retrying on collision;
* `adopt` — bind an already-printed code, refusing to substitute;
* `primary_short_id` — the code this object gets printed on next;
* `resolve` — what a scanned code refers to.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from idcodec.shortid import (
    ALPHABET,
    BASE,
    CHECK_MODULUS,
    DATA_SYMBOLS,
    TOTAL_SYMBOLS,
    InvalidShortId,
    check_value,
    format_display,
    generate,
    is_valid,
    normalize,
    validate,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import EntityType
from app.models.identity import ObjectId

#: Re-exports are explicit: `mypy --strict` disables implicit re-export, so
#: without this every `from app.services.shortid import validate` would be an
#: error. Keeping the codec's names listed here is also the compatibility
#: promise — removing one from this list breaks call sites, so it cannot happen
#: by accident.
__all__ = [
    "ALPHABET",
    "BASE",
    "CHECK_MODULUS",
    "DATA_SYMBOLS",
    "TOTAL_SYMBOLS",
    "InvalidShortId",
    "ShortIdExhausted",
    "ShortIdTaken",
    "adopt",
    "allocate",
    "check_value",
    "format_display",
    "generate",
    "is_valid",
    "normalize",
    "primary_short_id",
    "resolve",
    "validate",
]


class ShortIdExhausted(RuntimeError):
    """Every generation attempt collided. Effectively impossible; not ignored."""


def allocate(
    session: Session,
    entity_type: EntityType | str,
    entity_pk: int,
    *,
    is_primary: bool = True,
    randbits: Callable[[int], int] = secrets.randbits,
    max_attempts: int = 8,
) -> str:
    """Mint a short ID and bind it to a row, retrying on collision.

    `object_ids.short_id` is the primary key, so a collision is *detected* by
    the database rather than silently overwriting. At ~5x10^4 objects over 32^7
    the birthday probability is around 3.6% — so this will happen eventually,
    and it has to cost one retry rather than corrupt anything.

    Uses a SAVEPOINT per attempt so a collision does not poison the caller's
    transaction.
    """
    for _ in range(max_attempts):
        candidate = generate(randbits)
        if session.get(ObjectId, candidate) is not None:
            continue
        with session.begin_nested():
            session.add(
                ObjectId(
                    short_id=candidate,
                    entity_type=str(entity_type),
                    entity_pk=entity_pk,
                    is_primary=is_primary,
                )
            )
        return candidate
    raise ShortIdExhausted(f"could not mint a free short id in {max_attempts} attempts")


def primary_short_id(session: Session, entity_type: EntityType | str, entity_pk: int) -> str | None:
    """The code this object gets printed on, if it has one yet.

    Ordered `is_primary` first then oldest, so a relabelled object reports its
    current label while the superseded ones stay resolvable.
    """
    return (
        session.execute(
            select(ObjectId.short_id)
            .where(
                ObjectId.entity_type == str(entity_type),
                ObjectId.entity_pk == entity_pk,
            )
            .order_by(ObjectId.is_primary.desc(), ObjectId.created_at)
        )
        .scalars()
        .first()
    )


class ShortIdTaken(ValueError):
    """The requested code is already bound to a different object."""

    def __init__(
        self, short_id: str, *, entity_type: str | None = None, entity_pk: int | None = None
    ) -> None:
        held = f"{entity_type} {entity_pk}" if entity_type is not None else "another object"
        super().__init__(f"short id {short_id} is already bound to {held}")
        self.short_id = short_id
        self.entity_type = entity_type
        self.entity_pk = entity_pk


def adopt(
    session: Session,
    entity_type: EntityType | str,
    entity_pk: int,
    raw: str,
    *,
    is_primary: bool = True,
) -> str:
    """Bind an **already printed** code to a row, rather than minting a new one.

    `allocate` chooses the code, which is right whenever the database exists
    before the physical artifact. It cannot serve the reverse order: pre-printed
    label stock, a bag of pre-encoded NFC tags, or re-adopting a tag after
    restoring a backup that predates the binding. In all three the code is
    already on the object and the database has to accept it.

    Three deliberate refusals, because this is the one path where a mistake
    produces a *resolvable* wrong answer that no later scan can detect:

    * The check symbol is verified. A code mistyped off a label would otherwise
      be bound as itself and resolve happily forever, pointing at the wrong bin.
    * A collision raises `ShortIdTaken` rather than minting a substitute. The
      label is already printed, so a substitute would put the physical world and
      the database permanently out of step — the failure this whole scheme
      exists to prevent.
    * Re-adopting the same code for the same row is a no-op, so a retried
      request or a re-scanned label succeeds rather than erroring.
    """
    short_id = validate(raw)

    existing = session.get(ObjectId, short_id)
    if existing is not None:
        if existing.entity_type != str(entity_type) or existing.entity_pk != entity_pk:
            raise ShortIdTaken(
                short_id, entity_type=existing.entity_type, entity_pk=existing.entity_pk
            )
        if is_primary:
            _make_primary(session, existing)
        return short_id

    row = ObjectId(
        short_id=short_id,
        entity_type=str(entity_type),
        entity_pk=entity_pk,
        is_primary=is_primary,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError as error:
        # Lost the race between the read above and the insert. Only reachable
        # with a second writer, which the deployment forbids — reported rather
        # than retried, because retrying would mint a different code.
        raise ShortIdTaken(short_id) from error

    if is_primary:
        _make_primary(session, row)
    return short_id


def _make_primary(session: Session, row: ObjectId) -> None:
    """Make `row` the one ID this object gets printed on, demoting the rest.

    An object may accumulate IDs — a relabelled bin keeps its old code
    resolvable, so the label still stuck to it and the one already in someone's
    hand both work. But only one is printed *next*, and adopting a new label is
    exactly the moment the old one stops being it. No constraint can express
    "exactly one", so it is maintained here rather than assumed.
    """
    session.execute(
        update(ObjectId)
        .where(
            ObjectId.entity_type == row.entity_type,
            ObjectId.entity_pk == row.entity_pk,
            ObjectId.short_id != row.short_id,
        )
        .values(is_primary=False)
    )
    row.is_primary = True


def resolve(session: Session, raw: str) -> ObjectId | None:
    """Look up whatever a scanned or typed code refers to.

    Returns the binding, not the target row: one shared ID space means the
    caller learns the entity *type* from the result rather than needing to know
    it in advance. That is what lets a single scan endpoint resolve anything.
    """
    try:
        canonical = validate(raw)
    except InvalidShortId:
        return None
    return session.execute(
        select(ObjectId).where(ObjectId.short_id == canonical)
    ).scalar_one_or_none()
