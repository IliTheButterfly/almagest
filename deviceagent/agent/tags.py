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
from typing import Protocol, runtime_checkable


def format_uid(raw: bytes | bytearray) -> str:
    """Reader bytes → the hex string `agent.identity` normalises.

    Here rather than in a driver because there are now two drivers, and a UID
    rendered by one differently from the other is the exact failure
    `almagest-idcodec` exists to prevent one layer down: it stays invisible to
    the `location_tags` binding it should match while looking perfectly correct
    on screen. `bytes(...)` rather than `.hex()` on the argument so an empty UID
    produces `""` — which every caller turns into `None` — rather than something
    `normalize_tag_uid` would accept.
    """
    return bytes(raw).hex().upper()


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


@runtime_checkable
class TagSource(Protocol):
    """A pollable NFC reader.

    Implementations must not raise for an empty field or an unreadable tag —
    those are return values. `TagSourceError` is reserved for the reader itself.
    """

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
