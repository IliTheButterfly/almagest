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
