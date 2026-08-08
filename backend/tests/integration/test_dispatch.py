"""The capture-dispatch queue: the opt-in, the lease, and the two terminal states.

## What this file is really guarding

**A photograph queued for a model that nobody asked to spend.** This is the queue's
one structural difference from the two before it, and the only one whose failure is
invisible: `DispatchState` defaults to `NOT_REQUESTED`, so a phone syncing forty
labels must queue nothing at all. If that default ever flips — or if `_claimable`
ever grows a branch that matches it — the symptom is not an error, it is a GPU
quietly occupied by forty model runs on a card a co-tenant needed.

**An unreadable photograph reported as breakage.** ADR 0021 requires `UNIDENTIFIED`
to be distinguishable from `FAILED` for the reason `research` keeps `EXHAUSTED` apart
from a broken run: *"we could not tell what this is" is a photograph problem whose
fix is another photograph*. So the tests below pin that an empty candidate list
settles `unidentified` with **no error text**, and that a failure is a different
state reached by a different call.

**A model writing something only a barcode or a person may write.** Three columns are
off limits at any confidence — `mpn` (what the checksummed symbology said),
`resolved_part_id` (what a person decided) and `status` (whether the worklist is done
with it) — and `record_result` is asserted to leave all three exactly as it found
them. This is `CLAUDE.md`'s never-auto-accept rule as a test rather than as a
docstring.

**A vision confidence that could promote a field.** ADR 0021 measured 0.95 on a wrong
answer, so `MAX_VISION_CONFIDENCE` clamps below `candidates.AUTO_PROMOTE_CONFIDENCE`
and a test compares the two constants directly. Asserting the stored number alone
would pass if somebody raised the promotion threshold.

**A queue that stops moving while every count reads clean.** Identical to the other
two queues' failure mode and guarded the same way: the lease, the attempt counted *at
claim time*, and the abandoned-claim sweep are three halves of one mechanism.

## Why the concurrency test substitutes `_candidates`

Route handlers are `def`, so two concurrent claims are two threads on two connections
and a claim genuinely can land between the pick and the take. Nothing single-threaded
reaches that interleave, so the test hands `claim` a **stale** candidate list directly
— the same seam and the same reason as the extraction and research queues'
equivalents. Without it the compare-and-swap in the `update` is untested and free to
be tidied away by somebody who reads it as a redundant `where`.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.captures import Capture
from app.models.dispatch import IntakeIdentityCandidate
from app.models.documents import Document
from app.models.enums import DispatchState, DocumentKind, PendingIntakeStatus
from app.models.scanning import PendingIntake
from app.models.types import utcnow
from app.services import dispatch
from app.services.dispatch import CandidateReport, DispatchError
from app.services.enrichment.candidates import AUTO_PROMOTE_CONFIDENCE
from tests.factories import make_part


def _document(db: Session, body: bytes = b"label") -> Document:
    """A `documents` row standing in for a stored image.

    Written directly rather than through `POST /api/documents` because these are
    service-level tests with no client: what the queue needs from a document is a
    sha256 and an id, and the upload route's magic-byte check is `test_captures`'
    business.
    """
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
    return document


def _capture(db: Session, body: bytes = b"label") -> Capture:
    capture = Capture(document_id=_document(db, body).id, width_px=1600, height_px=1200)
    db.add(capture)
    db.flush()
    return capture


def _entry(db: Session, *, with_capture: bool = True, op_id: str = "op-1") -> PendingIntake:
    entry = PendingIntake(
        client_op_id=op_id,
        raw_payload=f"capture:{op_id}",
        capture_id=_capture(db, op_id.encode()).id if with_capture else None,
        status=PendingIntakeStatus.PENDING,
    )
    db.add(entry)
    db.flush()
    return entry


def _read(mpn: str = "CF14JT100K", **kwargs: object) -> CandidateReport:
    body: dict[str, object] = {
        "mpn": mpn,
        "confidence": 0.7,
        "source_text": f"MFR PART NO: {mpn}",
    }
    body.update(kwargs)
    return CandidateReport(**body)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The opt-in — the difference from every other queue here
# ---------------------------------------------------------------------------


def test_a_parked_scan_is_not_queued_for_a_model(db: Session) -> None:
    """The default, and the only one of these tests whose failure is silent.

    A photograph nobody asked about must cost nothing. See the module docstring.
    """
    entry = _entry(db)
    assert entry.dispatch_state == DispatchState.NOT_REQUESTED
    assert dispatch.claim(db, worker_id="w1") == []


def test_forty_synced_labels_queue_nothing(db: Session) -> None:
    """The scenario the default exists for, spelled out.

    A phone that syncs a box of scans must not thereby have requested forty GPU
    handovers. Written as its own test rather than folded into the one above because
    the thing being guarded is a *fleet* property: a single-entry assertion would
    still pass if some batch path enqueued on insert.
    """
    for index in range(40):
        _entry(db, op_id=f"batch-{index}")
    assert dispatch.status_counts(db)[DispatchState.NOT_REQUESTED] == 40
    assert dispatch.status_counts(db)[DispatchState.PENDING] == 0
    assert dispatch.claim(db, worker_id="w1", limit=50) == []


def test_requesting_puts_one_entry_in_the_queue(db: Session) -> None:
    wanted = _entry(db, op_id="wanted")
    _entry(db, op_id="ignored")

    assert dispatch.request(db, entry=wanted) == DispatchState.PENDING
    claimed = dispatch.claim(db, worker_id="w1", limit=10)
    assert [row.id for row in claimed] == [wanted.id]


def test_requesting_twice_does_not_reset_a_live_lease(db: Session) -> None:
    """The second press of a button that appeared to do nothing must be harmless."""
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    assert entry.dispatch_state == DispatchState.CLAIMED
    assert entry.dispatch_attempts == 1
    held_by, held_at = entry.dispatch_claimed_by, entry.dispatch_claimed_at

    assert dispatch.request(db, entry=entry) == DispatchState.CLAIMED
    assert entry.dispatch_attempts == 1
    assert (entry.dispatch_claimed_by, entry.dispatch_claimed_at) == (held_by, held_at)


def test_requesting_a_settled_entry_re_reads_it_from_zero(db: Session) -> None:
    """`request` is the requeue verb too — ADR 0021's re-read-after-a-model-swap path."""
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    dispatch.record_result(db, entry=entry, candidates=[])
    assert entry.dispatch_state == DispatchState.UNIDENTIFIED

    assert dispatch.request(db, entry=entry) == DispatchState.PENDING
    assert entry.dispatch_attempts == 0
    assert [row.id for row in dispatch.claim(db, worker_id="w2")] == [entry.id]


