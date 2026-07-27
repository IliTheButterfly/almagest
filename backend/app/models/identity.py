"""The shared short-ID space."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import EntityType
from app.models.types import StrEnumType, UtcDateTime, utcnow


class ObjectId(Base):
    """One short ID, pointing at one row of one table.

    **One ID space across every object type.** A scan therefore resolves with
    no context about what was scanned, and an object that gets reclassified
    keeps its printed label instead of invalidating it. Readability is
    recovered by rendering the type as a display prefix (`BIN 4K7T-92MQ`) that
    is never parsed and never stored as part of the code.

    `short_id` is the primary key, so a generator collision is *detected* and
    costs one retry rather than silently corrupting anything. At ~5x10^4
    objects over 32^7 the birthday probability is about 3.6%, so this will
    happen eventually and must be cheap.
    """

    __tablename__ = "object_ids"

    #: Crockford base32, 7 data symbols + 1 mod-37 check symbol. Stored
    #: unhyphenated and normalised; the `4K7T-92MQ` hyphen is display only.
    short_id: Mapped[str] = mapped_column(String(8), primary_key=True)

    entity_type: Mapped[str] = mapped_column(StrEnumType(EntityType), nullable=False)
    entity_pk: Mapped[int] = mapped_column(Integer, nullable=False)

    #: An object may accumulate several IDs — a relabelled bin, a legacy code
    #: kept resolvable. Exactly one is the one we print.
    is_primary: Mapped[bool] = mapped_column(Integer, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_object_ids_entity", "entity_type", "entity_pk"),)
