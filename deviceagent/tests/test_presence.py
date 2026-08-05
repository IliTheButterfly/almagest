"""Tag presence: the machine that turns polls into edges, script-driven then by hand.

The headline case is `test_a_container_that_stays_put_produces_silence`. The
station loops in `ACTION` for as long as the container is on the platform, so a
poller that re-fired on every read would hand the PWA a fresh "identified" event
several times a second — and a take confirmed against each of them is a stack of
spurious ledger rows against a real bin. Silence is the feature.
"""

from __future__ import annotations

import pytest
from idcodec import shortid

from agent import events
from agent.events import Event
from agent.fake_tags import FakeTagSource, ScriptedPoll
from agent.presence import PresenceState, TagPresence
from agent.tags import TagRead, TagSourceError

CODE_A = "4K7T92M8"
CODE_B = "NX6C5ZTQ"
UID_A = "041A2B3C4D5E6F"
UID_B = "04AABBCCDDEE10"
UID_C = "0499887766554433"
UID_D = "0455555555555555"

TAG_A = TagRead(uid="04:1A:2B:3C:4D:5E:6F", ndef_url=f"https://almagest.aether.lan/s/{CODE_A}")
UNREADABLE = TagRead(uid=None, ndef_url=None)


def drain(presence: TagPresence, source: FakeTagSource) -> list[Event]:
    """Replay a whole script through the machine, exactly as `poll_forever` does."""
    emitted: list[Event] = []
    while not source.exhausted:
        try:
            read = source.poll()
        except TagSourceError as error:
            emitted.extend(presence.observe_fault(str(error)))
        else:
            emitted.extend(presence.observe(read))
    return emitted


def shapes(emitted: list[Event]) -> list[tuple[str, dict[str, object]]]:
    return [(event.type, event.data) for event in emitted]


# ---------------------------------------------------------------------------
# The whole scripted session, asserted event for event
# ---------------------------------------------------------------------------


def test_the_packaged_script_produces_exactly_this_stream(
    presence: TagPresence, source: FakeTagSource
) -> None:
    """The one test that would catch a re-recorded fixture changing meaning.

    Written as the literal expected stream rather than as properties, because the
    thing being asserted *is* the sequence: which polls cause an event and which
    cause silence is the entire contract of this module.
    """
    assert shapes(drain(presence, source)) == [
        # A drawer lands. Both carriers read, so NDEF wins and the UID rides along.
        (
            events.TAG_IDENTIFIED,
            {"short_id": CODE_A, "tag_uid": UID_A, "ndef_url": TAG_A.ndef_url, "via": "ndef"},
        ),
        # Four more polls of the same tag — one of them a UID-only read — and a
        # dropped read in the middle. All silence.
        (events.TAG_REMOVED, {"missed_polls": 3}),
        # An unreadable tag: four "still reading" beats, then the budget.
        (events.TAG_READING, {"poll": 1, "of": 5}),
        (events.TAG_READING, {"poll": 2, "of": 5}),
        (events.TAG_READING, {"poll": 3, "of": 5}),
        (events.TAG_READING, {"poll": 4, "of": 5}),
        (events.TAG_TIMEOUT, {"polls": 5}),
        (events.TAG_REMOVED, {"missed_polls": 3}),
        # Two consecutive reader faults, one event.
        (events.TAG_ERROR, {"message": "PN532 did not ACK: SAMConfig timed out on /dev/ttyAMA0"}),
        # A tag whose NDEF was never written.
        (
            events.TAG_IDENTIFIED,
            {"short_id": None, "tag_uid": UID_B, "ndef_url": None, "via": "uid"},
        ),
        # Swapped for another drawer with no empty poll between: teardown, then
        # the new one. `missed_polls: 0` is what marks it as a swap.
        (events.TAG_REMOVED, {"missed_polls": 0}),
        (
            events.TAG_IDENTIFIED,
            {
                "short_id": CODE_B,
                "tag_uid": UID_C,
                "ndef_url": f"https://almagest.aether.lan/s/{CODE_B}",
                "via": "ndef",
            },
        ),
        # A foreign card: identified by UID, its payload reported verbatim.
        (events.TAG_REMOVED, {"missed_polls": 0}),
        (
            events.TAG_IDENTIFIED,
            {
                "short_id": None,
                "tag_uid": UID_D,
                "ndef_url": "https://example.invalid/loyalty/1234",
                "via": "uid",
            },
        ),
        (events.TAG_REMOVED, {"missed_polls": 3}),
    ]
    assert presence.state is PresenceState.IDLE


