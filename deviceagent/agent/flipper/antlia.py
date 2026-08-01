"""The line protocol Antlia and the bridge speak inside `app_data_exchange`.

**This file and `antlia/src/antlia_rpc.c` are two implementations of one grammar,
in two languages, in two repositories.** That is the same hazard `idcodec` exists
to remove for the short-ID codec, and it is handled the same way: the grammar is
tiny, it is written down in exactly one place (here), and the C side is a
transcription of this docstring. Keep them in step or a Flipper will answer a
command the bridge cannot read.

Design rules, in the order they mattered:

**Text, not protobuf.** The RPC envelope is already protobuf and modelling it cost
six messages (`agent.flipper.proto`). Making the *inner* protocol binary too would
have meant a second wire format on the Flipper — in C, on a device with no
debugger attached — to save a few dozen bytes per tap. Text is greppable in a log,
diffable in a test, and typeable by hand into a serial console when something is
wrong at a bench.

**One line in, one line out.** No framing beyond `\\n`, no length prefixes, no
interleaving. The RPC layer below already guarantees message boundaries and
ordering, so re-inventing them here would be two framers disagreeing about one
stream.

**Space-delimited, `-` for absent.** A URL never contains a space and never
equals `-`, so a split on whitespace is unambiguous without quoting. An empty
field would be invisible in a log line; `-` is not.

**The reason vocabulary is `agent.tags`', verbatim.** `not_blank`, `no_tag`,
`too_long`, `read_back_failed`, `unsupported`. A Flipper refusing a write and a
PN532 refusing the same write produce the same `tag.write.failed` reason, so the
PWA has one table and not one per reader.

## Grammar

Bridge → Antlia:

    PING                    is the app alive
    READ                    look once
    WRITE  <url>            write, refusing a tag that is not blank
    WRITE! <url>            write, overwriting whatever is there

Antlia → bridge:

    HELLO <version> <caps>  sent once on start; caps is r/rw
    NONE                    the field is empty
    TAG <uid|-> <url|->     something answered; either carrier may be absent
    WROTE <url|->           the write completed; the argument is the read-back
    ERR <reason> <text...>  a refusal, reason from the closed vocabulary above
    PONG

Every reply is terminated by a newline and contains no others.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from agent import tags
from agent.tags import TagCapabilities, TagRead

#: Bumped when the grammar changes in a way an older Antlia could not survive.
#: Reported in `HELLO` and checked at attach, so a stale `.fap` on a Flipper is
#: an explicit refusal rather than a reader that answers nothing.
PROTOCOL_VERSION: Final = 1

ABSENT: Final = "-"

PING: Final = "PING"
READ: Final = "READ"
WRITE: Final = "WRITE"
WRITE_OVERWRITE: Final = "WRITE!"

HELLO: Final = "HELLO"
NONE: Final = "NONE"
TAG: Final = "TAG"
WROTE: Final = "WROTE"
ERR: Final = "ERR"
PONG: Final = "PONG"

#: What Antlia may claim in `HELLO`. Anything else is refused at attach rather
#: than quietly downgraded — a typo that reads as "read-only" would make a
#: perfectly capable Flipper unable to provision, silently.
CAPS_READ: Final = "r"
CAPS_READ_WRITE: Final = "rw"


class AntliaProtocolError(Exception):
    """Antlia said something this grammar does not contain."""


# ---------------------------------------------------------------------------
# Bridge → Antlia
# ---------------------------------------------------------------------------


def command_read() -> bytes:
    return f"{READ}\n".encode()


def command_ping() -> bytes:
    return f"{PING}\n".encode()


def command_write(url: str, *, overwrite: bool = False) -> bytes:
    """`WRITE <url>` or `WRITE! <url>`.

    The bang rather than a flag argument so that the destructive form is
    unmistakable in a log line, and so that an Antlia parsing only `WRITE` can
    never be tricked into overwriting by a trailing token it ignored.
    """
    if not url or any(c.isspace() for c in url):
        raise ValueError(f"a URL with whitespace cannot travel in this protocol: {url!r}")
    verb = WRITE_OVERWRITE if overwrite else WRITE
    return f"{verb} {url}\n".encode()


# ---------------------------------------------------------------------------
# Antlia → bridge
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hello:
    version: int
    capabilities: TagCapabilities


@dataclass(frozen=True, slots=True)
class Wrote:
    """A completed write and what the tag read back. `None` is ADR 0012's
    `degraded` — see `agent.tags.TagWrite`, whose shape this mirrors."""

    read_back_url: str | None


@dataclass(frozen=True, slots=True)
class Refused:
    reason: str
    message: str


#: `NONE` decodes to this rather than to `None`, so that "the field is empty" is
#: distinguishable from "nothing has been decoded yet" at every call site — the
#: same three-valued care `TagSource.poll` takes.
@dataclass(frozen=True, slots=True)
class Empty:
    pass


Reply = Hello | Empty | TagRead | Wrote | Refused | None


def _field(token: str) -> str | None:
    return None if token == ABSENT else token


def parse_reply(line: str) -> Reply:
    """One line into a reply. `None` for `PONG`, which carries nothing.

    Unknown verbs raise rather than being ignored. A bridge that skipped a line
    it did not understand would sit waiting for a reply that had already arrived
    in a form it discarded, and the symptom — a reader that works until it
    doesn't — is much worse than a loud refusal at attach.
    """
    stripped = line.strip()
    if not stripped:
        raise AntliaProtocolError("empty line")
    verb, *rest = stripped.split(" ")

    if verb == PONG:
        return None
    if verb == NONE:
        return Empty()
    if verb == HELLO:
        if len(rest) != 2:
            raise AntliaProtocolError(f"HELLO takes a version and caps, got {stripped!r}")
        version, caps = rest
        try:
            parsed_version = int(version)
        except ValueError as error:
            raise AntliaProtocolError(f"HELLO version {version!r} is not a number") from error
        if caps not in (CAPS_READ, CAPS_READ_WRITE):
            raise AntliaProtocolError(
                f"HELLO caps {caps!r} is neither {CAPS_READ!r} nor {CAPS_READ_WRITE!r}"
            )
        return Hello(
            version=parsed_version,
            capabilities=TagCapabilities(
                reads_uid=True,
                reads_ndef=True,
                writes_ndef=caps == CAPS_READ_WRITE,
            ),
        )
    if verb == TAG:
        if len(rest) != 2:
            raise AntliaProtocolError(f"TAG takes a uid and a url, got {stripped!r}")
        return TagRead(uid=_field(rest[0]), ndef_url=_field(rest[1]))
    if verb == WROTE:
        if len(rest) != 1:
            raise AntliaProtocolError(f"WROTE takes one read-back argument, got {stripped!r}")
        return Wrote(read_back_url=_field(rest[0]))
    if verb == ERR:
        if not rest:
            raise AntliaProtocolError(f"ERR needs a reason, got {stripped!r}")
        reason, *words = rest
        if reason not in REASONS:
            raise AntliaProtocolError(f"ERR reason {reason!r} is not in the vocabulary")
        return Refused(reason=reason, message=" ".join(words) or reason)

    raise AntliaProtocolError(f"unknown verb {verb!r}")


#: The closed set, shared with `agent.tags` so a Flipper and a PN532 refuse a
#: write with the same word. Asserted against that module in the tests, because
#: two copies of a vocabulary is exactly how they drift.
REASONS: Final = frozenset(
    {
        tags.NO_TAG,
        tags.NOT_BLANK,
        tags.TOO_LONG,
        tags.UNSUPPORTED,
        tags.READ_BACK_FAILED,
    }
)
