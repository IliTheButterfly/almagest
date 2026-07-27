"""The ledger is the spine. These tests are the load-bearing ones.

Everything here runs against a database built by the **real migration**, which
is the only reason the triggers exist at all — they are invisible to the
models, so `create_all()` would produce a schema where every one of these
tests passes vacuously.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.maintenance import check_lot_balance_drift, rebuild_lot_balances
from app.models.enums import LedgerKind
from app.models.stock import StockLedger
from tests.factories import make_location, make_lot, make_part, post


def test_update_is_rejected_by_trigger(db: Session) -> None:
    """A future 'just fix the typo' script must fail, loudly, at the database."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db))
    row = post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()

    with pytest.raises(IntegrityError, match="append-only"):
        db.execute(
            text("UPDATE stock_ledger SET delta_milli = 9999 WHERE seq = :s"),
            {"s": row.seq},
        )
    db.rollback()

    assert (
        db.execute(select(StockLedger.delta_milli).where(StockLedger.seq == row.seq)).scalar_one()
        == 1000
    )


def test_delete_is_rejected_by_trigger(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db))
    row = post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()

    with pytest.raises(IntegrityError, match="append-only"):
        db.execute(text("DELETE FROM stock_ledger WHERE seq = :s"), {"s": row.seq})
    db.rollback()

    assert db.execute(select(func.count()).select_from(StockLedger)).scalar_one() == 1


def test_undo_is_a_compensating_row_not_a_deletion(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db))
    original = post(db, lot, 5000, LedgerKind.RECEIVE)

    reversal = post(db, lot, -5000, LedgerKind.ADJUST, reversal_of_seq=original.seq)
    db.commit()

    assert lot.qty_milli_cached == 0
    # Both rows survive: the history says "this happened, then it was undone",
    # which is not the same statement as "this never happened".
    assert db.execute(select(func.count()).select_from(StockLedger)).scalar_one() == 2
    assert reversal.reversal_of_seq == original.seq


def test_seq_is_monotonic_and_never_reused(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db))
    seqs = [post(db, lot, 100).seq for _ in range(5)]
    db.commit()

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_cached_balance_matches_ledger_sum_after_random_movements(db: Session) -> None:
    """The invariant that makes it safe to never sum the ledger in an API path."""
    rng = random.Random(20260727)
    part = make_part(db)
    lots = [make_lot(db, part, make_location(db, name=f"Bin {i}")) for i in range(4)]

    for _ in range(300):
        lot = rng.choice(lots)
        delta = rng.randint(-500, 500)
        if delta == 0:
            continue
        post(db, lot, delta, LedgerKind.RECEIVE if delta > 0 else LedgerKind.CONSUME)
    db.commit()

    for lot in lots:
        ledger_sum = db.execute(
            select(func.coalesce(func.sum(StockLedger.delta_milli), 0)).where(
                StockLedger.lot_id == lot.id
            )
        ).scalar_one()
        assert lot.qty_milli_cached == ledger_sum

    assert check_lot_balance_drift(db).is_clean


def test_balances_may_go_negative(db: Session) -> None:
    """Not constrained non-negative on purpose: a bad recount has to surface as
    a dashboard anomaly, not block the write that records what happened."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db))
    post(db, lot, -250, LedgerKind.COUNT)
    db.commit()

    assert lot.qty_milli_cached == -250


def test_drift_is_detected_and_repairable(db: Session) -> None:
    part = make_part(db)
    lot = make_lot(db, part, make_location(db))
    post(db, lot, 1000, LedgerKind.RECEIVE)
    db.commit()

    # Simulate a write path that updated the cache without the ledger.
    lot.qty_milli_cached = 4242
    db.commit()

    report = check_lot_balance_drift(db)
    assert not report.is_clean
    assert report.drift_count == 1
    assert report.sample_ids == (lot.id,)

    rebuild_lot_balances(db)
    db.commit()
    db.refresh(lot)

    assert lot.qty_milli_cached == 1000
    assert check_lot_balance_drift(db).is_clean


def test_drift_check_records_its_findings(db: Session) -> None:
    """The nightly job needs somewhere to leave a number a human will see."""
    check_lot_balance_drift(db)
    db.commit()

    row = db.execute(
        text("SELECT last_checked_at, drift_count FROM cache_state WHERE name = 'lot_balances'")
    ).one()
    assert row.last_checked_at is not None
    assert row.drift_count == 0


def test_whole_lot_move_is_one_row_with_zero_delta(db: Session) -> None:
    """Move semantics: quantity is unchanged, place is not. Minting a new lot
    per shelf change would destroy lot identity and per-lot cost continuity."""
    part = make_part(db)
    source = make_location(db, name="Shelf A")
    destination = make_location(db, name="Shelf B")
    lot = make_lot(db, part, source)
    post(db, lot, 1000, LedgerKind.RECEIVE)

    row = post(
        db,
        lot,
        0,
        LedgerKind.MOVE,
        from_location_id=source.id,
        to_location_id=destination.id,
    )
    lot.location_id = destination.id
    db.commit()

    assert row.delta_milli == 0
    assert lot.qty_milli_cached == 1000
    assert lot.location_id == destination.id


def test_partial_move_is_two_rows_sharing_a_group(db: Session) -> None:
    part = make_part(db)
    source = make_location(db, name="Reel rack")
    destination = make_location(db, name="Bench bin")
    from_lot = make_lot(db, part, source)
    to_lot = make_lot(db, part, destination)
    post(db, from_lot, 5000, LedgerKind.RECEIVE)

    group = "3f8b1c22-0000-4000-8000-000000000001"
    post(db, from_lot, -1200, LedgerKind.SPLIT_OUT, group_uuid=group)
    post(db, to_lot, 1200, LedgerKind.SPLIT_IN, group_uuid=group)
    db.commit()

    assert from_lot.qty_milli_cached == 3800
    assert to_lot.qty_milli_cached == 1200

    paired = db.execute(
        select(func.count()).select_from(StockLedger).where(StockLedger.group_uuid == group)
    ).scalar_one()
    assert paired == 2

    # Conservation: the pair moves stock without creating or destroying any.
    total = db.execute(
        select(func.sum(StockLedger.delta_milli)).where(StockLedger.group_uuid == group)
    ).scalar_one()
    assert total == 0


def test_client_op_id_is_unique(db: Session) -> None:
    """Idempotency key. A retried request must not post a second movement."""
    part = make_part(db)
    lot = make_lot(db, part, make_location(db))
    post(db, lot, 100, client_op_id="dup-key")
    db.commit()

    with pytest.raises(Exception):  # noqa: B017 - IntegrityError under any driver
        post(db, lot, 100, client_op_id="dup-key")
        db.commit()
    db.rollback()


def test_lot_quantity_never_lives_on_the_part(db: Session) -> None:
    """Structural assertion: `parts` must have no quantity column at all.

    PartKeepr hung quantity on the part and could never support multi-location
    or per-batch cost. A well-meaning future migration adding `parts.quantity`
    would silently reintroduce that, so it is asserted rather than trusted.
    """
    columns = {row[1] for row in db.execute(text("PRAGMA table_info(parts)")).all()}
    forbidden = {"quantity", "qty", "qty_milli", "stock", "qty_milli_cached", "on_hand"}
    assert not (columns & forbidden)

    lot_columns = {row[1] for row in db.execute(text("PRAGMA table_info(stock_lots)")).all()}
    assert "qty_milli_cached" in lot_columns
