"""NDEF-first with a UID fallback, and the two rules that must not be re-derived.

The reason these tests exist at this level rather than only through the station:
the failure they guard against is silent. A UID folded by a rule of the agent's
own would be stored in an event, posted to `/api/location-tags/resolve`, and
matched against nothing — reporting an unprovisioned drawer for a drawer that is
provisioned, with every intermediate value looking correct.
"""

from __future__ import annotations

import pytest
from app.services import provisioning, shortid

from agent.identity import NO_TAG, VIA_NDEF, VIA_UID, identify
from agent.tags import TagRead


def test_ndef_wins_when_both_carriers_read() -> None:
    code = shortid.generate()
    identity = identify(
        TagRead(uid="04:1a:2b:3c:4d:5e:6f", ndef_url=f"https://almagest.lan/s/{code}")
    )
    assert identity.via == VIA_NDEF
    assert identity.short_id == code
    # Still reported. The PWA posts both carriers so the *server* can say they
    # disagree; an agent that dropped the UID here would hide a mis-bound tag.
    assert identity.tag_uid == "041A2B3C4D5E6F"


def test_a_uid_only_tag_is_identified_by_its_uid() -> None:
    """A tag whose NDEF was never written, or whose write was interrupted. Not an
    error: its binding lives in `location_tags` and resolves server-side."""
    identity = identify(TagRead(uid="04AABBCCDDEE10", ndef_url=None))
    assert identity.via == VIA_UID
    assert identity.short_id is None
    assert identity.tag_uid == "04AABBCCDDEE10"


def test_the_uid_is_folded_by_the_backends_rule_not_a_local_one() -> None:
    """The property that matters is agreement, so assert against the rule itself
    rather than against a string typed out here."""
    raw = "04:1A:2B:3C:4D:5E:6F"
    assert identify(TagRead(uid=raw, ndef_url=None)).tag_uid == provisioning.normalize_tag_uid(raw)


def test_a_foreign_ndef_record_falls_back_to_the_uid() -> None:
    """A hotel key card or a transit pass. The URL is still reported — "that is
    not one of ours" is a better message than "unreadable"."""
    identity = identify(TagRead(uid="0455555555555555", ndef_url="https://example.invalid/x/1"))
    assert identity.via == VIA_UID
    assert identity.short_id is None
    assert identity.ndef_url == "https://example.invalid/x/1"


def test_a_bad_check_symbol_is_not_trusted_and_falls_back() -> None:
    """`parse_ndef_url` verifies the mod-37 check symbol, which is the whole
    reason it is imported rather than re-written as a regex here."""
    code = shortid.generate()
    corrupted = code[:-1] + ("0" if code[-1] != "0" else "1")
    identity = identify(TagRead(uid="04AABBCCDDEE10", ndef_url=f"https://x/s/{corrupted}"))
    assert identity.short_id is None
    assert identity.via == VIA_UID


def test_a_malformed_uid_does_not_discard_a_good_ndef_read() -> None:
    """The weaker carrier must not veto the stronger one."""
    code = shortid.generate()
    identity = identify(TagRead(uid="not-a-uid", ndef_url=f"https://x/s/{code}"))
    assert identity.short_id == code
    assert identity.tag_uid is None
    assert identity.via == VIA_NDEF


@pytest.mark.parametrize("uid", ["", "zz", "04G1", "0" * 33])
def test_a_malformed_uid_alone_is_not_an_identity(uid: str) -> None:
    identity = identify(TagRead(uid=uid, ndef_url=None))
    assert not identity.is_identified


def test_an_empty_field_and_an_unreadable_tag_fold_alike_but_arrive_differently() -> None:
    """`identify` cannot tell them apart and must not pretend to: presence is
    `read is None`, which is the station's business, not this function's."""
    assert identify(None) is NO_TAG
    assert not identify(TagRead(uid=None, ndef_url=None)).is_identified


def test_sameness_is_keyed_on_the_uid_and_meaning_on_the_ndef() -> None:
    """The inversion that keeps a flaky user-memory read from looking like a swap:
    one poll reads both carriers, the next reads only the UID, and the station must
    see one tag."""
    code = shortid.generate()
    both = identify(TagRead(uid="041A2B3C4D5E6F", ndef_url=f"https://almagest.lan/s/{code}"))
    uid_only = identify(TagRead(uid="04:1a:2b:3c:4d:5e:6f", ndef_url=None))
    assert both.key == uid_only.key == "041A2B3C4D5E6F"
    assert both.short_id == code and uid_only.short_id is None


def test_a_tag_with_neither_carrier_has_no_key() -> None:
    assert identify(TagRead(uid=None, ndef_url=None)).key is None