def test_an_entry_with_no_photograph_cannot_be_dispatched(db: Session) -> None:
    """Refused at the door rather than by a worker burning an attempt on it.

    A scan parked from a barcode alone is a perfectly ordinary entry — most of them
    are — and simply not this queue's business.
    """
    entry = _entry(db, with_capture=False)
    with pytest.raises(DispatchError) as raised:
        dispatch.request(db, entry=entry)
    assert raised.value.reason == dispatch.NO_CAPTURE
    assert entry.dispatch_state == DispatchState.NOT_REQUESTED


def test_deleting_the_photograph_takes_the_entry_out_of_the_queue(db: Session) -> None:
    """The other half of "there is nothing to look at", and the reason there is no
    separate `capture_missing` refusal.

    `capture_id` is `SET NULL` on delete, so deleting a blurry photograph does not
    orphan the id — it clears it, and the entry becomes one that cannot be dispatched
    for the ordinary reason. A dangling `capture_id` is therefore unreachable while
    foreign keys are enforced, which is why `dispatch.py` does not check for one.
    """
    entry = _entry(db)
    capture = db.get(Capture, entry.capture_id)
    assert capture is not None
    db.delete(capture)
    db.flush()
    db.refresh(entry)

    assert entry.capture_id is None
    assert not dispatch.dispatchable(entry)
    with pytest.raises(DispatchError) as raised:
        dispatch.request(db, entry=entry)
    assert raised.value.reason == dispatch.NO_CAPTURE


