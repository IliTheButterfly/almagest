"""The 400 ms hold-off, keyed per payload. Python sibling of the PWA's copy.

`frontend/src/lib/scan/holdoff.ts` is the same mechanism with the same semantics,
and `frontend/src/lib/scan/feedback.ts` fixes the window this module defaults to.
The number is **not invented here**: PLAN.md's tag-provisioning walk specifies a
"400 ms debounce (so the same tag can't double-fire while still in range)" before
the cursor auto-advances, and the frontend already reuses it for decode feedback.
Reusing it a third time is the point — one number, one meaning, so a user who has
learned how fast the bench responds is not surprised by which screen they are on.

Two implementations rather than one because the two runtimes are genuinely
separate (a browser and a daemon on the Pi), and a shared constant would have to
travel through the API schema, which is for the data model and not for UI timing.
`tests/test_holdoff.py` pins the semantics to the TypeScript file's.

Keyed per payload rather than on one most-recent slot, for the same reason as the
TS version: two alternating keys must each be held off independently, or A, B, A,
B fires four times.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: PLAN.md's cabinet-binding recipe, and `FEEDBACK_DEBOUNCE_MS` in the frontend.
DEFAULT_WINDOW_MS = 400


class HoldOff:
    """Admit a key at most once per window.

    The window runs from the sighting that was **admitted**, not from the last one
    seen — the literal reading of "duplicate within the window is dropped", and the
    behaviour a Commit button needs: a double-tap is one commit, and a deliberate
    second identical action a second later is honoured rather than being held off
    for as long as the user keeps tapping.
    """

    def __init__(
        self,
        window_ms: int = DEFAULT_WINDOW_MS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_ms < 0:
            raise ValueError("window_ms cannot be negative")
        self._window_s = window_ms / 1000.0
        # Monotonic, not wall clock: an NTP step or a DST change must not open or
        # close the window, and a station Pi does step its clock at boot.
        self._clock = clock
        self._admitted: dict[str, float] = {}

    def admit(self, key: str) -> bool:
        """Whether to act on this key now. Records the sighting when it says yes."""
        now = self._clock()
        last = self._admitted.get(key)
        if last is not None and now - last < self._window_s:
            return False
        self._admitted[key] = now
        return True

    def forget(self, key: str) -> None:
        """Drop a key's window, so the next sighting is admitted immediately.

        Called when the thing the key identified is gone — a session ending, an
        idempotency key rotating. Without it the map grows for the lifetime of the
        process, and at a bench that is weeks.
        """
        self._admitted.pop(key, None)

    def clear(self) -> None:
        self._admitted.clear()
