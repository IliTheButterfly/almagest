"""Crockford base32 short IDs with a mod-37 check symbol.

Format: **7 data symbols + 1 check symbol**, rendered ``4K7T-92M8``. The hyphen
is cosmetic and never stored.

The alphabet drops ``I``, ``L``, ``O`` and ``U``: the first three because they
are confusable with ``1`` and ``0`` on a printed label read at arm's length,
the last because excluding it prevents the generator producing an obscenity.

Why mod **37** specifically: 37 is the smallest prime above the 32-symbol
alphabet, and primality is what makes the check exhaustive rather than
probabilistic. With the check computed as ``(Σ dᵢ·32ⁱ) mod 37``:

* a **single wrong symbol** shifts the sum by ``(d' − d)·32ⁱ``, and since
  ``0 < |d' − d| < 32 < 37`` and ``32ⁱ`` is invertible mod 37, that shift can
  never be ``0``;
* an **adjacent transposition** shifts it by ``(dᵢ₊₁ − dᵢ)·32ⁱ·(1 − 32)``, and
  ``−31 ≢ 0 (mod 37)``, so that cannot vanish either.

Those are precisely the two mistakes humans make copying a code by eye.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import EntityType
from app.models.identity import ObjectId

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BASE = len(ALPHABET)
DATA_SYMBOLS = 7
CHECK_MODULUS = 37
TOTAL_SYMBOLS = DATA_SYMBOLS + 1

_VALUE_OF = {symbol: index for index, symbol in enumerate(ALPHABET)}

#: Applied before validation. Crockford's canonical confusions, and nothing
#: else — `U` is deliberately *not* remapped, because it is excluded from the
#: alphabet rather than merged into another symbol.
_CONFUSIONS = str.maketrans({"O": "0", "I": "1", "L": "1"})

_STRIPPABLE = re.compile(r"[\s\-_.]+")


def _squash(text: str) -> str:
    """Remove cosmetic separators, upper-case, fold the confusable glyphs."""
    return _STRIPPABLE.sub("", text).upper().translate(_CONFUSIONS)


#: Cosmetic grouping, 4 + 4.
_GROUP = 4


class InvalidShortId(ValueError):
    """A code that is malformed, or whose check symbol does not match."""

    def __init__(self, message: str, *, reason: str, value: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.value = value


def check_value(data: str) -> int:
    """The mod-37 residue of the 7 data symbols. See the module docstring."""
    total = 0
    for symbol in data:
        total = total * BASE + _VALUE_OF[symbol]
    return total % CHECK_MODULUS


def normalize(raw: str) -> str:
    """Canonicalise user- or scanner-supplied text to 8 bare symbols.

    Tolerates the cosmetic hyphen, surrounding whitespace, lower case, and the
    ``O``/``I``/``L`` confusions. Also tolerates a leading display prefix
    (``BIN 4K7T-92M8``) by keeping only the final whitespace-separated token —
    which is not the same as *parsing* the prefix. The prefix carries no
    meaning and is discarded, exactly as the design requires; this only stops a
    human who typed what they saw on the label from being told they are wrong.
    """
    stripped = raw.strip()
    if not stripped:
        raise InvalidShortId("empty short id", reason="empty", value=raw)

    # A display prefix is dropped by keeping the final whitespace-separated
    # token — but only when that token is itself a full-length code. Otherwise
    # the whitespace was being used as the group separator ("4K7T 92M8") and
    # the whole string is meant. Deterministic either way; nothing is guessed.
    tokens = stripped.split()
    if len(tokens) > 1 and len(_squash(tokens[-1])) == TOTAL_SYMBOLS:
        stripped = tokens[-1]
    text = _squash(stripped)

    if len(text) != TOTAL_SYMBOLS:
        raise InvalidShortId(
            f"expected {TOTAL_SYMBOLS} symbols, got {len(text)}",
            reason="length",
            value=raw,
        )
    bad = [character for character in text if character not in _VALUE_OF]
    if bad:
        raise InvalidShortId(
            f"illegal symbol(s) {''.join(sorted(set(bad)))}", reason="alphabet", value=raw
        )
    return text


def validate(raw: str) -> str:
    """Normalise and verify the check symbol. Returns the canonical 8 symbols."""
    text = normalize(raw)
    data, check = text[:DATA_SYMBOLS], text[DATA_SYMBOLS]
    expected = check_value(data)
    if expected >= BASE or ALPHABET[expected] != check:
        raise InvalidShortId("check symbol does not match", reason="check", value=raw)
    return text


def is_valid(raw: str) -> bool:
    try:
        validate(raw)
    except InvalidShortId:
        return False
    return True


def generate(randbits: Callable[[int], int] = secrets.randbits) -> str:
    """Mint a new short ID.

    Candidates whose check residue lands in 32–36 are **discarded and redrawn**
    (about 13.5% of them, 5 cases in 37). Crockford's spec would encode those
    as ``*~$=U``, and those glyphs are font-fragile and awkward to type — so the
    printed string is kept strictly inside the 32-symbol alphabet at the cost of
    a few extra draws. Rejection sampling keeps the remaining space uniform.
    """
    while True:
        number = randbits(DATA_SYMBOLS * 5)  # 5 bits per base-32 symbol
        data = _encode(number)
        residue = check_value(data)
        if residue < BASE:
            return data + ALPHABET[residue]


def _encode(number: int) -> str:
    symbols = []
    for _ in range(DATA_SYMBOLS):
        number, remainder = divmod(number, BASE)
        symbols.append(ALPHABET[remainder])
    return "".join(reversed(symbols))


def format_display(short_id: str, entity_type: str | None = None) -> str:
    """Render for a label or a screen: ``4K7T-92M8``, or ``BIN 4K7T-92M8``.

    The type prefix is **cosmetic**. It is never stored and never parsed back,
    so an object that changes type does not invalidate anything already
    printed.
    """
    text = normalize(short_id)
    grouped = f"{text[:_GROUP]}-{text[_GROUP:]}"
    if entity_type is None:
        return grouped
    return f"{_DISPLAY_PREFIXES.get(entity_type, entity_type.upper())} {grouped}"


_DISPLAY_PREFIXES: dict[str, str] = {
    EntityType.PART: "PART",
    EntityType.LOCATION: "BIN",
    EntityType.STOCK_LOT: "LOT",
    EntityType.CONTAINER_TYPE: "TYPE",
    EntityType.PART_CATEGORY: "CAT",
    EntityType.SUPPLIER_PART: "SUPP",
    EntityType.DOCUMENT: "DOC",
    EntityType.PROJECT: "PROJ",
    EntityType.DEVICE: "DEV",
}


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
