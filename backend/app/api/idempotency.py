"""Server-side idempotency, shared by every write route.

`client_operations` has been in the schema since the first migration because
retry semantics are **not cheaply retrofittable**: they touch every write path,
so bolting them on later means revisiting all of them at once, after real data
has already been written by paths that did not have them. This module is the one
implementation, and no write route skips it.

The client-side hold-off in the scan resolver stops most duplicates before they
leave the device. It cannot stop the ones that already left: a phone on flaky
wifi retries a request whose response was lost, and one label held in front of a
camera can fire twice with a poor connection in between. Both must resolve to the
*same* ledger row — an append-only ledger has no way to take back a second
movement except by writing a third.

The contract is:

* **same key, same body** — replay the stored response, write nothing;
* **same key, different body** — refuse. Conflating this with a genuine retry
  would silently apply one of two different writes, and neither choice is
  defensible;
* **no key** — do the work. The caller has accepted at-least-once semantics,
  which is the honest reading of a request carrying no way to recognise its own
  retry.

`stock_ledger.client_op_id` is UNIQUE as a second, independent backstop: even if
this guard were bypassed, the database itself refuses to record the same keyed
movement twice.

`run` guards one request. A **batch** — a cart checkout, ADR 0007 — needs the
same contract one level down as well, because a batch may not fail as a whole
over one bad line and so a retry of it is genuinely partial. `replay_line` and
`record_line` give a line inside a batch its own key against the same table, and
they are deliberately here rather than in a route: two implementations of "has
this already been applied" is the thing this module exists to prevent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import ReplayableResponse
from app.models.enums import ClientOperationStatus
from app.models.system import ClientOperation


class LineIdempotencyError(Exception):
    """A per-line idempotency key that cannot be honoured.

    Raised instead of `HTTPException` because the caller is a **batch** route,
    and a batch may not fail as a whole over one bad line (ADR 0007): a cart
    checkout that 4xx'd because one of forty keys was reused would leave the
    client unable to say which row to fix. The batch converts this into that
    line's failure entry and keeps going, exactly as `stock.empty_bin` does with
    a `LedgerError`.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def request_digest(payload: BaseModel) -> str:
    """SHA-256 of the request body as Pydantic serialises it.

    Pydantic's JSON output is field-declaration ordered, so it is stable for the
    same model and the same values — which is all this needs, since a digest is
    only ever compared against another one computed the same way.
    """
    return hashlib.sha256(payload.model_dump_json().encode()).hexdigest()


def run[ResponseT: ReplayableResponse](
    session: Session,
    *,
    client_op_id: str | None,
    device_id: str | None,
    endpoint: str,
    payload: BaseModel,
    response_model: type[ResponseT],
    work: Callable[[], ResponseT],
) -> ResponseT:
    """Run one write at most once, and commit it.

    **Call this first in a handler, before staging anything else on the session**:
    the duplicate-key branch below rolls back, which is only safe while nothing
    else is pending.

    `work` does the whole write — ledger row and cached balance together — and
    this function owns the single `commit()` that makes it durable, so the two
    can never land in separate transactions.
    """
    if client_op_id is None:
        result = work()
        session.commit()
        return result

    digest = request_digest(payload)
    replay = _replay(session, client_op_id, endpoint=endpoint, digest=digest)
    if replay is not None:
        return _revive(response_model, replay)

    record = ClientOperation(
        client_op_id=client_op_id,
        device_id=device_id,
        endpoint=endpoint,
        request_hash=digest,
        status=ClientOperationStatus.IN_PROGRESS,
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        # Lost a race with a concurrent identical submit. SQLite serialises
        # writers, so the winner has committed by the time we get here and its
        # answer is on disk — hand that back rather than reporting a conflict
        # for what is exactly the duplicate this module exists to absorb.
        session.rollback()
        replay = _replay(session, client_op_id, endpoint=endpoint, digest=digest)
        if replay is None:
            raise
        return _revive(response_model, replay)

    result = work()
    record.status = ClientOperationStatus.COMPLETED
    record.response_json = result.model_dump_json()
    session.commit()
    return result


def replay_line[ResponseT: ReplayableResponse](
    session: Session,
    *,
    client_op_id: str | None,
    endpoint: str,
    payload: BaseModel,
    response_model: type[ResponseT],
) -> ResponseT | None:
    """The stored result of **one line** of a batch, or None if the line is new.

    `run` guards a whole request; this guards a line inside one, and both are
    needed for a cart checkout. `run` alone protects a retry that reuses the
    batch key — but a client that regenerated its batch key after a *partial*
    success (nineteen lines applied, one refused) would resubmit the nineteen as
    new work. The line key is what makes that resubmission a no-op for the lines
    that already landed, so the honest retry (send the whole cart again) is safe.

    Only *applied* lines are recorded, by `record_line`. A failed line has no row
    here on purpose: "the lot had moved" is a state the user can fix, and a
    retry must be allowed to succeed.

    Raises `LineIdempotencyError` rather than an `HTTPException` for the two
    cases that are not retries — see that class for why a batch must not 4xx.
    """
    if client_op_id is None:
        return None
    record = session.get(ClientOperation, client_op_id)
    if record is None:
        return None
    if record.endpoint != endpoint or record.request_hash != request_digest(payload):
        raise LineIdempotencyError(
            "client_op_id was already used for a different line; generate a new key",
            reason="request_mismatch",
        )
    if record.status != ClientOperationStatus.COMPLETED or record.response_json is None:
        raise LineIdempotencyError(
            "an earlier attempt with this client_op_id did not complete",
            reason="operation_in_flight",
        )
    return _revive(response_model, record.response_json)


def record_line(
    session: Session,
    *,
    client_op_id: str | None,
    device_id: str | None,
    endpoint: str,
    payload: BaseModel,
    result: ReplayableResponse,
) -> None:
    """Remember that this line was applied, so a resubmission replays it.

    Written `COMPLETED` in one go rather than `IN_PROGRESS` then updated: the
    line's own work is already staged on this session when this is called, and
    the enclosing `run` owns the single `commit()` that makes both durable
    together. There is therefore no window in which the row could claim a line
    landed that did not.
    """
    if client_op_id is None:
        return
    session.add(
        ClientOperation(
            client_op_id=client_op_id,
            device_id=device_id,
            endpoint=endpoint,
            request_hash=request_digest(payload),
            status=ClientOperationStatus.COMPLETED,
            response_json=result.model_dump_json(),
        )
    )
    session.flush()


def _replay(session: Session, client_op_id: str, *, endpoint: str, digest: str) -> str | None:
    """The stored response for this key, or None if this key is new.

    Raises rather than returning for the two cases that are not retries: a key
    reused with different content, and a key whose first run never finished.
    """
    record = session.get(ClientOperation, client_op_id)
    if record is None:
        return None

    if record.endpoint != endpoint or record.request_hash != digest:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "request_mismatch",
                "message": (
                    "client_op_id was already used for a different request; "
                    "generate a new key for a new operation"
                ),
                "endpoint": record.endpoint,
            },
        )

    if record.status != ClientOperationStatus.COMPLETED or record.response_json is None:
        # Only reachable if some write path committed midway through an
        # operation. None does today, so this is a bug signal rather than
        # ordinary contention — and retrying blindly could double-post the
        # half that did commit.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "operation_in_flight",
                "message": "an earlier attempt with this client_op_id did not complete",
            },
        )
    return record.response_json


def _revive[ResponseT: ReplayableResponse](
    response_model: type[ResponseT], stored: str
) -> ResponseT:
    return response_model.model_validate_json(stored).model_copy(update={"replayed": True})
