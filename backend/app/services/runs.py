"""Recording a model call, and reading one intake entry's runs back.

Two functions and a truncation rule. The interesting decisions are all in
`app.models.runs`; what is left here is the one thing a service has to own, which
is **what happens when a transcript is longer than the column**.

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
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ModelRunKind
from app.models.runs import MAX_TRANSCRIPT_CHARS, ModelRun
from app.models.types import utcnow


def _clip(text: str | None) -> tuple[str | None, bool]:
    """`text` inside the bound, and whether it had to be cut."""
    if text is None or len(text) <= MAX_TRANSCRIPT_CHARS:
        return text, False
    return text[:MAX_TRANSCRIPT_CHARS], True


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
