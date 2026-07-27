"""Settings, cache bookkeeping and write idempotency."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin
from app.models.types import UtcDateTime, utcnow


class Setting(Base, TimestampMixin):
    """Runtime-tunable configuration.

    The assignment scorer's weights live here specifically so that tuning how
    put-away suggestions behave is not a deploy.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class CacheState(Base):
    """Freshness and drift for each derived cache.

    Every cache in this schema — lot balances, tree paths, occupancy, reserved
    quantities — is fully reconstructible from its source of truth. This table
    records when each was last rebuilt and what the nightly check found, so
    drift is a visible number rather than a mystery.
    """

    __tablename__ = "cache_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    is_dirty: Mapped[bool] = mapped_column(Integer, nullable=False, default=False)
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Rows found disagreeing with a recomputation at the last check. Non-zero
    #: is a correctness alert, not a performance note.
    drift_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str | None] = mapped_column(Text)


class ClientOperation(Base):
    """Idempotency for every write path.

    Included from the very first migration because it is **not cheaply
    retrofittable**: retry semantics touch every write, so bolting it on later
    means revisiting all of them at once.

    A phone on flaky wifi double-submits; the resolver's 3-second hold-off
    stops most duplicates client-side, and this stops the rest server-side.
    """

    __tablename__ = "client_operations"

    client_op_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str | None] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Hash of the request body. A replay with the *same* key but a *different*
    #: body is a client bug, and conflating it with a genuine retry would apply
    #: the wrong write silently.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
