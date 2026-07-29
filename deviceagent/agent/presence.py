"""Tag presence: turns a stream of polls into a stream of edges.

**Half of workflow 5, and the half with no network in it.** This module answers
"is a container on the platform, and is it the same one as last poll?" — nothing
else. `agent.session` sits on top and owns the rest of PLAN.md's loop (`READY →
ACTION → CONFIRM → COMMIT`), which is where the API, the ledger and the user's
input come in. Splitting them that way keeps every timing decision here testable
without a server, and keeps every write decision there testable without a reader.

PLAN.md opens the station's loop with
`IDLE → CONTAINER_DETECTED (weight jump > ~200 mg) → IDENTIFYING`. **ADR 0003
deferred the load cell, so there is no weight jump and therefore no trigger.**
Continuous PN532 polling is the replacement, chosen because it is the only one
that preserves the property the station exists for: you set a container down and
it identifies itself, with no gesture at all. (A button or an IR proximity sensor
would each reintroduce one.)

What continuous polling costs is that presence becomes a *repeated* observation
instead of a single event, and this module is the entire answer to that:

* a settled tag produces **silence**, poll after poll. The station loops in
  `ACTION` while the container stays put, so an event per read would make the
  PWA re-fire, and re-firing a take is a spurious ledger row against a real bin.
* a removal is **debounced**. A PN532 misses reads — off-centre tags, a hand
  passing over the platform — and PLAN.md specifies that removing the container
  before `COMMIT` aborts and writes nothing. A single dropped read must
  therefore not look like a removal, or a stray flicker discards work the user
  has done.
* a swap with no gap between the two containers is torn down and rebuilt,
  never patched. Carrying an in-progress action across a change of container is
  how a ledger row lands against the wrong bin.

The machine is pure and clock-free: it is fed one observation at a time and
returns the events that observation caused. Every timing decision is expressed
as a count of polls, so tests are exact rather than sleepy.
"""

from __future__ import annotations

from enum import StrEnum

from agent import events
from agent.events import Event
from agent.identity import TagIdentity, identify
from agent.tags import TagRead


class PresenceState(StrEnum):
    """What the *reader* knows. `agent.session.SessionState` is what the station
    knows, and the two are deliberately separate machines: this one advances on
    polls, that one on answers from the API and taps from the user.

    PLAN.md's first four states, with the weight ones absent by ADR 0003.

    A `StrEnum` is safe here: the no-`sa.Enum` rule exists because SQLite cannot
    alter a `CHECK` constraint, and nothing in this process touches a column.
    """

    IDLE = "idle"
    #: Something is in the field; no carrier has read yet. PLAN.md's IDENTIFYING.
    IDENTIFYING = "identifying"
    IDENTIFIED = "identified"
    #: Present, budget spent, still unreadable. PLAN.md's UNIDENTIFIED.
    UNIDENTIFIED = "unidentified"


#: PLAN.md: "NFC poll, ~5 tries / 1.5 s". Five tries at `agent.config`'s default
#: 300 ms cadence *bound* that budget at 1.5 s — see `agent.main.poll_forever`,
#: which paces to a fixed period so the bound holds, and which says plainly that
#: whether a real reader's poll fits inside a 300 ms interval has never been
#: measured. **The budget itself is a count of polls, and that is the number this
#: module honours**; the seconds are a consequence of the cadence the loop is
#: given, and no code here reads a clock.
DEFAULT_IDENTIFY_POLLS = 5

#: Consecutive empty polls before a removal is believed — at most ~0.9 s at a
#: 300 ms cadence, with the same caveat as above.
#: The trade is symmetric and neither end is free: too low and a flickering read
#: aborts a session mid-commit; too high and the next container is on the
#: platform before the last one was released, which looks like a swap.
DEFAULT_ABSENT_POLLS = 3


