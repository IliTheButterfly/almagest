"""Publishing a streamed answer while it is still arriving, at a bounded rate.

A vision read is a **forty-second silence** today. ADR 0021 measured why: qwen3-vl
is a thinking model and spends its budget reasoning before the answer begins —
1700 characters on the easy corpus case, and once **12 318 characters with no
answer at all**. None of that is visible until the call returns, so a person
watching the intake queue cannot tell an overrunning read from a hung worker, and
the only evidence arrives after the GPU second is already spent.

So the worker asks for a streamed response and republishes the partial text onto
its `model_runs` row as it grows. `app.api.routes.activity` serves it and the
activity screen polls. No push transport, no API-to-model coupling, ADR 0005
untouched — the API still stores text somebody else generated.

## The throttle is the whole reason this module exists

**The API is one SQLite writer.** A per-token republish would put thousands of
transactions through it for one photograph, on the single replica that also serves
every scan, every put-away and the ledger. That is not a performance nicety: the
writer is pinned to an RWO volume with `strategy: Recreate` precisely because there
is only ever one of it, and flooding it is how a background diagnostic starves the
foreground.

Two conditions, and both are required:

* **At most one write per `min_interval`.** One second, which is the granularity
  the design accepted: a person watching reasoning accumulate does not need
  sub-second frames, and the difference between 1 Hz and 30 Hz here is thirty times
  the write load for no readable difference.
* **Only when the text actually grew.** A stream emits empty deltas, keep-alive
  frames and a final chunk carrying nothing but usage counts. Republishing an
  unchanged string is a transaction that stores what is already stored.

The first offer is **not** delayed. It publishes immediately, because the whole
point is to replace a silence — waiting a second before the first sign of life
would reintroduce the thing being fixed, in miniature.

## Growth is assumed, and that is a contract on the caller

`offer` compares lengths rather than contents, so the accumulated text must only
ever grow. Every producer here satisfies that by construction (it appends deltas),
and the comparison is what keeps this cheap enough to call per token. A caller that
rewrote the text shorter would be silently ignored — said out loud because it is
the one way to misuse this.

## Failing to publish must never fail the read

`sink` is wrapped: an exception from it is logged and swallowed. A progress write
is a diagnostic and the read is the work, so a transcript route that is older than
the worker, or a momentarily busy writer, must not push a photograph back into the
queue to spend a **second GPU handover** on a bookkeeping error.
`dispatch_captures.HttpApiClient.record_run` takes the same posture for the same
reason.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("almagest.dispatch.progress")

#: Seconds between republished partials, at most. See the module docstring: this is
#: the number that keeps a background diagnostic from flooding the one writer.
MIN_PROGRESS_INTERVAL = 1.0


@dataclass
class ProgressReporter:
    """Rate-limited republishing of an answer that is still being generated."""

    #: Where a partial goes. Called with the **whole accumulated text**, not a
    #: delta: the receiving route replaces rather than appends, so a dropped or
    #: duplicated call cannot corrupt the transcript into something the model never
    #: said.
    sink: Callable[[str], None]
    min_interval: float = MIN_PROGRESS_INTERVAL
    #: Injectable so the throttle is testable without sleeping. `monotonic` rather
    #: than `time.time`, because a clock stepped backwards by NTP would otherwise
    #: silence progress for as long as the step.
    clock: Callable[[], float] = time.monotonic

    #: How many partials actually reached the sink. Read by the tests, and by
    #: nothing else — it is what makes "the throttle is doing something" assertable
    #: rather than inferred from timing.
    writes: int = field(default=0, init=False)
    _last_at: float | None = field(default=None, init=False)
    _published: int = field(default=0, init=False)

    def offer(self, text: str) -> bool:
        """Publish `text` if it grew and the interval has elapsed. True if written.

        Cheap enough to call per token, which is the point — the caller should not
        have to know the policy.
        """
        if len(text) <= self._published:
            return False
        now = self.clock()
        if self._last_at is not None and now - self._last_at < self.min_interval:
            return False
        self._last_at = now
        self._published = len(text)
        self.writes += 1
        try:
            self.sink(text)
        except Exception as error:  # a sink's exception types are not enumerable
            # Swallowed on purpose — see the module docstring. The read is the work.
            log.warning("could not republish a partial answer: %s", error)
        return True
