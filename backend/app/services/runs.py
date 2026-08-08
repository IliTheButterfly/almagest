"""Recording a model call, watching it while it runs, and reading the runs back.

The interesting decisions are mostly in `app.models.runs`; what is left here is
the one thing a service has to own, which is **what happens when a transcript is
longer than the column** — plus, now, the lifecycle of a row that is written
before the answer exists.

## A run is created at the start, not at the end

It used to be recorded once, afterwards, with everything filled in. That is the
simpler shape and it cannot show anything live: for the forty seconds ADR 0021
measured, there was no row at all, so a person watching had nothing to look at and
could not distinguish an overrunning read from a hung worker.

So `record` (start) and `finish` (settle) are two calls, with `progress` republishing
the partial answer in between. Three consequences worth stating:

* **`finished_at IS NULL` is what "in flight" means.** No second state column, no
  `status` enum. A NULL timestamp already carries it, and a derived flag that can
  disagree with the timestamp is worse than no flag.
* **A worker that dies mid-read leaves a row that never finishes.** That is honest —
  something *was* asked and never came back — but it must not make a screen poll
  forever, so `in_flight_at` calls a long-silent run stale. See `STALE_AFTER_SECONDS`.
* **`progress` refuses a finished run.** A partial arriving after the final answer
  would overwrite the real completion with an earlier prefix of it, which is data
  loss dressed as an update. Out-of-order delivery is normal over HTTP, so this is a
  guard against the ordinary case rather than a paranoid one.

## Truncating rather than refusing, and saying so in the row

## Truncating rather than refusing, and saying so in the row

A run whose prompt is 300 000 characters is not a bug report, it is a run. The
alternative — refusing the submission — loses the record of a model call that
genuinely happened, and loses it in the direction that matters: the pathological
runs are the ones with the most to read. So the text is cut and
`ModelRun.truncated` is set, which is the difference between an incomplete record
and a silently incomplete one.

`MAX_TRANSCRIPT_CHARS` therefore lives *here* and not on the wire type. A route
that refused a 200 001-character transcript would make a worker responsible for
knowing the number, and a worker that discovered it would have no better option
than to throw the transcript away — which is the outcome the bound exists to
avoid rather than to cause.

It applies to a **partial** exactly as it does to a final answer, and that is where
it earns its keep: the accumulated reasoning of a runaway read is the text most
likely to reach the ceiling, and it reaches it while somebody is watching.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ModelRunKind
from app.models.runs import MAX_TRANSCRIPT_CHARS, ModelRun
from app.models.types import utcnow

#: How long an unfinished run stays "in flight" before it is called stale.
#:
#: A bound, not a timeout — nothing here cancels anything. It exists so a worker
#: killed mid-read (an evicted pod, an expired lease, a `kubectl delete`) cannot
#: leave a row that makes the activity screen poll the single SQLite writer forever.
#:
#: Fifteen minutes, chosen against the two real durations either side of it:
#: `vision_openai_compat.DEFAULT_TIMEOUT` is 300 s for one call, and the dispatch
#: queue's lease is 1800 s. So this is generous enough that a slow read behind a
#: model swap is never mistaken for a dead one, and short enough that a dead one
#: stops being watched well inside the lease that will retry it.
STALE_AFTER_SECONDS = 900


class RunError(RuntimeError):
    """A refusal the routes map to an HTTP status. Mirrors `chat.ChatError`."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _clip(text: str | None) -> tuple[str | None, bool]:
    """`text` inside the bound, and whether it had to be cut."""
    if text is None or len(text) <= MAX_TRANSCRIPT_CHARS:
        return text, False
    return text[:MAX_TRANSCRIPT_CHARS], True


def in_flight_at(
    started_at: datetime, finished_at: datetime | None, *, now: datetime | None = None
) -> bool:
    """Is this run still expected to produce an answer?

    Takes the two timestamps rather than a row, so the wire type can compute it from
    the same rule the service does with no second definition to drift.

    **The screen must stop polling when this is false everywhere.** A dashboard
    polling the one writer forever is a slow leak, and an invisible one — every
    individual request is cheap, and the cost only shows up as contention on
    somebody else's put-away.
    """
    if finished_at is not None:
        return False
    return (now or utcnow()) - started_at < timedelta(seconds=STALE_AFTER_SECONDS)


