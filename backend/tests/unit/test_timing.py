"""The one way this system measures how long something took.

Small enough to look unnecessary, and it is not: `scan_events.latency_ms` is a
persisted column and a negative value in it is uninterpretable forever. The clamp
is the behaviour worth pinning down, because the clock that would violate it is
the one nobody can reproduce on demand.
"""

from __future__ import annotations

import time

from app.services.timing import elapsed_ms


def test_elapsed_is_measured_forward() -> None:
    started = time.perf_counter()
    assert elapsed_ms(started) >= 0


def test_a_reading_from_the_future_clamps_to_zero() -> None:
    """Rather than returning a negative number nobody downstream can interpret.

    Reachable in practice through a clock that steps, which is precisely why
    `perf_counter` is required and why this costs one `max()` to guarantee
    instead of hoping.
    """
    assert elapsed_ms(time.perf_counter() + 5.0) == 0


def test_the_result_is_whole_milliseconds() -> None:
    # An integer column sorts and aggregates without a float's surprises, and
    # sub-millisecond precision is noise at every scale measured here.
    assert isinstance(elapsed_ms(time.perf_counter()), int)
