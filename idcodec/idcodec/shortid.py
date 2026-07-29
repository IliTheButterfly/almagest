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

This module is the **codec only** — the half that is arithmetic on a string.
Binding a code to a row (``allocate``, ``adopt``, ``resolve``) needs a database
session and lives in ``app.services.shortid``, which re-exports everything here
so a caller sees one namespace.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable

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

    `entity_type` is a plain `str`, not `app.models.enums.EntityType`: this
    package holds no database vocabulary, and the prefix is a display string
    either way. `EntityType` is a `StrEnum`, so passing a member works
    unchanged. An unknown type upper-cases rather than raising — a new kind of
    object must not break a label.
    """
    text = normalize(short_id)
    grouped = f"{text[:_GROUP]}-{text[_GROUP:]}"
    if entity_type is None:
        return grouped
    return f"{DISPLAY_PREFIXES.get(entity_type, entity_type.upper())} {grouped}"


#: Keyed by `app.models.enums.EntityType`'s *values*, spelled out because this
#: package must not import the enum. `backend/tests/unit/test_shortid_display.py`
#: asserts every member has an entry here, so adding one to the enum without
#: adding it here fails there rather than shipping a bare `STOCK_LOT` on a label.
DISPLAY_PREFIXES: dict[str, str] = {
    "part": "PART",
    "location": "BIN",
    "stock_lot": "LOT",
    "container_type": "TYPE",
    "part_category": "CAT",
    "supplier_part": "SUPP",
    "document": "DOC",
    "project": "PROJ",
    "device": "DEV",
}
