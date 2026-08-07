"""How long something took, measured one way.

Two callers now measure elapsed time -- the scan resolver, which persists
`scan_events.latency_ms`, and the model providers, which report what a completion
cost. That is the point at which one shared convention beats two private ones,
because the alternative is two functions that agree today and drift the first time
somebody rounds differently or forgets that a monotonic clock is the requirement.

Three decisions, all small and all worth not re-taking per caller:

* **`perf_counter`, never `time.time`.** A wall clock can step backwards across an
  NTP correction, and a negative latency stored on a row is a number nobody can
  interpret later.
* **Integer milliseconds.** Sub-millisecond precision is noise at every scale this
  system measures, and an integer column sorts and aggregates without a float's
  surprises.
* **Clamped at zero.** `max(0, ...)` costs nothing and makes the column's
  non-negativity a property of the code rather than a hope about the clock.
"""

from __future__ import annotations

import time


def elapsed_ms(started: float) -> int:
    """Milliseconds since a `time.perf_counter()` reading, never negative."""
    return max(0, round((time.perf_counter() - started) * 1000))
