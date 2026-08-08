"""The nightly pass sweeps abandoned queue leases (ADR 0013's repair half).

## What this file is really guarding

**A queue that stopped moving while every count reads clean.** Each queue repairs
its own leases at the top of every `claim`, which is correct and depends on
somebody claiming. The dispatch queue is opt-in and has no scheduled worker, so
"the next claim will sweep it" can mean never — and an entry left `CLAIMED` by a
dead worker is not pending, not failed, not claimable, and absent from every count
that would show anything was wrong. The tests below drive exactly that: a lease
abandoned with **no further claim ever made**, repaired by the nightly pass alone.

**A repair that quietly decides an outcome.** The sweep may only move a claim whose
attempts are already spent, and only into that queue's failure state. A photograph
nobody could read must still reach `UNIDENTIFIED` by a worker reporting it — never
by a scheduled job — so one test pins that the sweep leaves a retryable claim
completely alone.

**A stalled queue folded into the drift signal.** Drift is a wrong number; a
stopped worker is not. `has_drift` and `has_stalled_leases` are separate fields
because a nightly Job that cannot tell them apart sends whoever reads it to the
wrong place.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import maintenance
from app.models.captures import Capture
from app.models.documents import Document
from app.models.enums import DispatchState, DocumentKind, PendingIntakeStatus
from app.models.scanning import PendingIntake
from app.models.types import utcnow
from app.services import dispatch
from tests.factories import make_part


def _photo(db: Session, body: bytes) -> Capture:
    data = b"\x89PNG\r\n\x1a\n" + body
    sha256 = hashlib.sha256(data).hexdigest()
    document = Document(
        sha256=sha256,
        kind=DocumentKind.PHOTO,
        media_type="image/png",
        byte_size=len(data),
        storage_path=f"{sha256[:2]}/{sha256}",
    )
    db.add(document)
    db.flush()
    capture = Capture(document_id=document.id, width_px=100, height_px=100)
    db.add(capture)
    db.flush()
    return capture


def _parked(db: Session, op_id: str = "op-1") -> PendingIntake:
    entry = PendingIntake(
        client_op_id=op_id,
        raw_payload=f"capture:{op_id}",
        capture_id=_photo(db, op_id.encode()).id,
        status=PendingIntakeStatus.PENDING,
    )
    db.add(entry)
    db.flush()
    return entry


def _abandon(db: Session, entry: PendingIntake, *, attempts: int) -> None:
    """Leave a claim behind exactly as a killed worker does: held, and long expired."""
    dispatch.request(db, entry=entry)
    entry.dispatch_state = DispatchState.CLAIMED
    entry.dispatch_claimed_at = utcnow() - timedelta(seconds=dispatch.LEASE_SECONDS * 4)
    entry.dispatch_claimed_by = "a worker that died"
    entry.dispatch_attempts = attempts
    db.flush()


def test_the_nightly_pass_repairs_a_lease_no_claim_will_ever_sweep(db: Session) -> None:
    """The case that motivated this, and the one the queue cannot fix by itself.

    Nothing claims. The queue's own `expire_abandoned` never runs, and without this
    pass the entry stays `CLAIMED` forever — invisible to the pending count, to the
    failure count, and to anybody looking at queue depth.
    """
    entry = _parked(db)
    _abandon(db, entry, attempts=dispatch.MAX_DISPATCH_ATTEMPTS)

    sweeps = {sweep.queue: sweep for sweep in maintenance.sweep_abandoned_leases(db)}
    db.refresh(entry)

    assert sweeps["dispatch"].failed == 1
    assert entry.dispatch_state == DispatchState.FAILED
    assert "abandoned" in (entry.dispatch_error or "")
    # And now it is in the count a health check reads, which is the whole point.
    assert dispatch.status_counts(db)[DispatchState.FAILED] == 1


def test_a_retryable_expired_lease_is_reported_and_not_touched(db: Session) -> None:
    """The sweep repairs; it does not decide.

    An expired lease with attempts left is already claimable, so there is nothing to
    repair. Moving it would be a scheduled job settling an outcome that belongs to a
    worker — and `UNIDENTIFIED` in particular must never be reached that way.
    """
    entry = _parked(db)
    _abandon(db, entry, attempts=dispatch.MAX_DISPATCH_ATTEMPTS - 1)

    sweeps = {sweep.queue: sweep for sweep in maintenance.sweep_abandoned_leases(db)}
    db.refresh(entry)

    assert sweeps["dispatch"].failed == 0
    assert sweeps["dispatch"].stalled == 1, "the signal that nothing is draining"
    assert entry.dispatch_state == DispatchState.CLAIMED
    assert entry.dispatch_error is None
    # Still claimable, which is why it needed no repair.
    assert [row.id for row in dispatch.claim(db, worker_id="late-worker")] == [entry.id]


def test_a_live_lease_is_left_alone(db: Session) -> None:
    """A worker that is still working must not have its claim collected."""
    entry = _parked(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="busy")

    sweeps = {sweep.queue: sweep for sweep in maintenance.sweep_abandoned_leases(db)}
    db.refresh(entry)

    assert (sweeps["dispatch"].failed, sweeps["dispatch"].stalled) == (0, 0)
    assert entry.dispatch_state == DispatchState.CLAIMED
    assert entry.dispatch_claimed_by == "busy"


def test_every_queue_is_swept_not_only_the_new_one(db: Session) -> None:
    """Three queues, one pass. Research and extraction have the same hole.

    Their workers drain routinely so it bites less often, but "less often" is not
    "never", and a sweep that covered only the queue whose author noticed would be
    the kind of asymmetry nobody remembers later.
    """
    sweeps = maintenance.sweep_abandoned_leases(db)
    assert [sweep.queue for sweep in sweeps] == ["extraction", "research", "dispatch"]
    assert all(sweep.is_clean for sweep in sweeps)


def test_a_research_lease_is_swept_too(db: Session) -> None:
    from app.models.enums import ResearchState
    from app.services import research

    part = make_part(db, "Abandoned part", mpn="ABC-123")
    part.research_state = ResearchState.CLAIMED
    part.research_claimed_at = utcnow() - timedelta(seconds=research.LEASE_SECONDS * 4)
    part.research_attempts = research.MAX_RESEARCH_ATTEMPTS
    db.flush()

    sweeps = {sweep.queue: sweep for sweep in maintenance.sweep_abandoned_leases(db)}
    db.refresh(part)
    assert sweeps["research"].failed == 1
    assert part.research_state == ResearchState.FAILED


def test_the_route_reports_the_sweep_apart_from_drift(client: TestClient, db: Session) -> None:
    """`has_drift` and `has_stalled_leases` are separate, deliberately.

    A wrong balance and a stopped worker are different problems with different
    fixes, and the nightly Job's exit code can only mean one thing.
    """
    entry = _parked(db)
    _abandon(db, entry, attempts=dispatch.MAX_DISPATCH_ATTEMPTS - 1)
    db.commit()

    response = client.post("/api/system/maintenance")
    assert response.status_code == 200, response.text
    body = response.json()

    queues = {sweep["queue"]: sweep for sweep in body["lease_sweeps"]}
    assert set(queues) == {"extraction", "research", "dispatch"}
    assert queues["dispatch"]["stalled"] == 1
    assert body["has_stalled_leases"] is True
    # A stalled lease is not drift, and must not be reported as it.
    assert body["has_drift"] is False


def test_the_route_repairs_through_the_wire(client: TestClient, db: Session) -> None:
    entry = _parked(db)
    _abandon(db, entry, attempts=dispatch.MAX_DISPATCH_ATTEMPTS)
    db.commit()

    body = client.post("/api/system/maintenance").json()
    queues = {sweep["queue"]: sweep for sweep in body["lease_sweeps"]}
    assert queues["dispatch"]["failed"] == 1
    assert body["has_stalled_leases"] is False

    db.expire_all()
    assert client.get("/api/dispatch/status").json()["failed"] == 1


def test_an_unidentified_entry_is_never_produced_by_the_sweep(db: Session) -> None:
    """The invariant a scheduled repair could most easily break.

    "We could not tell what this is" is a photograph problem whose fix is another
    photograph, and only a worker that actually looked may say it. A nightly job
    reaching that state would manufacture a diagnosis nobody made.
    """
    for index in range(3):
        entry = _parked(db, op_id=f"op-{index}")
        _abandon(db, entry, attempts=index)

    maintenance.sweep_abandoned_leases(db)
    counts = dispatch.status_counts(db)
    assert counts[DispatchState.UNIDENTIFIED] == 0
    assert counts[DispatchState.PROPOSED] == 0
