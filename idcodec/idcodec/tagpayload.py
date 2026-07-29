"""One payload, two carriers: reading a tag's NDEF URI, and folding its UID.

Both rules are needed on **both sides of the wire** — the API writes
`location_tags.tag_uid` with `normalize_tag_uid` and the station Pi compares
against it — and they must agree exactly. A UID folded by a different rule is
invisible to the binding it should match *while looking perfectly correct in
both places*, so a verification walk reports a whole cabinet as swapped. That is
why these live here, in a package with no dependencies, rather than in the API
where only the API could reach them.

**Nothing mutable ever goes on a tag.** The NDEF payload is
`{base_url}/s/{short_id}` and nothing else — no count, no fill state. A tag is a
foreign key, not a record, so parsing one is only ever "which short id is this".
"""

from __future__ import annotations

import re

from idcodec.shortid import InvalidShortId, validate

#: The UID lives in factory-locked pages, so it is fixed-alphabet hex however it
#: is presented. Separators are cosmetic — a PN532 library prints `04:1A:2B` and
#: Web NFC hands over `041a2b` — and folding them here is what stops the same
#: physical tag being recorded as two different tags by two readers.
_UID_SEPARATORS = re.compile(r"[\s:.\-_]+")
_UID = re.compile(r"\A[0-9A-F]{4,32}\Z")

#: `{base_url}/s/{short_id}`, matched **host-agnostically**. The host may
#: legitimately change (a rename, a reverse proxy, a lab hostname) while every
#: tag already written keeps the old one, and the meaning of the payload was
#: never the host — it is the short id. Refusing a tag because it names the
#: previous hostname would be refusing a tag that is perfectly correct.
_NDEF_PATH = re.compile(r"/s/(?P<code>[^/?#]+)/?\Z")

#: The `reason` an invalid UID reports. Named because it reaches an API response
#: body verbatim — `app.api.routes.provisioning` maps it to 422 — so it is a
#: wire contract, not a log string.
INVALID_TAG_UID = "invalid_tag_uid"


class InvalidTagUid(ValueError):
    """The string in a `tag_uid` field is not a tag UID.

    Carries a `reason` in the same shape as `InvalidShortId` and as the API's
    own error types, so `app.services.provisioning` can translate it into a
    `ProvisioningError` without inventing the string.
    """

    def __init__(self, message: str, *, value: str, reason: str = INVALID_TAG_UID) -> None:
        super().__init__(message)
        self.reason = reason
        self.value = value


def normalize_tag_uid(raw: str) -> str:
    """Canonicalise a UID to bare upper-case hex."""
    text = _UID_SEPARATORS.sub("", raw).upper()
    if not _UID.fullmatch(text):
        raise InvalidTagUid(f"{raw!r} is not a tag UID: expected 4-32 hex digits", value=raw)
    return text


def parse_ndef_url(url: str) -> str | None:
    """The short id carried by an NDEF URI record, or None if it carries none.

    Returns the canonical short id, check symbol verified — the payload is
    trusted no further than a scanned label is.
    """
    match = _NDEF_PATH.search(url.strip())
    if match is None:
        return None
    try:
        return validate(match.group("code"))
    except InvalidShortId:
        return None
