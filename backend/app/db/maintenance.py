"""Cache rebuilds and drift checks.

Every derived value in this schema is reconstructible from its source of truth,
and this module is where that promise is kept. The property matters more than
it looks: it means a cache bug is a stale number a nightly job repairs, never
lost data, and it is what makes it safe to read balances from a cache instead
of computing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from app.models.types import utcnow

#: Correlated single-statement rebuild. Deliberately one statement rather than
#: a Python loop over lots: at 200k ledger rows the loop is the difference
#: between a sub-second maintenance job and one that needs a progress bar.
_REBUILD_LOT_BALANCES = text(
    """
    UPDATE stock_lots
    SET qty_milli_cached = COALESCE(
        (SELECT SUM(delta_milli) FROM stock_ledger WHERE stock_ledger.lot_id = stock_lots.id),
        0
    )
    """
)

_COUNT_LOT_BALANCE_DRIFT = text(
    """
    SELECT COUNT(*)
    FROM stock_lots AS l
    WHERE l.qty_milli_cached <> COALESCE(
        (SELECT SUM(delta_milli) FROM stock_ledger AS sl WHERE sl.lot_id = l.id),
        0
    )
    """
)

_DRIFTING_LOT_IDS = text(
    """
    SELECT l.id
    FROM stock_lots AS l
    WHERE l.qty_milli_cached <> COALESCE(
        (SELECT SUM(delta_milli) FROM stock_ledger AS sl WHERE sl.lot_id = l.id),
        0
    )
    ORDER BY l.id
    LIMIT :limit
    """
)

LOT_BALANCES = "lot_balances"


@dataclass(frozen=True)
class DriftReport:
    cache_name: str
    drift_count: int
    #: Bounded sample, so a systemic failure does not produce a 200k-row log line.
    sample_ids: tuple[int, ...]

    @property
    def is_clean(self) -> bool:
        return self.drift_count == 0


def rebuild_lot_balances(session: Session) -> int:
    """Recompute every `stock_lots.qty_milli_cached` from the ledger.

    Returns the number of lots touched. This is the escape hatch referenced
    throughout the design: whenever the cache is suspect, this restores it from
    the append-only record, which cannot itself have been edited.
    """
    # `Session.execute` is typed as returning `Result`, which does not declare
    # `rowcount`; a DML statement always yields a `CursorResult`, which does.
    result = cast(CursorResult[Any], session.execute(_REBUILD_LOT_BALANCES))
    _mark_rebuilt(session, LOT_BALANCES)
    return result.rowcount


def check_lot_balance_drift(session: Session, *, sample_limit: int = 20) -> DriftReport:
    """Compare cached balances against the ledger without changing anything.

    Run nightly. A non-zero result is a **correctness alert**, not a
    performance note: it means some write path updated the ledger and the cache
    inconsistently, and the numbers the UI has been showing were wrong.
    """
    drift_count = session.execute(_COUNT_LOT_BALANCE_DRIFT).scalar_one()
    sample: tuple[int, ...] = ()
    if drift_count:
        rows = session.execute(_DRIFTING_LOT_IDS, {"limit": sample_limit}).scalars().all()
        sample = tuple(rows)

    report = DriftReport(cache_name=LOT_BALANCES, drift_count=drift_count, sample_ids=sample)
    _record_check(session, report)
    return report


def _mark_rebuilt(session: Session, cache_name: str) -> None:
    session.execute(
        text("UPDATE cache_state SET is_dirty = 0, last_rebuilt_at = :now WHERE name = :name"),
        {"now": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "name": cache_name},
    )


def _record_check(session: Session, report: DriftReport) -> None:
    detail = ",".join(str(i) for i in report.sample_ids) if report.sample_ids else None
    session.execute(
        text(
            "UPDATE cache_state"
            " SET last_checked_at = :now, drift_count = :drift, detail = :detail"
            " WHERE name = :name"
        ),
        {
            "now": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "drift": report.drift_count,
            "detail": detail,
            "name": report.cache_name,
        },
    )