def test_cancelling_takes_it_back_out_and_keeps_what_was_read(db: Session) -> None:
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    dispatch.record_result(db, entry=entry, candidates=[_read()])

    assert dispatch.cancel(db, entry=entry) == DispatchState.NOT_REQUESTED
    assert dispatch.claim(db, worker_id="w2") == []
    # The record of what was already read survives, exactly as `research.requeue`
    # leaves its candidates: it is what a person consults to decide whether a re-read
    # is worth another handover.
    assert [row.mpn for row in dispatch.candidates_for(db, intake_id=entry.id)] == ["CF14JT100K"]


# ---------------------------------------------------------------------------
# The two terminal states
# ---------------------------------------------------------------------------


def test_reading_nothing_is_unidentified_and_carries_no_error(db: Session) -> None:
    """ADR 0021's requirement, and the reason this enum has six members.

    A blurred label is not a bug report. An error message beside it would put it in a
    health check that exists to surface real breakage, and that check then fills with
    photographs nothing is wrong with.
    """
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")

    assert dispatch.record_result(db, entry=entry, candidates=[]) == DispatchState.UNIDENTIFIED
    assert entry.dispatch_error is None
    assert entry.dispatch_claimed_by is None
    assert dispatch.candidates_for(db, intake_id=entry.id) == []


def test_a_broken_run_is_failed_and_says_why(db: Session) -> None:
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    dispatch.record_failure(db, entry=entry, error="model server refused the connection")
    # One attempt left, so the queue offers it again rather than giving up.
    assert entry.dispatch_state == DispatchState.PENDING
    assert entry.dispatch_error == "model server refused the connection"

    dispatch.claim(db, worker_id="w2")
    dispatch.record_failure(db, entry=entry, error="again")
    assert entry.dispatch_state == DispatchState.FAILED
    assert dispatch.claim(db, worker_id="w3") == []


def test_unidentified_and_failed_are_counted_apart(db: Session) -> None:
    """The whole point of the split, as a health check would see it."""
    unreadable = _entry(db, op_id="blurred")
    broken = _entry(db, op_id="broken")
    for entry in (unreadable, broken):
        dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1", limit=2)
    dispatch.record_result(db, entry=unreadable, candidates=[])
    dispatch.claim(db, worker_id="w1", limit=2)
    dispatch.record_failure(db, entry=broken, error="boom")
    dispatch.claim(db, worker_id="w1", limit=2)
    dispatch.record_failure(db, entry=broken, error="boom again")

    counts = dispatch.status_counts(db)
    assert counts[DispatchState.UNIDENTIFIED] == 1
    assert counts[DispatchState.FAILED] == 1


def test_two_attempts_and_the_abandoned_sweep_walks_an_entry_to_failed(db: Session) -> None:
    """A worker that dies twice must not leave an entry claimed by nobody.

    `MAX_DISPATCH_ATTEMPTS` is two rather than three because each attempt costs a GPU
    handover — so this walk is one step shorter than research's, and the sweep is what
    ends it.
    """
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    start = utcnow()

    # Two claims, neither reporting. The attempt is counted when the lease is granted,
    # so an entry that kills whatever picks it up runs out on its own.
    #
    # Each claim is placed strictly *more* than one lease after the previous one:
    # `_claimable` compares `dispatch_claimed_at < cutoff`, so a moment exactly one
    # lease later is not yet expired. The `+ 60` is what makes this test about the
    # attempt count rather than about a boundary.
    step = dispatch.LEASE_SECONDS + 60
    for index in range(dispatch.MAX_DISPATCH_ATTEMPTS):
        moment = start + timedelta(seconds=step * index)
        assert [row.id for row in dispatch.claim(db, worker_id="w1", now=moment)] == [entry.id]
    assert entry.dispatch_attempts == dispatch.MAX_DISPATCH_ATTEMPTS

    # The next claim sweeps it instead of granting it, and the sweep needs no
    # scheduler to have run.
    later = start + timedelta(seconds=step * dispatch.MAX_DISPATCH_ATTEMPTS)
    assert dispatch.claim(db, worker_id="w1", now=later) == []
    assert entry.dispatch_state == DispatchState.FAILED
    assert "abandoned" in (entry.dispatch_error or "")