def test_the_script_still_covers_every_situation_it_was_written_for(
    script: tuple[ScriptedPoll, ...],
) -> None:
    """Guards the fixture itself, which is the more likely thing to be trimmed.

    Re-recording it against a real reader is expected; losing one of these
    situations while doing so would quietly delete a test.
    """
    reads = [poll.read for poll in script]
    assert None in reads, "no empty-field poll"
    assert any(r is not None and r.uid and r.ndef_url for r in reads), "no fully-readable tag"
    assert any(r is not None and r.uid and not r.ndef_url for r in reads), "no UID-only tag"
    assert any(r is not None and not r.uid and not r.ndef_url for r in reads), "no unreadable tag"
    assert any(poll.error for poll in script), "no reader fault"
    uids = {r.uid for r in reads if r is not None and r.uid}
    assert len(uids) >= 2, "no second tag, so no swap and no re-identification"


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def test_a_container_that_stays_put_produces_silence(presence: TagPresence) -> None:
    """The station loops while the tag stays present. One announcement, then
    nothing — a re-fire here is a duplicate ledger row at the far end."""
    assert len(presence.observe(TAG_A)) == 1
    for _ in range(50):
        assert presence.observe(TAG_A) == []
    assert presence.state is PresenceState.IDENTIFIED


def test_a_uid_only_read_of_the_same_tag_is_not_a_swap(presence: TagPresence) -> None:
    """User memory is read separately from the anticollision UID, so a poll can
    lose the NDEF record without the tag having moved."""
    presence.observe(TAG_A)
    assert presence.observe(TagRead(uid=UID_A, ndef_url=None)) == []
    assert presence.identity is not None
    # The richer identity is kept: the placement was already announced with a
    # short id, and re-announcing it without one would be a downgrade.
    assert presence.identity.short_id == CODE_A


def test_a_single_dropped_read_is_not_a_removal(presence: TagPresence) -> None:
    """A hand passing over the platform, or an off-centre tag. Removing the
    container before COMMIT aborts and writes nothing, so a false removal
    discards work the user has done."""
    presence.observe(TAG_A)
    assert presence.observe(None) == []
    assert presence.observe(None) == []
    assert presence.state is PresenceState.IDENTIFIED
    assert presence.observe(TAG_A) == []


def test_the_debounce_restarts_after_a_recovered_read(presence: TagPresence) -> None:
    """Two missed polls, a good one, then two more must still not be a removal —
    otherwise a marginal tag drips towards a spurious abort."""
    presence.observe(TAG_A)
    presence.observe(None)
    presence.observe(None)
    presence.observe(TAG_A)
    assert presence.observe(None) == []
    assert presence.observe(None) == []
    assert presence.state is PresenceState.IDENTIFIED


def test_removal_is_announced_once_the_debounce_is_satisfied(presence: TagPresence) -> None:
    presence.observe(TAG_A)
    presence.observe(None)
    presence.observe(None)
    assert shapes(presence.observe(None)) == [(events.TAG_REMOVED, {"missed_polls": 3})]
    assert presence.state is PresenceState.IDLE
    assert presence.identity is None
    # And an empty bench stays quiet for ever afterwards.
    for _ in range(10):
        assert presence.observe(None) == []


def test_an_unreadable_tag_in_the_field_is_not_an_empty_platform(presence: TagPresence) -> None:
    """The distinction the three-valued `poll()` exists for: this must offer
    "provision this container", not the idle screen."""
    presence.observe(UNREADABLE)
    assert presence.state is PresenceState.IDENTIFYING


# ---------------------------------------------------------------------------
# Identification budget
# ---------------------------------------------------------------------------


def test_the_identify_budget_ends_in_a_timeout_then_silence(presence: TagPresence) -> None:
    emitted: list[Event] = []
    for _ in range(5):
        emitted.extend(presence.observe(UNREADABLE))
    assert shapes(emitted)[-1] == (events.TAG_TIMEOUT, {"polls": 5})
    assert presence.state is PresenceState.UNIDENTIFIED
    for _ in range(20):
        assert presence.observe(UNREADABLE) == []