def record(
    session: Session,
    *,
    kind: ModelRunKind,
    provider: str,
    model: str,
    intake_id: int | None = None,
    document_sha256: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    finish_reason: str | None = None,
    request_json: str | None = None,
    response_text: str | None = None,
    error: str | None = None,
) -> ModelRun:
    """Store one call. Always inserts.

    **Deliberately not idempotent, and this is the one place in the pipeline where
    that is right.** `dispatch.record_result` is keyed on `(intake_id, mpn)` so a
    re-read overwrites, because a second opinion about one photograph replaces the
    first. A run is the opposite kind of fact: *two calls happened*, and a second
    row is the only way to see that the first one failed and the retry succeeded —
    or that both failed the same way, which is what says the problem is not
    transient. Collapsing them would erase the retry history that makes
    `dispatch_attempts` legible.

    Every count stays exactly as passed. Nothing here substitutes 0 for `None`:
    see the columns' own note, and `CallStats` before it.
    """
    clipped_request, request_cut = _clip(request_json)
    clipped_response, response_cut = _clip(response_text)
    run = ModelRun(
        kind=kind,
        provider=provider,
        model=model,
        intake_id=intake_id,
        document_sha256=document_sha256,
        started_at=started_at or utcnow(),
        finished_at=finished_at,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        request_json=clipped_request,
        response_text=clipped_response,
        error=error,
        truncated=request_cut or response_cut,
    )
    session.add(run)
    session.flush()
    return run


def progress(session: Session, *, run: ModelRun, response_text: str) -> ModelRun:
    """Replace the partial answer on a run that is still going.

    **Replaces rather than appends, and the caller sends the whole accumulated
    text.** An append endpoint would have to be exactly-once to be correct: a
    retried POST or two frames delivered out of order would duplicate or interleave a
    fragment, producing a transcript the model never emitted — and a fabricated
    transcript is worse than no transcript, because the whole point of this table is
    that a reviewer can trust what it says was said. Replacement is idempotent, so
    the failure modes HTTP actually has cost a frame and nothing else.

    The bandwidth is affordable at the rate `progress.MIN_PROGRESS_INTERVAL` allows:
    one write a second, of a text bounded by `MAX_TRANSCRIPT_CHARS`.

    **Refuses a finished run.** A partial that arrives after the answer would
    overwrite the real completion with an earlier prefix of it. `finish` is terminal.
    """
    if run.finished_at is not None:
        raise RunError(
            "run_finished",
            f"run {run.id} finished at {run.finished_at.isoformat()}; "
            "a partial cannot overwrite a completed answer",
        )
    clipped, cut = _clip(response_text)
    run.response_text = clipped
    # Latched, never cleared: a later partial that happens to fit does not undo the
    # fact that an earlier one was cut. Same reason the column is stored rather than
    # derived from the length.
    run.truncated = run.truncated or cut
    session.flush()
    return run


def finish(
    session: Session,
    *,
    run: ModelRun,
    finished_at: datetime | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    finish_reason: str | None = None,
    response_text: str | None = None,
    error: str | None = None,
) -> ModelRun:
    """Settle a run: the final answer, what it cost, or what broke.

    **Idempotent by overwrite, deliberately not refused on a second call.** Unlike
    `progress`, finishing twice is a worker retrying a call it could not confirm, and
    the second body carries the same facts as the first. Refusing it would leave a
    finished read looking permanently in flight because a 200 got lost.

    `response_text` is `None` when the worker had nothing to report — a transport that
    raised before a completion existed. **The partial already on the row is then
    kept**, which is the whole reason a failed streamed read is worth watching: what
    arrived before the socket died is the evidence, and clearing it to NULL because
    the final answer is absent would throw away the only thing there is to read.

    Every count goes in exactly as passed. Nothing substitutes 0 for `None` — see
    the columns' own note and `CallStats` before it.
    """
    run.finished_at = finished_at or utcnow()
    run.latency_ms = latency_ms
    run.prompt_tokens = prompt_tokens
    run.completion_tokens = completion_tokens
    run.finish_reason = finish_reason
    run.error = error
    if response_text is not None:
        clipped, cut = _clip(response_text)
        run.response_text = clipped
        run.truncated = run.truncated or cut
    session.flush()
    return run


def runs_for(session: Session, *, intake_id: int) -> Sequence[ModelRun]:
    """Every run recorded against one parked scan, oldest first.

    Oldest first, unlike `dispatch.candidates_for`'s ranked order, because this is
    a *history* and a history read backwards is a history nobody follows. `id`
    breaks a tie on `started_at`: two runs inside one clock tick are ordered by
    insertion, which is the order they happened in.
    """
    return list(
        session.execute(
            select(ModelRun)
            .where(ModelRun.intake_id == intake_id)
            .order_by(ModelRun.started_at, ModelRun.id)
        )
        .scalars()
        .all()
    )
