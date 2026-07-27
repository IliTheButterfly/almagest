"""Column types.

Two decorators, both of which exist to keep the *stored* representation exactly
what `PLAN.md` specifies while giving Python something ergonomic to work with.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Dialect, String
from sqlalchemy.types import TypeDecorator

#: Fixed width, so lexicographic ordering in SQL is chronological ordering.
#: A variable-length representation would sort '2026-01-01T00:00:00Z' after
#: '2026-01-01T00:00:00.000001Z', which is wrong and would silently corrupt
#: every `ORDER BY ts` in the system.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC datetime, stored as ISO-8601 text.

    Text rather than a native type because SQLite has no date type and the
    design fixes the on-disk representation as ISO-8601 UTC. Naive datetimes
    are rejected on write rather than assumed to be UTC — silently guessing a
    timezone is how timestamps drift by hours.
    """

    impl = String(27)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime rejected; pass an aware datetime "
                "(app.models.types.utcnow() gives one)"
            )
        return value.astimezone(UTC).strftime(_ISO_FORMAT)

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        # Tolerant on read: a value may have been written by a SQL default or a
        # migration using a slightly different spelling. Anything ISO-8601 is
        # accepted, and a missing offset is taken as UTC because that is the
        # documented invariant for this column.
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


def utcnow() -> datetime:
    """Timezone-aware now, in UTC. The only sanctioned clock read."""
    return datetime.now(UTC)


class StrEnumType(TypeDecorator[str]):
    """A plain `VARCHAR` whose membership is validated in Python on write.

    Deliberately **not** `sa.Enum`, which emits `VARCHAR + CHECK`. SQLite
    cannot alter a `CHECK`, so that would make adding an enum member a full
    table rebuild instead of a one-line change.

    Validation is write-only on purpose. A value already in the database that
    this build does not recognise is returned as-is rather than raising: the
    whole point of avoiding `CHECK` is that the set of legal values may grow,
    and a reader that explodes on an unknown member would reintroduce exactly
    the rigidity the rule exists to avoid.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[StrEnum], length: int = 32, **kwargs: Any) -> None:
        self.enum_cls = enum_cls
        self._permitted = frozenset(member.value for member in enum_cls)
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        text = str(value)
        if text not in self._permitted:
            allowed = ", ".join(sorted(self._permitted))
            raise ValueError(
                f"{text!r} is not a valid {self.enum_cls.__name__}; expected one of: {allowed}"
            )
        return text
