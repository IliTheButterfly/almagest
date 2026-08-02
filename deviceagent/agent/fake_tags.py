"""`FakeTagSource` — the only tag reader this codebase can actually verify.

Shipped in the package rather than hidden in `tests/`, for two reasons. It is
what `--fake` runs, so a frontend developer can drive the kiosk PWA's whole
station screen with no Pi and no reader on the desk. And it means the script the
tests assert against is byte-for-byte the script the agent replays, instead of
two hand-maintained sequences that drift.

The default script is `agent/fixtures/scripted_session.json`, and it is
**hand-written, not recorded** — no PN532 has ever been attached to this code.
That file's `description` field says so, and re-recording it against a real
reader is the first thing to do when one exists: the tests are written against
the *situations* in the script, so a re-recorded file that still contains those
situations makes every one of them a real regression test.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Final

from agent import ndef, tags
from agent.tags import (
    READS_BOTH,
    READS_BOTH_AND_WRITES,
    TagCapabilities,
    TagRead,
    TagSourceError,
    TagWrite,
    TagWriteRefused,
)


#: A scripted poll: what the reader saw, or the fault it raised instead.
#: `read is None` is an empty field — the same three-valued convention as
#: `TagSource.poll`, so a script cannot express something a reader cannot.
@dataclass(frozen=True, slots=True)
class ScriptedPoll:
    read: TagRead | None = None
    error: str | None = None
    #: Free text from the fixture, kept so a failing test can say *which* poll.
    note: str = ""


SCRIPT_RESOURCE: Final = "scripted_session.json"


def _poll_from_mapping(entry: dict[str, Any]) -> ScriptedPoll:
    error = entry.get("error")
    if error is not None:
        return ScriptedPoll(read=None, error=str(error), note=str(entry.get("note", "")))
    tag = entry.get("tag")
    read = None if tag is None else TagRead(uid=tag.get("uid"), ndef_url=tag.get("ndef_url"))
    return ScriptedPoll(read=read, note=str(entry.get("note", "")))


def load_script(path: Path | None = None) -> tuple[ScriptedPoll, ...]:
    """Read a session script. `None` loads the one packaged with the agent."""
    if path is None:
        text = (files("agent") / "fixtures" / SCRIPT_RESOURCE).read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    return tuple(_poll_from_mapping(entry) for entry in document["polls"])


class FakeTagSource:
    """Replays a scripted session, one `poll()` at a time.

    Satisfies `TagSource` structurally; the tests assert that with
    `isinstance(..., TagSource)` so the fake cannot drift from the protocol the
    real reader implements.
    """

    def __init__(
        self, polls: Iterable[ScriptedPoll] | None = None, *, repeat: bool = False
    ) -> None:
        self._polls: Sequence[ScriptedPoll] = tuple(polls) if polls is not None else load_script()
        if not self._polls:
            raise ValueError("a script with no polls cannot stand in for a reader")
        self._repeat = repeat
        self.index = 0
        self.closed = False

    @property
    def capabilities(self) -> TagCapabilities:
        """Reads both carriers, writes nothing.

        A scripted source *cannot* honestly claim a write: the script says what
        the next poll returns, so a "write" would be a no-op that then read back
        whatever the fixture said next. That is a fake that passes a test the
        real reader would fail. `FakeWritableTagSource` models a tag instead.
        """
        return READS_BOTH

    @property
    def exhausted(self) -> bool:
        return not self._repeat and self.index >= len(self._polls)

    def poll(self) -> TagRead | None:
        """The next scripted poll.

        **An exhausted script reads as an empty platform, not an error.** The
        agent's poll loop must not die because a demo script ran out — the
        failure mode of a station that stops answering is a user who thinks the
        bench is broken, and `--fake` is meant to be left running.
        """
        if self.closed:
            raise TagSourceError("poll() after close()")
        if self.exhausted:
            return None

        entry = self._polls[self.index % len(self._polls)]
        self.index += 1
        if entry.error is not None:
            raise TagSourceError(entry.error)
        return entry.read

    def close(self) -> None:
        self.closed = True


class FakeWritableTagSource:
    """One simulated NTAG213 in the field, with real user memory.

    The Python twin of `frontend/src/lib/tags/simulated.ts`, and modelled on the
    same three tags because they are the three that break a provisioning walk in
    different places:

    * **blank** — the happy path, and the only one `overwrite=False` accepts;
    * **already written** — carries someone else's URI, so a walk that does not
      refuse it silently rebinds a drawer;
    * **silently failing** — accepts every write and reads back unchanged. This
      is the one worth having a fake for at all. It is ADR 0012's `degraded`
      arriving through the *success* path, and a writer that trusted its own
      write instead of reading back would report it as verified.

    Unlike `FakeTagSource` this is not scripted: it holds bytes, and a write
    changes what the next `poll` returns. That is what makes the round trip —
    write, read back, compare — a real assertion rather than a fixture agreeing
    with itself. The payload goes through `agent.ndef` in both directions, so the
    encoder and the parser are exercised, not bypassed.
    """

    def __init__(
        self,
        *,
        uid: str | None = "04A2B3C4D5E680",
        url: str | None = None,
        writes_land: bool = True,
        present: bool = True,
        user_pages: int = ndef.NTAG213_USER_PAGES,
    ) -> None:
        self.uid = uid
        self.url = url
        #: False models a tag that acknowledges every write and keeps its old
        #: contents — a worn tag, or one lifted mid-write.
        self.writes_land = writes_land
        #: False models an empty field, so `no_tag` is reachable without
        #: rebuilding the object.
        self.present = present
        self.user_pages = user_pages
        self.writes: list[str] = []
        self.closed = False

    @property
    def capabilities(self) -> TagCapabilities:
        return READS_BOTH_AND_WRITES

    def poll(self) -> TagRead | None:
        if self.closed:
            raise TagSourceError("poll() after close()")
        if not self.present:
            return None
        return TagRead(uid=self.uid, ndef_url=self.url)

    def write_uri(self, url: str, *, overwrite: bool = False) -> TagWrite:
        """Same refusal order as `Pn532TagSource.write_uri`, and the same read-back.

        Kept in step by `tests/test_tag_write.py`, which runs one table of cases
        against this and (under `-m live`) against the real reader.
        """
        if self.closed:
            raise TagSourceError("write_uri() after close()")
        if not self.present:
            raise TagWriteRefused("no tag in the field", reason=tags.NO_TAG)
        # Raises ValueError when it will not fit, before anything is touched.
        pages = ndef.pages_for_uri(url, user_pages=self.user_pages)
        if self.url is not None and not overwrite:
            raise TagWriteRefused(f"the tag already carries {self.url!r}", reason=tags.NOT_BLANK)

        self.writes.append(url)
        if self.writes_land:
            # Round-tripped through the real codec rather than stored verbatim,
            # so an encoder that mangles a URI is caught here and not on a bench.
            self.url = ndef.parse_uri_record(ndef.collect_ndef_bytes(_pager(pages)))

        read_back = self.poll()
        if read_back is None or read_back.ndef_url is None:
            raise TagWriteRefused(
                "the tag did not read back after writing", reason=tags.READ_BACK_FAILED
            )
        return TagWrite(read_back_url=read_back.ndef_url)

    def close(self) -> None:
        self.closed = True


def _pager(pages: Sequence[bytes]) -> Callable[[int], bytes | None]:
    """`collect_ndef_bytes`'s `read_page` over a list, so the fake reads its own
    written pages back exactly the way `Pn532TagSource` reads a real tag's."""

    def read_page(page: int) -> bytes | None:
        index = page - ndef.FIRST_USER_PAGE
        if index < 0 or index >= len(pages):
            return None
        return pages[index]

    return read_page
