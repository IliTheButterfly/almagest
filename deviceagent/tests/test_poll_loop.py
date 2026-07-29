"""The poll loop's pacing: the period is the interval, not the interval plus the read.

**The defect this file exists for.** `poll_forever` used to `await
asyncio.sleep(interval_s)` *after* the read, so the true poll period was
`read + interval`, never `interval`. Every duration this daemon publishes is a
count of polls multiplied by the interval — the ~1.5 s identify budget in
`agent/config.py`, README.md and `.env.example`, and the ~0.9 s removal debounce in
`agent/presence.py` — so all of them were inflated by one read per poll. The read
is not free in exactly the cases those budgets govern: `read_passive_target` blocks
for its full `DEFAULT_TARGET_TIMEOUT_S` (250 ms) *before* reporting an empty field,
which is every poll of a removal debounce and every poll of an unreadable tag's
identify budget. Driven against a reader that blocked 250 ms, five 300 ms polls
took 2.76 s and three took 1.66 s.

**Why the shape matters more than the milliseconds.** No reader has ever been
attached to this code, so nothing here can falsify a wall-clock claim that is pure
multiplication — the number simply looked right in four files at once. The fix is
therefore to make the arithmetic true by construction (pace to a fixed period) and
to say plainly what remains unmeasured (whether a real poll fits inside an
interval at all — README.md, "Unverifiable without hardware", item 2).

The clock and the sleeper are injected rather than measured, for the same reason
`StationSession` takes a `clock`: the arithmetic *is* the assertion, and a
stopwatch would be both slow and flaky on a loaded runner.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agent.hub import EventHub
from agent.main import poll_forever
from agent.nfc_pn532 import DEFAULT_TARGET_TIMEOUT_S
from agent.presence import DEFAULT_ABSENT_POLLS, DEFAULT_IDENTIFY_POLLS, TagPresence
from agent.session import StationSession
from agent.tags import TagRead
from tests.fake_api import FakeStationApi

#: `agent.config.AgentSettings.poll_interval_ms`, as seconds.
INTERVAL_S = 0.300

#: What one poll costs when the field does not answer, which is the whole of both
#: budgets. Imported rather than retyped so a change to the driver's timeout shows
#: up here as a changed expectation instead of as a stale comment.
READ_S = DEFAULT_TARGET_TIMEOUT_S

UNREADABLE = TagRead(uid=None, ndef_url=None)


class StepClock:
    """A monotonic clock the test owns, plus the sleeper that advances it.

    Callable so it drops straight into the `clock` parameter, and `sleep` records
    what it was asked for — which is the interesting half: a paced loop asks for
    `interval − elapsed`, an unpaced one asks for `interval` every time.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class CostlyReader:
    """A `TagSource` whose `poll` costs time, the way a real reader's does.

    It charges the clock rather than really sleeping. Safe despite running under
    `asyncio.to_thread`: the loop is blocked awaiting this call, so nothing else
    touches the clock while it does.
    """

    def __init__(self, clock: StepClock, *, cost_s: float, read: TagRead | None) -> None:
        self._clock = clock
        self._cost_s = cost_s
        self._read = read
        self.polls = 0
        self.closed = False

    def poll(self) -> TagRead | None:
        self.polls += 1
        self._clock.now += self._cost_s
        return self._read

    def close(self) -> None:
        self.closed = True


def drive(
    *,
    read: TagRead | None,
    cost_s: float,
    polls: int,
    interval_s: float = INTERVAL_S,
) -> StepClock:
    """Run the **real** `poll_forever` for `polls` iterations. Returns the clock.

    The real presence machine and the real session are wired in rather than stubbed,
    so the measured period includes everything a poll actually does — folding the
    read, publishing, and letting the session react.
    """
    clock = StepClock()
    source = CostlyReader(clock, cost_s=cost_s, read=read)
    asyncio.run(
        poll_forever(
            source,
            TagPresence(identify_polls=DEFAULT_IDENTIFY_POLLS, absent_polls=DEFAULT_ABSENT_POLLS),
            StationSession(FakeStationApi(), debounce_ms=0, clock=clock),
            EventHub(),
            interval_s=interval_s,
            max_polls=polls,
            clock=clock,
            sleep=clock.sleep,
        )
    )
    assert source.polls == polls
    return clock


def test_the_identify_budget_is_polls_times_the_interval_not_polls_times_read_plus_interval() -> (
    None
):
    """5 × 300 ms = 1.5 s, against the reader cost the budget is actually made of.

    Unpaced this was 5 × (250 + 300) ms = 2.75 s, which is the defect: the identify
    budget is spent entirely on polls where the field does not answer, and those are
    the polls that cost the driver's full anticollision timeout.
    """
    clock = drive(read=UNREADABLE, cost_s=READ_S, polls=DEFAULT_IDENTIFY_POLLS)
    assert clock.slept == pytest.approx([INTERVAL_S - READ_S] * DEFAULT_IDENTIFY_POLLS)
    assert clock.now == pytest.approx(DEFAULT_IDENTIFY_POLLS * INTERVAL_S)


def test_the_removal_debounce_is_polls_times_the_interval_too() -> None:
    """3 × 300 ms = 0.9 s. An empty platform is the same expensive poll: the PN532
    blocks for its whole timeout before reporting that nothing answered."""
    clock = drive(read=None, cost_s=READ_S, polls=DEFAULT_ABSENT_POLLS)
    assert clock.slept == pytest.approx([INTERVAL_S - READ_S] * DEFAULT_ABSENT_POLLS)
    assert clock.now == pytest.approx(DEFAULT_ABSENT_POLLS * INTERVAL_S)


def test_a_free_read_is_paced_to_the_same_period() -> None:
    """Where the defect hid. A zero-cost reader — every fake in this suite, and the
    only reader that has ever run — produced exactly the documented number, so the
    claim was true of the fixture and false of the hardware."""
    clock = drive(read=UNREADABLE, cost_s=0.0, polls=DEFAULT_IDENTIFY_POLLS)
    assert clock.slept == pytest.approx([INTERVAL_S] * DEFAULT_IDENTIFY_POLLS)
    assert clock.now == pytest.approx(DEFAULT_IDENTIFY_POLLS * INTERVAL_S)


def test_a_read_longer_than_the_interval_never_sleeps_negative_and_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one case pacing cannot fix, so it is reported instead of absorbed.

    Past the interval the cadence *is* the read and both budgets stretch — which is
    the unmeasured hardware risk, since 250 ms of anticollision leaves 50 ms of a
    300 ms interval for an NDEF read of one UART round trip per page. Logged once
    per run of overruns, not once per poll: at 3 polls/second a line each would
    bury itself. And the sleep is floored at zero, so a slow poll catches up rather
    than the loop drifting further behind on every iteration.
    """
    caplog.set_level(logging.WARNING, logger="almagest.deviceagent")
    slow_s = 0.500
    clock = drive(read=UNREADABLE, cost_s=slow_s, polls=4)

    assert clock.slept == [0.0, 0.0, 0.0, 0.0]
    assert clock.now == pytest.approx(4 * slow_s)
    overruns = [record for record in caplog.records if "over the" in record.getMessage()]
    assert len(overruns) == 1
    assert "500 ms" in overruns[0].getMessage()


def test_an_interval_of_zero_is_not_an_overrun() -> None:
    """`interval_s=0` is the tests and `--max-polls`, where there is no cadence to
    fall behind. Warning there would make every existing socket test noisy."""
    clock = drive(read=UNREADABLE, cost_s=READ_S, polls=3, interval_s=0.0)
    assert clock.slept == [0.0, 0.0, 0.0]
