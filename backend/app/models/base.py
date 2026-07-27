"""Shared column mixins.

`TreeMixin` is the interesting one: both hierarchies in the system — physical
`locations` and logical `part_categories` — are the *same* structure, so it is
implemented once and parameterised by table rather than twice by copy-paste.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.models.types import UtcDateTime, utcnow

#: `id_path` is built from **numeric ids**, wrapped in separators at both ends:
#: `/1/5/12/`. Numeric so a rename never invalidates a prefix query, and
#: wrapped at both ends so all three queries the design needs are plain LIKEs
#: with no risk of matching a partial id (`/1/` must not match `/12/`):
#:
#:   subtree of X   ->  id_path LIKE X.id_path || '%'
#:   ancestors of X ->  X.id_path LIKE id_path || '%'
#:   cycle guard    ->  id_path LIKE '%/' || :moved_id || '/%'
PATH_SEP = "/"

#: Display-only separator for `label_path`. A name containing this string is
#: rendered as-is; the path is never parsed back apart, so that is cosmetic.
LABEL_SEP = " / "


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class TreeMixin:
    """Adjacency list plus a fully derived path cache.

    Not nested sets — a subtree move renumbers the table. Not a closure table —
    machinery with no payoff at ~10³ nodes when SQLite has recursive CTEs.

    `depth`, `id_path` and `label_path` are **100% reconstructible** from
    `parent_id` alone by one recursive CTE, which is the property that matters:
    a cache bug is a stale display, never data loss, and the escape hatch is a
    sub-second full rebuild.
    """

    if TYPE_CHECKING:
        # Supplied by the concrete mapped class. Declared here only so the
        # self-referential ForeignKey below can name its own table. Not a
        # ClassVar: `DeclarativeBase` already declares it as an instance
        # attribute, and narrowing it here would be an incompatible override.
        __tablename__: str

    @declared_attr
    @classmethod
    def parent_id(cls) -> Mapped[int | None]:
        # RESTRICT, not CASCADE: deleting a cabinet must never silently delete
        # the drawers inside it along with whatever they contain.
        return mapped_column(
            ForeignKey(f"{cls.__tablename__}.id", ondelete="RESTRICT"),
            nullable=True,
            index=True,
        )

    #: Root nodes are depth 0.
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Indexed because every subtree query is `id_path LIKE :prefix || '%'`,
    #: which is exactly the left-anchored pattern an index can serve.
    id_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="", index=True)
    label_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
