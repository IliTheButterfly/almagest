"""The 400 ms hold-off, pinned to the semantics of its TypeScript sibling.

`frontend/src/lib/scan/holdoff.ts` is the same mechanism in the browser and
`frontend/src/lib/scan/holdoff.test.ts` asserts the same three properties. Two
implementations exist because the runtimes do; these tests are what keep them one
mechanism rather than two that drifted.
"""

from __future__ import annotations

import pytest

from agent.holdoff import DEFAULT_WINDOW_MS, HoldOff


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms / 1000.0


def test_the_window_is_plan_mds_four_hundred_milliseconds() -> None:
    """PLAN.md's cabinet-binding recipe: "after a 400 ms debounce (so the same tag
    can't double-fire while still in range)". The frontend's
    `FEEDBACK_DEBOUNCE_MS` is the same number for the same reason."""
    assert DEFAULT_WINDOW_MS == 400


def test_the_first_sighting_is_admitted() -> None:
    assert HoldOff(400, clock=Clock()).admit("take:5")


def test_a_repeat_inside_the_window_is_dropped() -> None:
    clock = Clock()
    holdoff = HoldOff(400, clock=clock)
    assert holdoff.admit("take:5")
    clock.advance(399)
    assert not holdoff.admit("take:5")


def test_a_repeat_after_the_window_is_admitted() -> None:
    clock = Clock()
    holdoff = HoldOff(400, clock=clock)
    assert holdoff.admit("take:5")
    clock.advance(400)
    assert holdoff.admit("take:5")


def test_the_window_runs_from_the_admitted_sighting_not_the_last_one() -> None:
    """The literal reading of "a duplicate inside the window is dropped", and what a
    Commit button needs: a held-off tap does not push the window forward, so the
    user is never locked out for as long as they keep tapping."""
    clock = Clock()
    holdoff = HoldOff(400, clock=clock)
    holdoff.admit("confirm")
    for _ in range(3):
        clock.advance(100)
        assert not holdoff.admit("confirm")
    clock.advance(101)
    assert holdoff.admit("confirm")


def test_two_keys_are_held_off_independently() -> None:
    """One most-recent slot would let A, B, A, B fire four times — the reason the TS
    version is keyed per payload too."""
    clock = Clock()
    holdoff = HoldOff(400, clock=clock)
    assert holdoff.admit("take:5")
    assert holdoff.admit("take:3")
    clock.advance(100)
    assert not holdoff.admit("take:5")
    assert not holdoff.admit("take:3")


def test_clearing_forgets_every_window() -> None:
    """Called when a session ends: its keys carry that session's idempotency key and
    are dead the moment it is. Without this a bench running for weeks accumulates
    them."""
    holdoff = HoldOff(400, clock=Clock())
    holdoff.admit("take:5")
    holdoff.clear()
    assert holdoff.admit("take:5")


def test_a_zero_window_admits_everything() -> None:
    """What `DEVICEAGENT_COMMAND_DEBOUNCE_MS=0` means, and it must not mean
    "block everything"."""
    holdoff = HoldOff(0, clock=Clock())
    assert holdoff.admit("take:5")
    assert holdoff.admit("take:5")


def test_a_negative_window_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        HoldOff(-1)