def test_a_reseated_tag_that_finally_reads_is_identified_without_a_teardown(
    presence: TagPresence,
) -> None:
    """Nothing had been announced that a client could have collected input
    against, so there is nothing to tear down first."""
    for _ in range(5):
        presence.observe(UNREADABLE)
    assert shapes(presence.observe(TAG_A)) == [
        (
            events.TAG_IDENTIFIED,
            {"short_id": CODE_A, "tag_uid": UID_A, "ndef_url": TAG_A.ndef_url, "via": "ndef"},
        )
    ]


def test_resolving_during_the_budget_emits_no_removal(presence: TagPresence) -> None:
    presence.observe(UNREADABLE)
    presence.observe(UNREADABLE)
    assert [event.type for event in presence.observe(TAG_A)] == [events.TAG_IDENTIFIED]


def test_a_short_budget_is_honoured() -> None:
    """The budget is a count of polls, not a duration, so it is exact in a test
    and derived from the cadence in production."""
    presence = TagPresence(identify_polls=1, absent_polls=1)
    assert [event.type for event in presence.observe(UNREADABLE)] == [events.TAG_TIMEOUT]
    assert [event.type for event in presence.observe(None)] == [events.TAG_REMOVED]


@pytest.mark.parametrize(("identify_polls", "absent_polls"), [(0, 3), (5, 0), (-1, -1)])
def test_a_budget_of_zero_polls_is_refused(identify_polls: int, absent_polls: int) -> None:
    """Zero would mean "decide before looking", which is not a configuration, it
    is a bug that would present as tags never identifying."""
    with pytest.raises(ValueError, match="at least 1"):
        TagPresence(identify_polls=identify_polls, absent_polls=absent_polls)


# ---------------------------------------------------------------------------
# Swaps and faults
# ---------------------------------------------------------------------------


def test_a_swap_tears_the_old_session_down_before_announcing_the_new_one(
    presence: TagPresence,
) -> None:
    """Two containers, no gap. A client that saw only the second `tag.identified`
    could reasonably apply input the user entered against the first."""
    presence.observe(TAG_A)
    emitted = presence.observe(
        TagRead(uid=UID_C, ndef_url=f"https://almagest.aether.lan/s/{CODE_B}")
    )
    assert [event.type for event in emitted] == [events.TAG_REMOVED, events.TAG_IDENTIFIED]
    assert emitted[0].data["missed_polls"] == 0


def test_two_tags_that_differ_only_in_their_ndef_are_still_a_swap(
    presence: TagPresence,
) -> None:
    """Belt and braces on the key: a UID-only tag followed by a *different*
    UID-only tag has no short id to compare, so the UID has to carry it."""
    presence.observe(TagRead(uid=UID_B, ndef_url=None))
    emitted = presence.observe(TagRead(uid=UID_C, ndef_url=None))
    assert [event.type for event in emitted] == [events.TAG_REMOVED, events.TAG_IDENTIFIED]


def test_a_run_of_faults_is_one_event(presence: TagPresence) -> None:
    assert len(presence.observe_fault("port gone")) == 1
    for _ in range(10):
        assert presence.observe_fault("port gone") == []


def test_a_fault_does_not_synthesise_a_removal(presence: TagPresence) -> None:
    """The container has not moved because a USB cable did. Aborting the user's
    session on a transport blip would lose their work for no reason."""
    presence.observe(TAG_A)
    assert [event.type for event in presence.observe_fault("port gone")] == [events.TAG_ERROR]
    assert presence.state is PresenceState.IDENTIFIED
    assert presence.observe(TAG_A) == []


def test_a_removal_during_a_fault_run_still_arrives_through_the_normal_debounce(
    presence: TagPresence,
) -> None:
    presence.observe(TAG_A)
    presence.observe_fault("port gone")
    for _ in range(2):
        assert presence.observe(None) == []
    assert [event.type for event in presence.observe(None)] == [events.TAG_REMOVED]


def test_a_recovered_reader_can_fault_again_and_be_heard(presence: TagPresence) -> None:
    """Deduplication is per *run*. Collapsing every fault for the lifetime of the
    process would hide a reader that is failing intermittently, which is the
    failure a marginal cable actually produces."""
    presence.observe_fault("port gone")
    presence.observe(None)
    assert len(presence.observe_fault("port gone")) == 1


def test_the_short_ids_in_this_file_are_real() -> None:
    """These strings are compared against parser output, so a typo would look
    like a parser bug."""
    assert shortid.is_valid(CODE_A) and shortid.is_valid(CODE_B)
