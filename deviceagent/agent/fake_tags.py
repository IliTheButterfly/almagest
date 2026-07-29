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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Final

from agent.tags import TagRead, TagSourceError


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
