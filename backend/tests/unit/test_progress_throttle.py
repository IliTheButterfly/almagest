"""The rate limit between a streaming model and one SQLite writer.

**This file exists because removing the throttle would otherwise pass every other
test in the repository.** A worker that republished every token would produce a
perfectly correct transcript and a perfectly correct set of candidates; the only
symptom would be thousands of transactions per photograph going through the single
API replica that also serves every scan, every put-away and the ledger. Nothing else
here can see that, so it is asserted directly and by counting.

The clock is injected, so none of this sleeps.
"""

from __future__ import annotations

import itertools

from app.services.enrichment.progress import MIN_PROGRESS_INTERVAL, ProgressReporter


class _Clock:
    """A hand-wound monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reporter(clock: _Clock, sink: list[str], **kwargs: float) -> ProgressReporter:
    return ProgressReporter(sink=sink.append, clock=clock, **kwargs)


def test_the_first_partial_is_published_immediately() -> None:
    """No initial delay, deliberately.

    The whole point is to replace a silence. Waiting a second before the first sign of
    life would reintroduce the thing being fixed, in miniature — and the first frame is
    the one that says the model is alive at all.
    """
    clock, written = _Clock(), []
    reporter = _reporter(clock, written)

    assert reporter.offer("thinking") is True
    assert written == ["thinking"]


def test_a_second_partial_inside_the_interval_is_dropped() -> None:
    """The assertion the whole feature's safety rests on.

    A streamed answer arrives as hundreds of deltas over tens of seconds. Every one of
    them reaching the API would be a transaction on the one writer.
    """
    clock, written = _Clock(), []
    reporter = _reporter(clock, written)

    assert reporter.offer("a") is True
    clock.advance(MIN_PROGRESS_INTERVAL / 2)
    assert reporter.offer("ab") is False
    assert reporter.offer("abc") is False
    assert written == ["a"]
    assert reporter.writes == 1


def test_a_partial_is_published_once_the_interval_has_elapsed() -> None:
    clock, written = _Clock(), []
    reporter = _reporter(clock, written)

    reporter.offer("a")
    clock.advance(MIN_PROGRESS_INTERVAL * 1.01)
    assert reporter.offer("abcd") is True
    assert written == ["a", "abcd"]


def test_text_that_did_not_grow_is_never_republished() -> None:
    """The second condition, and it is not redundant with the interval.

    A stream emits empty deltas, keep-alive frames and a final chunk carrying nothing
    but usage counts. Each of those arrives long after the last interval expired, so the
    interval alone would let every one of them through — and each would store text that
    is already stored.
    """
    clock, written = _Clock(), []
    reporter = _reporter(clock, written)

    reporter.offer("abc")
    clock.advance(MIN_PROGRESS_INTERVAL * 10)
    assert reporter.offer("abc") is False
    # Shorter than what was published: silently ignored rather than stored. `offer`
    # compares lengths so it stays cheap enough to call per token, and the docstring
    # names this as the one way to misuse it.
    assert reporter.offer("ab") is False
    assert written == ["abc"]


def test_hundreds_of_deltas_over_a_realistic_read_cost_a_handful_of_writes() -> None:
    """The count, said as the number that matters.

    ADR 0021 measured a hard read at ~39 s of inference. Simulated here as 400 deltas
    over 40 s: unthrottled that is 400 transactions for one photograph, and it must come
    out as about one per second instead.
    """
    clock, written = _Clock(), []
    reporter = _reporter(clock, written)

    text = ""
    for index in range(400):
        text += f"token{index} "
        reporter.offer(text)
        clock.advance(0.1)

    assert 35 <= reporter.writes <= 45, reporter.writes
    # Monotonic, which is what the length comparison assumes and what makes the
    # replace-rather-than-append route correct.
    assert written == sorted(written, key=len)
    assert all(later.startswith(earlier) for earlier, later in itertools.pairwise(written))


def test_a_sink_that_raises_does_not_break_the_read() -> None:
    """A transcript is the diagnostic; the read is the work.

    If publishing a partial could raise, a route that is older than the worker or a
    momentarily busy writer would push the photograph back into the queue — and here
    that means spending a **second GPU handover** on a bookkeeping error.
    """
    clock = _Clock()
    calls: list[str] = []

    def angry(text: str) -> None:
        calls.append(text)
        raise RuntimeError("the writer is busy")

    reporter = ProgressReporter(sink=angry, clock=clock)

    assert reporter.offer("partial") is True
    assert calls == ["partial"]
    # And it kept going: the next interval still publishes, rather than the reporter
    # latching broken after one failure.
    clock.advance(MIN_PROGRESS_INTERVAL * 1.01)
    assert reporter.offer("partial and more") is True
