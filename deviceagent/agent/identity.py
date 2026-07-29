"""NDEF-first resolution with a UID fallback, done once and locally.

Both rules come from `idcodec.tagpayload`, imported rather than reimplemented.
That is not tidiness: `normalize_tag_uid` produces the key that
`location_tags.tag_uid` is written with, so a UID folded by a *different* rule
here is invisible to the binding it should match while looking perfectly correct
in the event payload — and `parse_ndef_url` verifies the mod-37 check symbol, so
a second copy that skipped it would forward a mis-read short id as fact.

`idcodec` is the same code the API runs, and it is standard-library-only, so
sharing it costs this process nothing: importing it does not put FastAPI or
SQLAlchemy on the Pi. The API reaches the same two through
`app.services.provisioning`, which re-exports `parse_ndef_url` verbatim and wraps
`normalize_tag_uid` only to turn its `InvalidTagUid` into the `ProvisioningError`
its routes answer with — the folding rule either side applies is this one.

**Why the agent parses at all, given the backend already has
`POST /api/location-tags/resolve`.** Two reasons, both narrow:

1. The kiosk should react the instant a container lands, and the local parse is
   what lets it render "reading 4K7T-92M8…" before the round trip returns.
2. The station needs to know whether two consecutive polls are the *same tag*,
   which is a local question the server has no business being asked 5×/second.

**What the agent must not do is decide.** The authoritative answer comes from
that route, which is given *both carriers verbatim* and reports
`disagreement=true` when a tag's payload names one slot and its UID is bound to
another. Preferring either carrier here would hide exactly the mis-binding the
verification walk exists to find — so `TagIdentity` carries both, always, and
`via` records which carrier produced the local short id rather than claiming
anything was resolved. `via` is deliberately not called `matched_by`: the
backend's `matched_by` means "which carrier matched a row in the database", and
nothing here has looked at a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from idcodec.tagpayload import InvalidTagUid, normalize_tag_uid, parse_ndef_url

from agent.tags import TagRead

#: The vocabulary is shared with the backend's `matched_by` on purpose: two words
#: for the same carrier in one protocol is how a client ends up with a branch for
#: each spelling.
VIA_NDEF: Final = "ndef"
VIA_UID: Final = "uid"


@dataclass(frozen=True, slots=True)
class TagIdentity:
    """What one poll established about the tag in the field."""

    #: Parsed out of the NDEF URI, check symbol verified. `None` on a UID-only
    #: tag — which is not an error state: it is a tag whose binding lives in
    #: `location_tags` and resolves server-side.
    short_id: str | None

    #: Normalised to bare upper-case hex by the shared `idcodec` rule — the same
    #: one the API writes with — so it is directly comparable with
    #: `location_tags.tag_uid`. `None` if the reader
    #: gave no UID, or gave something that is not one.
    tag_uid: str | None

    #: Verbatim, host included, because that is what the resolve route wants and
    #: because a tag written before a hostname change is still perfectly correct.
    ndef_url: str | None

    #: `"ndef"`, `"uid"`, or `None` when neither carrier yielded anything usable.
    via: str | None

    @property
    def is_identified(self) -> bool:
        return self.via is not None

    @property
    def key(self) -> str | None:
        """A stable handle for "is this the same physical tag as last poll?".

        **UID first here, which is the reverse of the resolution order above, and
        the inversion is the point.** Meaning comes from the NDEF record because
        that is the payload this system authored. Sameness comes from the UID
        because it lives in factory-locked pages that cannot be half-written: a
        tag can lose its NDEF to an interrupted write, but a tag that answers at
        all answers with its UID. Keying on the short id first would make one
        flaky user-memory read look like a container swap, and a swap tears the
        session down.

        `None` only for a tag that produced neither carrier, which the station
        never treats as identified anyway.
        """
        return self.tag_uid or self.short_id


#: A poll that saw nothing. Shared instance so `identify(None) is NO_TAG` holds.
NO_TAG: Final = TagIdentity(short_id=None, tag_uid=None, ndef_url=None, via=None)


def identify(read: TagRead | None) -> TagIdentity:
    """Fold one raw read into an identity. Pure, and never raises.

    A malformed UID is dropped rather than propagated: `normalize_tag_uid`
    refuses anything that is not 4-32 hex digits, and a reader emitting that is
    faulty. Dropping it keeps a *good* NDEF read usable, where letting the
    exception out would discard the whole poll over the weaker carrier.
    """
    if read is None:
        return NO_TAG

    short_id = parse_ndef_url(read.ndef_url) if read.ndef_url else None

    tag_uid: str | None = None
    if read.uid:
        try:
            tag_uid = normalize_tag_uid(read.uid)
        except InvalidTagUid:
            tag_uid = None

    via: str | None = None
    if short_id is not None:
        via = VIA_NDEF
    elif tag_uid is not None:
        via = VIA_UID

    return TagIdentity(
        short_id=short_id,
        tag_uid=tag_uid,
        # Kept even when it did not parse. A payload that is not ours is
        # evidence — "you tapped a hotel key card" is a better message than
        # "unreadable", and the resolve route sees the same bytes we did.
        ndef_url=read.ndef_url,
        via=via,
    )