def test_a_live_lease_is_not_handed_to_a_second_worker(db: Session) -> None:
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    assert [row.id for row in dispatch.claim(db, worker_id="w1")] == [entry.id]
    assert dispatch.claim(db, worker_id="w2") == []


def test_the_take_re_checks_what_the_pick_found(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compare-and-swap, driven through the seam a real race would use.

    Nothing single-threaded reaches the interleave, so `_candidates` is substituted to
    return an id another worker has already taken. Without this the repeated
    `_claimable` in the `update` reads as a redundant `where` and gets tidied away.
    """
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    assert [row.id for row in dispatch.claim(db, worker_id="w1")] == [entry.id]

    def stale(*_args: object, **_kwargs: object) -> list[int]:
        """The pick a concurrent claim has already invalidated."""
        return [entry.id]

    monkeypatch.setattr(dispatch, "_candidates", stale)
    assert dispatch.claim(db, worker_id="w2") == []


# ---------------------------------------------------------------------------
# What a model may write, and what it may never
# ---------------------------------------------------------------------------


def test_a_proposal_never_touches_the_barcode_the_person_or_the_worklist(db: Session) -> None:
    """The three columns that are off limits at any confidence.

    `mpn` is what the checksummed symbology said, `resolved_part_id` is what a person
    decided, and `status` is whether the worklist is done with the entry. A model
    writing any of them is `CLAUDE.md`'s never-auto-accept rule broken.
    """
    entry = _entry(db)
    entry.mpn = "CF14JT100K"
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")

    part = make_part(db, name="a stub the worker minted", is_stub=True)
    state = dispatch.record_result(
        db,
        entry=entry,
        candidates=[_read("CFI4JT100K", confidence=0.99, part_id=part.id)],
        label_kind="bag",
    )

    assert state == DispatchState.PROPOSED
    # The model read the OCR's wrong string. None of it reached the row's own fields.
    assert entry.mpn == "CF14JT100K"
    assert entry.resolved_part_id is None
    assert entry.status == PendingIntakeStatus.PENDING
    # The stub it minted is on the candidate, which is where a proposal lives.
    assert dispatch.candidates_for(db, intake_id=entry.id)[0].part_id == part.id


def test_a_vision_confidence_cannot_reach_the_promotion_threshold(db: Session) -> None:
    """ADR 0021 measured 0.95 on an answer that was the item's FCC ID.

    Compared against `AUTO_PROMOTE_CONFIDENCE` itself rather than against a literal:
    asserting `< 0.8` would still pass if somebody lowered the promotion bar to 0.7,
    which is exactly the change that would make an unclamped reading dangerous.
    """
    assert dispatch.MAX_VISION_CONFIDENCE < AUTO_PROMOTE_CONFIDENCE

    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    dispatch.record_result(db, entry=entry, candidates=[_read(confidence=0.95)])

    stored = dispatch.candidates_for(db, intake_id=entry.id)[0]
    assert stored.confidence < AUTO_PROMOTE_CONFIDENCE


def test_candidates_keep_their_order_and_their_losers(db: Session) -> None:
    """The losers are live options, not diagnostics — ADR 0021's mechanism.

    Ordering comes from `rank`, stored from the model's own ordering, which is why the
    clamp above can collapse ties in `confidence` without costing anything.
    """
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    dispatch.record_result(
        db,
        entry=entry,
        candidates=[
            _read("XB3-24Z8UM", confidence=0.95, rank=0),
            _read("XB3-24Z8UM-J", confidence=0.95, rank=1),
            _read("MCQ-XBEE3", confidence=0.6, rank=2),
        ],
    )
    rows = dispatch.candidates_for(db, intake_id=entry.id)
    assert [row.mpn for row in rows] == ["XB3-24Z8UM", "XB3-24Z8UM-J", "MCQ-XBEE3"]


def test_the_label_kind_survives_a_run_that_named_nothing(db: Session) -> None:
    """Which is why it is a column and not a candidate field.

    "This is a cut-tape strip" changes what a person expects the quantity to be, and
    it is worth knowing even when the part number was illegible.
    """
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    dispatch.record_result(db, entry=entry, candidates=[], label_kind="cut_tape")
    assert entry.dispatch_state == DispatchState.UNIDENTIFIED
    assert entry.dispatch_label_kind == "cut_tape"


# ---------------------------------------------------------------------------
# Idempotency, and the refusals
# ---------------------------------------------------------------------------


def test_re_reading_the_same_photograph_updates_in_place(db: Session) -> None:
    """Keyed on `(intake_id, mpn)`, so a retry lands the same rows.

    Asserted on the fields that a naive implementation gets *wrong* — the row count and
    the overwritten values — rather than only on the ones that are idempotent whatever
    happens. A test that checked just the state would pass against an implementation
    that appended a second candidate row on every submission.
    """
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")

    first = [_read("CF14JT100K", confidence=0.6, note="creased"), _read("CF14JT101K", rank=1)]
    dispatch.record_result(db, entry=entry, candidates=first, label_kind="bag")
    dispatch.record_result(db, entry=entry, candidates=first, label_kind="bag")

    rows = db.query(IntakeIdentityCandidate).all()
    assert len(rows) == 2

    # A genuine re-run is a fresh opinion about the same photograph and overwrites.
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w2")
    dispatch.record_result(
        db,
        entry=entry,
        candidates=[_read("CF14JT100K", confidence=0.75, note="second look, sharper")],
        label_kind="reel",
    )
    updated = dispatch.candidates_for(db, intake_id=entry.id)
    assert len(db.query(IntakeIdentityCandidate).all()) == 2
    assert updated[0].note == "second look, sharper"
    assert updated[0].confidence == pytest.approx(0.75)
    assert entry.dispatch_label_kind == "reel"


def test_a_candidate_that_quotes_nothing_is_refused(db: Session) -> None:
    """The load-bearing refusal. If the model cannot quote what it read, it did not.

    ADR 0021 records this catching an invented reading: the wrong answer quoted
    `'MODEL: MCQ-XBEE3'`, a line that is not on the label. A candidate with no quote is
    not a weak proposal, it is an unreviewable one.
    """
    entry = _entry(db)
    with pytest.raises(DispatchError) as raised:
        dispatch.record_result(db, entry=entry, candidates=[_read(source_text="   ")])
    assert raised.value.reason == "missing_source_text"


def test_the_same_part_number_twice_in_one_run_is_refused(db: Session) -> None:
    entry = _entry(db)
    with pytest.raises(DispatchError) as raised:
        dispatch.record_result(db, entry=entry, candidates=[_read(), _read()])
    assert raised.value.reason == "duplicate_candidate"


def test_a_confidence_outside_zero_to_one_is_refused(db: Session) -> None:
    """`vision._confidence` already normalises a percentage, so a value arriving out of
    range means the submission did not come through that parser at all."""
    entry = _entry(db)
    with pytest.raises(DispatchError) as raised:
        dispatch.record_result(db, entry=entry, candidates=[_read(confidence=95.0)])
    assert raised.value.reason == "invalid_confidence"


def test_too_many_candidates_is_refused(db: Session) -> None:
    entry = _entry(db)
    many = [_read(f"MPN-{index}") for index in range(dispatch.MAX_SUBMITTED_CANDIDATES + 1)]
    with pytest.raises(DispatchError) as raised:
        dispatch.record_result(db, entry=entry, candidates=many)
    assert raised.value.reason == "too_many_candidates"


def test_deleting_an_entry_takes_its_candidates(db: Session) -> None:
    """`CASCADE`: a proposal is meaningless without the photograph it describes."""
    entry = _entry(db)
    dispatch.request(db, entry=entry)
    dispatch.claim(db, worker_id="w1")
    dispatch.record_result(db, entry=entry, candidates=[_read()])
    db.delete(entry)
    db.flush()
    assert db.query(IntakeIdentityCandidate).all() == []
