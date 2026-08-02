"""Lots and the append-only ledger — the spine of the system."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin
from app.models.enums import LedgerGroupKind, LedgerKind, LedgerSource, LotStatus
from app.models.types import StrEnumType, UtcDateTime, utcnow


class StockLot(Base, TimestampMixin):
    """A physical package of one part at one location.

    **Quantity lives here, never on `parts`.** PartKeepr hung it on the part
    and could never support multi-location or per-batch cost.

    Lots are packaging-aware: a 5000-piece reel and a cut-tape strip of the
    same MPN in the same bin are two lots, independently costed.
    """

    __tablename__ = "stock_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: Mutable. A whole-lot move rewrites this and appends **one** ledger row;
    #: the history lives in the ledger. Minting a new lot per shelf change
    #: would destroy lot identity and per-lot cost continuity.
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    packaging_id: Mapped[int | None] = mapped_column(
        ForeignKey("packagings.id", ondelete="SET NULL")
    )
    pack_nominal_qty_milli: Mapped[int | None] = mapped_column(Integer)

    batch_code: Mapped[str | None] = mapped_column(String(128), index=True)
    serial: Mapped[str | None] = mapped_column(String(128))
    date_code: Mapped[str | None] = mapped_column(String(32))

    #: No FK yet — `supplier_parts` arrives in Phase 5. Same pattern the design
    #: uses for `actor_id`: a plain nullable INTEGER now, promoted to a real FK
    #: with `batch_alter_table` when the referenced table exists. Adding the
    #: column later would be the expensive part; adding the constraint is not.
    supplier_part_id: Mapped[int | None] = mapped_column(Integer)
    unit_cost_micro: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))

    status: Mapped[str] = mapped_column(
        StrEnumType(LotStatus), nullable=False, default=LotStatus.ACTIVE, index=True
    )

    #: **Balances must be read from here, never by summing the ledger.**
    #: Summing 200k rows in an API path is how this design dies. Rebuildable in
    #: one statement, with a nightly drift check into `cache_state`.
    #:
    #: Deliberately *not* constrained non-negative: a bad recount has to
    #: surface as a dashboard anomaly, not block the ledger write that records
    #: what actually happened.
    qty_milli_cached: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Derived from `stock_allocations`, never hand-maintained. A hand-kept
    #: reservation counter drifts and cannot be reconstructed.
    qty_reserved_milli_cached: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    slots_occupied: Mapped[int | None] = mapped_column(Integer)
    volume_mm3_cached: Mapped[float | None] = mapped_column(Float)
    #: Cache of `locations.tare_mg`, which is the authoritative copy. A split
    #: forces a fresh tare capture rather than a computed one: a reel's tare is
    #: not a fraction of its parent's, because leader, trailer and splice tape
    #: are uneven.
    container_tare_mg: Mapped[int | None] = mapped_column(Integer)

    opened_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Solder paste, batteries, electrolytics.
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_stock_lots_part_location", "part_id", "location_id"),
        Index("ix_stock_lots_location_status", "location_id", "status"),
    )


class StockLedger(Base):
    """Append-only movement history. **Enforced by database triggers.**

    The triggers are not belt-and-braces. Convention alone guarantees that
    someone eventually writes a "just fix the typo" script, and a ledger that
    can be edited is not a ledger. `UPDATE` and `DELETE` both `RAISE(ABORT)`;
    the triggers live in the migration, so they exist in tests too.

    Undo is a **compensating row** with `reversal_of_seq` set, never a delete.
    """

    __tablename__ = "stock_ledger"

    #: SQLite reuses rowids unless AUTOINCREMENT is declared. Deletes are
    #: impossible here so reuse could not actually occur, but `seq` *is* the
    #: history's ordering, and guaranteed monotonicity is worth the one extra
    #: bookkeeping table SQLite keeps for it.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    ts: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow, index=True)

    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_lots.id", ondelete="RESTRICT"), index=True
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    kind: Mapped[str] = mapped_column(StrEnumType(LedgerKind), nullable=False, index=True)

    #: Signed. Zero for a whole-lot move, which changes place and not quantity.
    delta_milli: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The lot balance immediately after this row. Redundant with the running
    #: sum on purpose: it makes drift detectable per-row rather than only in
    #: aggregate, so a corrupted cache can be traced to the row that broke it.
    qty_after_milli: Mapped[int] = mapped_column(Integer, nullable=False)

    from_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT")
    )
    to_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT")
    )

    #: What a physical recount actually said, kept alongside the delta it
    #: implied, so a disputed count stays reconstructible.
    counted_qty_milli: Mapped[int | None] = mapped_column(Integer)
    measured_mass_mg: Mapped[int | None] = mapped_column(Integer)

    unit_cost_micro: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))

    #: Free-form link to whatever caused this: a build, an import batch, a
    #: defrag plan. Not an FK, because the referent table varies.
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[int | None] = mapped_column(Integer)

    #: Ties the two halves of a partial move (`split_out` -N, `split_in` +N)
    #: and every row of a bulk operation into one undoable unit.
    group_uuid: Mapped[str | None] = mapped_column(String(36), index=True)

    #: Why those rows are grouped, and therefore what undoing one of them means.
    #: See `LedgerGroupKind`. NULL — every row written before this column — reads
    #: as `ATOMIC`, which is the behaviour those rows were written under.
    group_kind: Mapped[str | None] = mapped_column(StrEnumType(LedgerGroupKind))

    #: Plain nullable INTEGER with **no FK clause**, exactly as the design
    #: specifies: multi-user is deferred, and the FK gets added via
    #: `batch_alter_table` when an `actors` table arrives. With one user, NULL
    #: unambiguously means the owner — but note that history written before
    #: this column is used is permanently unattributable.
    actor_id: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(
        StrEnumType(LedgerSource), nullable=False, default=LedgerSource.MANUAL
    )

    #: Points at the `seq` this row compensates for. Set on an undo.
    reversal_of_seq: Mapped[int | None] = mapped_column(Integer, index=True)

    #: Client-generated idempotency key, attached at scan time. A duplicate
    #: scan or a retried request resolves to the same row instead of a second
    #: movement.
    client_op_id: Mapped[str | None] = mapped_column(String(36), unique=True)

    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_stock_ledger_lot_seq", "lot_id", "seq"),
        Index("ix_stock_ledger_part_ts", "part_id", "ts"),
        # Guarantees `seq` never goes backwards. See the note on the column.
        {"sqlite_autoincrement": True},
    )