class TagPresence:
    """Fold polls into edges. One instance per agent process.

    Not thread-safe and not meant to be: the poll loop is the only caller, and a
    second caller would interleave observations into a state machine whose whole
    job is the order they arrived in.
    """

    def __init__(
        self,
        *,
        identify_polls: int = DEFAULT_IDENTIFY_POLLS,
        absent_polls: int = DEFAULT_ABSENT_POLLS,
    ) -> None:
        if identify_polls < 1 or absent_polls < 1:
            raise ValueError("identify_polls and absent_polls must be at least 1")
        self._identify_polls = identify_polls
        self._absent_polls = absent_polls
        self.state = PresenceState.IDLE
        self.identity: TagIdentity | None = None
        self._present_polls = 0
        self._missed_polls = 0
        self._faulting = False

    # -- observations ------------------------------------------------------

    def observe(self, read: TagRead | None) -> list[Event]:
        """One successful poll. Returns the events it caused, often none.

        **Presence comes from `read`, meaning comes from `identify(read)`.** They
        are separate questions and conflating them is the bug this signature
        exists to prevent: `TagRead(None, None)` — a tag in the field that read
        nothing — folds to the same empty identity as no tag at all, and treating
        that as an empty platform would drop a dead-tagged container back to the
        idle screen instead of offering to provision it.
        """
        # A successful read ends a fault run, so the next fault is a fresh event
        # rather than being swallowed as a repeat of an old one.
        self._faulting = False

        if read is None:
            return self._observe_absent()
        return self._observe_present(identify(read))

    def observe_fault(self, message: str) -> list[Event]:
        """The reader raised. Emits `tag.error` once per run of faults.

        **State is left alone.** A container has not moved just because a USB
        cable did, so a fault must not synthesise a removal — that would abort a
        session the user is in the middle of. If the tag really is gone, the
        first successful poll after recovery produces the removal through the
        normal debounce, which is the same path and needs no special case.
        """
        if self._faulting:
            return []
        self._faulting = True
        return [events.tag_error(message=message)]

    # -- transitions -------------------------------------------------------

    def _observe_absent(self) -> list[Event]:
        if self.state is PresenceState.IDLE:
            # Silence while idle. At 3 polls/second an "empty" event would be
            # the overwhelming majority of the stream and would tell nobody
            # anything.
            return []

        self._missed_polls += 1
        if self._missed_polls < self._absent_polls:
            return []

        missed = self._missed_polls
        self._reset_to_idle()
        return [events.tag_removed(missed_polls=missed)]

    def _observe_present(self, identity: TagIdentity) -> list[Event]:
        # Any read at all is presence, so the removal debounce restarts. Note
        # this is *not* conditional on the read being identifiable: a partial
        # read of the tag that is still sitting there is presence.
        self._missed_polls = 0

        if identity.is_identified:
            return self._observe_identified(identity)
        return self._observe_unreadable()

    def _observe_identified(self, identity: TagIdentity) -> list[Event]:
        current = self.identity
        if (
            self.state is PresenceState.IDENTIFIED
            and current is not None
            and current.key == identity.key
        ):
            # THE case this module exists for: the container is still there.
            # Silence. Re-emitting here is what would let the PWA re-fire an
            # action several times a second against a real bin.
            #
            # Note the comparison is on `key` (UID-first), not on the whole
            # identity: a poll that reads the UID but misses user memory is the
            # same tag, and must not read as a swap.
            #
            # The stored identity is left as first announced, even when this poll
            # knows *more* (a UID-only placement whose NDEF reads on the third
            # try). Re-announcing to upgrade it would break the one-event-per-
            # placement promise for a gain of nothing: the client resolves
            # through `/api/location-tags/resolve`, which finds the binding from
            # the UID alone.
            return []

        emitted: list[Event] = []
        if self.state is PresenceState.IDENTIFIED:
            # A *different* tag with no empty poll in between: someone lifted one
            # container and set the next down inside the debounce window. Tear
            # the old session down explicitly before announcing the new one — a
            # client that only saw the second `tag.identified` could reasonably
            # apply input the user had already entered against the first.
            #
            # `missed_polls: 0` is how a client tells this apart from a normal
            # departure: nothing was ever missing, the answer just changed.
            emitted.append(events.tag_removed(missed_polls=self._missed_polls))
        # IDENTIFYING → IDENTIFIED is the ordinary success path and emits no
        # removal, and neither does UNIDENTIFIED → IDENTIFIED (a re-seat that
        # finally read): in both, nothing had been announced that a client could
        # have collected input against.

        self.state = PresenceState.IDENTIFIED
        self.identity = identity
        self._present_polls = 1
        emitted.append(events.tag_identified(identity))
        return emitted

    def _observe_unreadable(self) -> list[Event]:
        if self.state is PresenceState.UNIDENTIFIED:
            # Already told the client the budget was spent. Repeating
            # `tag.timeout` every poll for as long as the container sits there
            # would bury the event that ends the state.
            return []

        if self.state is PresenceState.IDENTIFIED:
            # A tag we already resolved, and this poll simply read nothing off
            # it. Presence is unchanged and the identity we hold is still the
            # best information available, so nothing has happened. Downgrading
            # to IDENTIFYING here would re-announce the container on the next
            # good read.
            return []

        if self.state is PresenceState.IDLE:
            self.state = PresenceState.IDENTIFYING
            self.identity = None
            self._present_polls = 0

        # Deliberately falls through to the budget check rather than emitting a
        # `tag.reading` for the first poll unconditionally: with
        # `identify_polls=1` the first poll *is* the whole budget, and reporting
        # "reading 1 of 1" and then never resolving would leave the client
        # waiting for an event that cannot arrive.
        self._present_polls += 1
        if self._present_polls < self._identify_polls:
            return [events.tag_reading(poll=self._present_polls, of=self._identify_polls)]

        self.state = PresenceState.UNIDENTIFIED
        return [events.tag_timeout(polls=self._present_polls)]

    def _reset_to_idle(self) -> None:
        self.state = PresenceState.IDLE
        self.identity = None
        self._present_polls = 0
        self._missed_polls = 0
