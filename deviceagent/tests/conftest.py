"""Shared fixtures, and the hook that keeps the hardware tests out of CI.

Mirrors `backend/tests/conftest.py`: `pyproject.toml` registers `live` as
"skipped unless `-m live` is passed", and `make agent-test` runs a bare pytest, so
without this hook that sentence would be a comment rather than a behaviour — and
the first reader contract test added would run in CI, where there is no reader of
either kind, and fail there.

**Skipped, not deselected.** A deselected test is invisible: it does not appear in
the summary, and the whole point of the live test is to be a visible reminder that
a contract exists which nothing in CI can exercise. A skip line in the output says
so on every run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

from agent.api import ActionKind, Movement, StationApi
from agent.events import Event
from agent.fake_tags import FakeTagSource, ScriptedPoll, load_script
from agent.presence import TagPresence
from agent.session import StationSession
from agent.tags import TagRead, TagSourceError
from tests.fake_api import FakeStationApi


def pytest_addoption(parser: pytest.Parser) -> None:
    """A second opt-in, on top of `-m live`, for the tests that *modify a tag*.

    Every other live assertion observes hardware; the Flipper's write tests
    change what is on the tag in your hand. Running the whole live file because
    you wanted the read checks, and silently rewriting a drawer's tag, is the
    sort of thing that is discovered at a bench a week later.
    """
    parser.addoption(
        "--flipper-write",
        action="store_true",
        default=False,
        help="allow the live Flipper tests that write to the tag on the antenna",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "live" in str(config.getoption("markexpr", default="") or ""):
        return
    skip_live = pytest.mark.skip(
        reason="needs a real reader wired up: run with `-m live` (make agent-test-live)"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def script() -> tuple[ScriptedPoll, ...]:
    """The packaged session script — the same bytes `--fake` replays."""
    return load_script()


@pytest.fixture
def source(script: tuple[ScriptedPoll, ...]) -> Iterator[FakeTagSource]:
    fake = FakeTagSource(script)
    yield fake
    fake.close()


@pytest.fixture
def presence() -> TagPresence:
    """Defaults from PLAN.md: 5 identify tries, 3 empty polls to confirm removal."""
    return TagPresence(identify_polls=5, absent_polls=3)


class Station:
    """A whole station, driven synchronously: fake reader → presence → session.

    Three things this harness exists to guarantee, all of which a test would
    otherwise get subtly wrong:

    * **the session is never fed hand-built presence events.** Every placement
      goes through `FakeTagSource` and the real `TagPresence`, so a test cannot
      accidentally assert on a transition the reader could not produce;
    * **no test sleeps.** The identify budget and the removal debounce are counts
      of polls, and the 400 ms command hold-off reads a `clock` this class steps by
      hand. A suite that waited 1.5 s per placement is a suite nobody runs;
    * **one event loop for the whole test.** `asyncio.Lock` binds to the loop it is
      first awaited on, so a per-call `asyncio.run` would fail on the second call.
      `run_until_complete` on one owned loop keeps the tests readable as straight
      line code.
    """

    def __init__(self, api: StationApi, *, debounce_ms: int = 400) -> None:
        self.api = api
        self.now = 0.0
        self.minted: list[str] = []
        self._loop = asyncio.new_event_loop()
        self.presence = TagPresence(identify_polls=5, absent_polls=3)
        self.session = StationSession(
            api,
            debounce_ms=debounce_ms,
            mint=self._mint,
            clock=lambda: self.now,
        )

    def _mint(self) -> str:
        # Predictable ids, so a test can say *which* key was committed under
        # instead of only that some uuid was.
        minted = f"id-{len(self.minted) + 1}"
        self.minted.append(minted)
        return minted

    def close(self) -> None:
        self._loop.close()

    # -- the reader --------------------------------------------------------

    def poll(self, *polls: ScriptedPoll) -> list[Event]:
        """Replay these polls exactly as `main.poll_forever` does, session included.

        A fresh `FakeTagSource` per batch because a script is immutable once
        constructed; the *presence machine* is the thing that carries state across
        batches, and it is the same instance throughout.
        """
        source = FakeTagSource(polls)
        emitted: list[Event] = []
        while not source.exhausted:
            try:
                read = source.poll()
            except TagSourceError as error:
                caused = self.presence.observe_fault(str(error))
            else:
                caused = self.presence.observe(read)
            for event in caused:
                emitted.append(event)
                emitted.extend(self._loop.run_until_complete(self.session.on_presence(event)))
        return emitted

    def place(self, tag: TagRead, *, times: int = 1) -> list[Event]:
        return self.poll(*[ScriptedPoll(read=tag) for _ in range(times)])

    def lift(self, *, polls: int = 3) -> list[Event]:
        return self.poll(*[ScriptedPoll() for _ in range(polls)])

    def fault(self, message: str = "PN532 did not ACK") -> list[Event]:
        return self.poll(ScriptedPoll(error=message))

    # -- the client --------------------------------------------------------

    def send(self, kind: str, **payload: Any) -> list[Event]:
        """One command frame. `session_id` defaults to the live session's."""
        frame: dict[str, Any] = {"type": kind}
        if "session_id" not in payload:
            frame["session_id"] = self.session.session_id
        frame.update(payload)
        return self.raw(json.dumps(frame))

    def raw(self, frame: str) -> list[Event]:
        return self._loop.run_until_complete(self.session.handle_frame(frame))

    def commit_directly(
        self, *, kind: str, lot_id: int, qty_milli: int, client_op_id: str
    ) -> Movement:
        """Commit through the API client, bypassing the session.

        This is what a *client's* retry of a lost response looks like from the
        API's side, and the only way to exercise it: the session itself cannot be
        made to send one key twice, which is the property being relied on.
        """
        return self._loop.run_until_complete(
            self.api.commit(
                kind=ActionKind(kind),
                lot_id=lot_id,
                qty_milli=qty_milli,
                client_op_id=client_op_id,
            )
        )

    def advance(self, ms: float) -> None:
        """Step the hold-off clock. Monotonic seconds, as `time.monotonic` returns."""
        self.now += ms / 1000.0


@pytest.fixture
def api() -> FakeStationApi:
    return FakeStationApi()


@pytest.fixture
def station(api: FakeStationApi) -> Iterator[Station]:
    bench = Station(api)
    yield bench
    bench.close()
