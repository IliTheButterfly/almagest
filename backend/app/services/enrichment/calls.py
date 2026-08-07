"""What one model call cost, in the vocabulary every provider can report.

Its own module because two pure interfaces need it -- `extract.ExtractionResult`
and `vision.VisionResult` -- and neither should have to import the other to say
how long a call took. `extract` re-exports the name so every existing import path
keeps working.

Deliberately not a timing concern and so deliberately not in `services/timing.py`:
latency is one of four fields here, and the other three come from the server
rather than from a clock.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallStats:
    """What one completion cost, as the server reported it.

    Every field but the latency is `None` when the server did not say, which is
    normal: `usage` is optional in the OpenAI response shape and several local
    servers omit it. **A missing count must stay distinguishable from a count of
    zero**, so none of these default to 0 -- an average over a benchmark run that
    silently folded in zeroes would pull toward whichever models were quiet.

    **`finish_reason` is here for a specific bug rather than for completeness.**
    `max_tokens` truncating a 24-variant batch produces invalid JSON, and the
    extraction provider used to report that as *"does this model support
    constrained decoding?"* -- a diagnosis that is flatly wrong and sends whoever
    reads it to investigate the serving stack instead of the batch size.
    `"length"` here says what actually happened.

    Servers disagree on the vocabulary: Ollama's native endpoint counts in
    `prompt_eval_count`/`eval_count` and says `done_reason`. Each transport maps
    its own into this shape, and passes an unrecognised reason through as-is
    rather than coercing it into a vocabulary it does not belong to.
    """

    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """Did the answer stop because it ran out of room rather than finishing?"""
        return self.finish_reason == "length"
