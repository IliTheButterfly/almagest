"""Defrag suggestions — proposals a human can apply or dismiss.

Only `overfull` suggestions are actually *written* by this phase: capacity
occupancy rebuilds flagging a location over capacity
(`app.services.capacity.upsert_overfull_suggestion`), and auto-assignment's
defrag escalation (`app.services.assignment`). The other five kinds
(`merge_lots`, `merge_bins`, `promote_hot`, `demote_cold`, `retire_empty`) are
provisioned here — table, enum, move-plan shape — for a later nightly
full-warehouse defrag scan that is out of scope for this phase; see
`docs/PLAN.md`, "Capacity and auto-assignment".

Every suggestion carries a `move_plan_json`: an ordered list of ordinary ledger
moves (`lot_id`, `from_location_id`, `to_location_id`, `qty_milli`) that an
apply endpoint can replay verbatim. A defrag is therefore always fully
undoable — it is nothing more than the same `move`/`split_out`/`split_in`
ledger machinery every other relocation already uses, never a special-cased
write path.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, and_
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import LayoutSuggestionKind, LayoutSuggestionStatus
from app.models.types import StrEnumType, UtcDateTime, utcnow


class LayoutSuggestion(Base):
    """One defrag opportunity, pending until applied or dismissed.

    **Dismissals stick.** A generator re-run must resolve to the same pending
    row rather than a duplicate. There is deliberately *no* general-purpose
    `UNIQUE(kind, location_id, other_location_id, part_id)` index for this:
    most of those columns are nullable, and SQL treats every `NULL` as
    distinct from every other `NULL`, so a compound unique index would let
    exactly the NULL-heavy rows (like `overfull`, which only ever fills
    `location_id`) duplicate anyway — the opposite of what it looks like it
    guarantees. Correctness for de-duplication lives in the upsert helper
    (`app.services.capacity.upsert_overfull_suggestion`), which does a
    NULL-safe lookup before inserting. The partial index below is real,
    narrow, defence-in-depth for the one kind this phase actually writes,
    where the subject column is never NULL.
    """

    __tablename__ = "layout_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    kind: Mapped[str] = mapped_column(StrEnumType(LayoutSuggestionKind), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        StrEnumType(LayoutSuggestionStatus),
        nullable=False,
        default=LayoutSuggestionStatus.PENDING,
        server_default=LayoutSuggestionStatus.PENDING.value,
        index=True,
    )

    #: The primary subject: the overfull bin, the "merge into" target, the
    #: location a promoted/demoted part should move towards.
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), index=True
    )
    #: The second location in a two-location suggestion (`merge_bins`'s "merge
    #: from"). NULL for kinds that only ever name one location.
    other_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE")
    )
    #: Set when the suggestion is about one specific part (`merge_lots`,
    #: `promote_hot`, `demote_cold`). NULL for `overfull`, which is about a
    #: location regardless of what is in it.
    part_id: Mapped[int | None] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"))

    #: Whatever signal ranked this suggestion — an affinity score, a fill
    #: ratio, a hot_score. Free-floating on purpose: each kind uses a
    #: different number, and none of them are ever filtered on, only displayed.
    score: Mapped[float | None] = mapped_column(Float)

    #: Ordered ledger moves an apply endpoint replays verbatim. JSON, not a
    #: child table: a plan is written once, read once, and never queried by
    #: its contents. See `app.services.capacity.MoveStep`/`DefragPlan`.
    move_plan_json: Mapped[str | None] = mapped_column(Text)
    #: Free-form context for the UI ("2 of 20 hottest parts", "both under 40%
    #: full, affinity 0.8") that is display-only, never decision-relevant.
    detail_json: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    dismissed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    applied_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        Index(
            "uq_layout_suggestions_pending_overfull_location",
            "location_id",
            unique=True,
            sqlite_where=and_(
                status == LayoutSuggestionStatus.PENDING.value,
                kind == LayoutSuggestionKind.OVERFULL.value,
            ),
        ),
    )
