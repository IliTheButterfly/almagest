"""Normalisation of scanned text. Every lookup key in the chain is made here.

Two normalisers, deliberately different, because they answer different
questions. `normalize_code` produces the `barcode_aliases.code_norm` lookup key
and has exactly one hard requirement: **bind time and resolve time must agree**,
or a taught binding never fires again. `normalize_mpn` produces the
`parts.mpn_norm` key and may be more aggressive, because an MPN printed with
hyphens, spaces or slashes is the same part number either way.

Nothing normalised here is ever stored as the record of what was scanned —
`scan_events.raw_payload` keeps the bytes verbatim — so both functions are free
to discard information.
"""

from __future__ import annotations

import re

#: Deliberately **not** `\s`. Python treats the C1 separators as whitespace
#: (`"\x1d".isspace()` is `True`), so a `\s`-based squash would silently eat the
#: GS/RS bytes that *are* an ECIA payload's field structure — the exact lossy
#: step `scan_events` exists to avoid, applied to the alias key instead. Listing
#: the ASCII whitespace characters explicitly keeps every control byte intact,
#: so a whole-payload alias stays faithful to the label it was taught from.
#: `-`, `_` and `.` go because they are how humans and label printers decorate a
#: code, matching `services.shortid`'s treatment of the cosmetic hyphen.
_COSMETIC = re.compile(r"[ \t\r\n\f\v\-_.]+")

#: Only ASCII alphanumerics survive in an MPN key. A part number is drawn from
#: that alphabet in every catalogue that exists, so anything else is decoration
#: (`ECA-1EM101` vs `ECA1EM101`) or an encoding artefact.
_NOT_MPN = re.compile(r"[^0-9a-z]+")

#: `str.isdigit()` is true for superscripts and non-Latin digits, which is how a
#: unicode payload sneaks into a numeric code path. GTIN digits are ASCII.
_ASCII_DIGITS = re.compile(r"^[0-9]+$")

#: The path component of the one payload written to every tag and QR,
#: `{base_url}/s/{short_id}`. Matched case-insensitively because a hand-typed or
#: reader-uppercased URL is still that URL.
_SHORT_ID_PATH = re.compile(r"/s/", re.IGNORECASE)

_URL_TERMINATORS = ("?", "#", "/")

#: GTIN lengths in circulation: EAN-8, UPC-A, EAN-13, ITF-14.
_GTIN_LENGTHS = frozenset({8, 12, 13, 14})


def normalize_code(raw: str) -> str:
    """The `barcode_aliases.code_norm` key for a scanned payload."""
    return _COSMETIC.sub("", raw).casefold()


def normalize_mpn(raw: str) -> str:
    """The `parts.mpn_norm` key for a manufacturer part number.

    **The single definition of that column's contents.** Anything that writes
    `mpn_norm` — the demo seed today, the intake path later — must call this,
    because a value written by a different rule is invisible to the resolver's
    bare-MPN step while still looking perfectly correct in the row.
    """
    return _NOT_MPN.sub("", raw.casefold())


def short_id_candidate(payload: str) -> str:
    """The part of `payload` that might be a short ID.

    Handles the tag and QR form, `https://<host>/s/4K7T-92MQ`, as well as a bare
    or hand-typed code. The **host is deliberately ignored**: the payload's
    authority is the opaque id, and matching against the configured base URL
    would strand every tag written before a hostname change — the one change
    this design cannot make cheaply, since it is physically stamped into tags.
    """
    matches = list(_SHORT_ID_PATH.finditer(payload))
    if not matches:
        return payload.strip()

    tail = payload[matches[-1].end() :]
    for terminator in _URL_TERMINATORS:
        tail = tail.partition(terminator)[0]
    return tail.strip()


def is_gtin(digits: str) -> bool:
    """Whether `digits` is a GTIN-8/12/13/14 with a correct check digit.

    The check digit is what makes this a *classification* rather than a guess:
    an arbitrary run of 13 digits passes only 1 time in 10, so claiming a
    payload as a retail barcode on the strength of it is defensible, and a near
    miss falls through to `unknown` where the user can bind it by hand.
    """
    if len(digits) not in _GTIN_LENGTHS or not _ASCII_DIGITS.match(digits):
        return False

    body, check = digits[:-1], int(digits[-1])
    # Weights alternate 3,1,3,1... reading right-to-left from the digit
    # immediately left of the check digit, for every GTIN length.
    total = sum(int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(body[::-1]))
    return (10 - total % 10) % 10 == check
