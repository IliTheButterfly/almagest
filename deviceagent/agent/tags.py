"""`TagSource` — what a tag reader is, reduced to the one operation needed.

The sibling of PLAN.md's `WeightSource`, and deliberately *not* a `ScanSource`.
A `ScanSource` hands the resolver a discrete payload that a human aimed at
something; a tag reader is polled, sees the same tag hundreds of times in a row,
and needs stateful filtering before any of it means anything. That filtering is
`agent.presence`, and it is the whole reason this protocol is as dumb as it is.

**Why the protocol is synchronous.** A PN532 read is a blocking UART
transaction with a timeout; the caller runs it in a thread (`asyncio.to_thread`).
Making `poll()` a coroutine would push `asyncio` into the fake and into every
test for no gain, and would not make the underlying read any less blocking.

**Why `poll()` is three-valued.** `None`, `TagRead(None, None)` and a populated
`TagRead` are three different facts about the world:

* `None` — the field is empty. Nothing is on the platform.
* `TagRead(uid=None, ndef_url=None)` — something *is* in the field but neither
  carrier came back: a dead tag, a tag too far off-centre, a foreign card.
* populated — at least one carrier read.

Collapsing the first two would erase the difference between "the bench is
clear" (show the idle screen) and "this container's tag is unreadable" (offer to
provision it), which are opposite answers to opposite questions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable


class TagSourceError(RuntimeError):
    """The *reader* failed, as distinct from the tag being unreadable.

    A wedged PN532, a serial port that vanished, a cable pulled. The station
    treats this differently from an unreadable tag on purpose: an unreadable tag
    is a thing the user can fix by re-seating a drawer, and a broken reader is
    not, so telling them to re-seat the drawer would be a lie.
    """


@dataclass(frozen=True, slots=True)
class TagRead:
    """One poll in which the reader saw *something* in its field.

    Both fields are verbatim as the reader produced them. Normalisation happens
    once, in `agent.identity`, against the backend's rules — a reader-specific
    hex rendering must not leak past this boundary.
    """

    #: The anticollision UID, in whatever separator style the reader library
    #: uses. `None` when the tag answered but its UID could not be read, which
    #: in practice means the read failed at a lower level than it reported.
    uid: str | None

    #: The NDEF URI record, verbatim, host included. `None` when the tag carries
    #: no NDEF record (blank, factory-fresh) or user memory could not be read.
    #: The UID lives in factory-locked pages 0-2 and the NDEF in user memory at
    #: page 4, so a write interrupted halfway degrades a tag to exactly this
    #: state rather than destroying it.
    ndef_url: str | None


@dataclass(frozen=True, slots=True)
class TagCapabilities:
    """What one reader can do. Not "supported"; a list of steps it can perform.

    ADR 0012: *"a reader is a capability set, never a supported/unsupported
    flag"*, and its table is a capability matrix — Web NFC reads both carriers
    and writes, a USB wedge produces only a short id and cannot write, a hand-
    typed UID is evidence about neither carrier. A walk that branched on "is
    this reader supported" would have to answer the same question again at every
    step; a walk that reads these three booleans answers it once per step, which
    is the only place the answer differs.

    **Why this does not contradict ADR 0003.** `station.hello` still enumerates
    nothing, because the scale rule is about affordances *derived from a stream*
    — no `weight.*` arrives, so no by-weight affordance is drawn, no flag needed.
    Writing is not derived from a stream. It is a command the client issues
    against a named device, and no history of read events can tell a PN532 that
    writes from a Flipper that does not, nor say which of two attached readers
    the user should hold the tag against. See ADR 0013.
    """

    #: Produces an anticollision UID. False for a keyboard wedge.
    reads_uid: bool

    #: Looks at user memory at all, so a `None` URI from it means "blank" rather
    #: than "never checked". The browser-side twin is `TagPresentation.carriesNdef`.
    reads_ndef: bool

    #: Can put a URI record on a tag it is holding. The only interesting one.
    writes_ndef: bool

    def as_data(self) -> dict[str, bool]:
        return {
            "reads_uid": self.reads_uid,
            "reads_ndef": self.reads_ndef,
            "writes_ndef": self.writes_ndef,
        }


#: Both carriers, read-only — the PN532 as it stands, and a Flipper in bridge
#: mode until Antlia's write path lands.
READS_BOTH: Final = TagCapabilities(reads_uid=True, reads_ndef=True, writes_ndef=False)

#: Both carriers and a write. What a provisioning walk needs end to end.
READS_BOTH_AND_WRITES: Final = TagCapabilities(
    reads_uid=True, reads_ndef=True, writes_ndef=True
)


@dataclass(frozen=True, slots=True)
class TagWrite:
    """The outcome of a write: what the tag read back as, and nothing else.

    **There is deliberately no `verified` boolean here.** ADR 0012 refuses a
    client-computed one and makes `POST /api/location-tags/{id}/write-result`
    take the read-back URI, compared server-side by short id rather than by
    string — so that a payload written against a differently-configured base URL
    is caught instead of silently agreeing with itself. The bridge is a client.
    It writes, reads back through the same reader, and reports the string.

    `read_back_url is None` is ADR 0012's `degraded`: the write did not take, or
    took partially. It is **not** a lost tag — the 7-byte UID lives in factory-
    locked pages 0-2, physically separate from user memory at page 4, so the tag
    still identifies itself perfectly and the answer is "offer a rewrite".
    """

    #: Verbatim as read back after the write, host included. `None` when nothing
    #: readable came back.
    read_back_url: str | None

    def as_data(self) -> dict[str, Any]:
        return {"read_back_url": self.read_back_url}


class TagWriteRefused(Exception):
    """The write did not happen, and the reader is fine.

    Distinct from `TagSourceError` for the same reason an unreadable tag is:
    these are facts about what is on the platform, and every one of them has a
    user-facing answer that is not "your reader is broken".

    A write is a discrete command rather than a poll, so unlike `TagSource.poll`
    this *is* an exception — the caller has one thing to do next and it must not
    be "carry on as though the tag were written".
    """

    #: One of `no_tag`, `not_blank`, `too_long`, `unsupported`, `read_back_failed`.
    #: Forwarded verbatim as `tag.write.failed`'s `reason`, so it is a closed
    #: vocabulary a client may branch on — unlike `message`, which is prose.
    reason: str

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


#: Refusing to overwrite is the default everywhere in Almagest — `nfc.ts` passes
#: `overwrite: false` to Web NFC for the same reason. A tag that already carries
#: a URI is very likely bound to a different container, and the cost of the two
#: mistakes is not symmetrical: refusing costs a toggle, overwriting costs a
#: drawer that answers to a short id belonging to another drawer.
NOT_BLANK: Final = "not_blank"
NO_TAG: Final = "no_tag"
TOO_LONG: Final = "too_long"
UNSUPPORTED: Final = "unsupported"
READ_BACK_FAILED: Final = "read_back_failed"


@runtime_checkable
class TagSource(Protocol):
    """A pollable NFC reader.

    Implementations must not raise for an empty field or an unreadable tag —
    those are return values. `TagSourceError` is reserved for the reader itself.
    """

    @property
    def capabilities(self) -> TagCapabilities:
        """What this reader can do. Constant for the lifetime of the object.

        Constant because a reader that gained or lost an ability mid-session
        would have to be re-announced, and the client would have to hold a
        pending write against a device whose answer changed underneath it. A
        Flipper whose firmware cannot write is a *different device object* from
        one that can, discovered as such at attach.
        """
        ...

    def poll(self) -> TagRead | None:
        """Look once. `None` means the field is empty.

        Must return within a bounded time; the station's poll cadence is the
        only clock it has, and a read that blocks for ten seconds silently
        stretches every timeout budget derived from that cadence.
        """
        ...

    def close(self) -> None:
        """Release the port. Must be idempotent — the agent calls it on every
        shutdown path, including the one that is already handling an error."""
        ...


@runtime_checkable
class WritableTagSource(TagSource, Protocol):
    """A reader that can also put a URI on the tag in its field.

    Separate from `TagSource` so that "can this write" is answerable by
    `isinstance` in the one place that has to route a command, rather than by a
    `hasattr` at every call site. `capabilities.writes_ndef` is the same fact on
    the wire; the two are asserted to agree at attach.
    """

    def write_uri(self, url: str, *, overwrite: bool = False) -> TagWrite:
        """Write, then read back through this same reader. Blocking, like `poll`.

        Must raise `TagWriteRefused` rather than return a failure, and must not
        write at all when the tag already carries a URI unless `overwrite`.

        **Read-back is not optional and must go through this reader**, because a
        write that reports its own success is the thing ADR 0012 refuses. The
        reader that holds the tag is the only witness there is.
        """
        ...
