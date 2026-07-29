"""A `StationApi` double that emulates the one server behaviour tests depend on.

Unlike `FakeTagSource` this is **not shipped**: `--fake` runs the real API (start
`make run` beside it), because a fabricated container would put a location that
does not exist into a demo stream that looks exactly like a real one.

It emulates server-side idempotency — a repeated `client_op_id` replays the stored
answer and moves nothing — because that is the behaviour the session's retry logic
is built on, and a fake that always moved stock would let a broken retry path pass.
`tests/test_session_ledger.py` then proves the real API does the same thing over a
real socket, which is the only proof that matters.
"""

from __future__ import annotations

from typing import Any

from agent.api import (
    Action,
    ActionKind,
    ContainerView,
    LotView,
    Movement,
    TagResolution,
)

LOT = LotView(
    lot_id=41,
    part_id=7,
    qty_milli=250_000,
    qty_reserved_milli=0,
    status="active",
    batch_code="B-2026-07",
)

CONTAINER = ContainerView(
    location_id=12,
    name="A3",
    label_path="Lab / Cabinet 1 / A3",
    short_id="4K7T92M8",
    lots=(LOT,),
)

RESOLVED = TagResolution(status="resolved", matched_by="uid", location_id=12, disagreement=False)
UNKNOWN = TagResolution(status="unknown", matched_by="none", location_id=None, disagreement=False)


def applied(lot: LotView, action: Action) -> LotView:
    """The lot as the API would render it after `action`."""
    if action.kind is ActionKind.TAKE:
        qty = lot.qty_milli - action.qty_milli
    elif action.kind is ActionKind.ADD:
        qty = lot.qty_milli + action.qty_milli
    else:
        qty = action.qty_milli
    return LotView(
        lot_id=lot.lot_id,
        part_id=lot.part_id,
        qty_milli=qty,
        qty_reserved_milli=lot.qty_reserved_milli,
        status=lot.status,
        batch_code=lot.batch_code,
    )


class FakeStationApi:
    """Programmable, and records everything so a test can assert what was *not* sent."""

    def __init__(
        self,
        container: ContainerView = CONTAINER,
        *,
        resolution: TagResolution = RESOLVED,
    ) -> None:
        self.container = container
        self.resolution = resolution
        #: Set any of these to an exception instance to make that call raise once
        #: per attempt — how the tests exercise `ApiUnavailable` and `ApiError`
        #: without a network.
        self.fail_resolve: Exception | None = None
        self.fail_read: Exception | None = None
        self.fail_commit: Exception | None = None

        self.resolve_calls: list[tuple[str | None, str | None]] = []
        self.read_calls: list[int] = []
        #: Every commit *attempt*, including the ones set up to fail. Counting
        #: attempts rather than successes is what makes a retry storm visible.
        self.commit_attempts = 0
        #: Every commit that **reached** the API. The abort tests assert this is
        #: empty, which is the fake standing in for "the ledger is untouched".
        self.commits: list[dict[str, Any]] = []
        self._stored: dict[str, Movement] = {}
        self._next_seq = 100

    async def resolve_tag(self, *, tag_uid: str | None, ndef_url: str | None) -> TagResolution:
        self.resolve_calls.append((tag_uid, ndef_url))
        if self.fail_resolve is not None:
            raise self.fail_resolve
        return self.resolution

    async def read_container(self, location_id: int) -> ContainerView:
        self.read_calls.append(location_id)
        if self.fail_read is not None:
            raise self.fail_read
        return self.container

    async def commit(
        self, *, kind: ActionKind, lot_id: int, qty_milli: int, client_op_id: str
    ) -> Movement:
        self.commit_attempts += 1
        if self.fail_commit is not None:
            raise self.fail_commit
        stored = self._stored.get(client_op_id)
        if stored is not None:
            return Movement(seqs=stored.seqs, replayed=True, lot=stored.lot)

        self.commits.append(
            {
                "kind": str(kind),
                "lot_id": lot_id,
                "qty_milli": qty_milli,
                "client_op_id": client_op_id,
            }
        )
        lot = self.container.lot(lot_id)
        assert lot is not None, "a commit reached the API naming a lot it never announced"
        updated = applied(lot, Action(kind=kind, lot_id=lot_id, qty_milli=qty_milli))
        self.container = self.container.with_lot(updated)
        self._next_seq += 1
        movement = Movement(seqs=(self._next_seq,), replayed=False, lot=updated)
        self._stored[client_op_id] = movement
        return movement
