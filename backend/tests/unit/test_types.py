from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.types import TypeDecorator

from app.models.enums import LedgerKind
from app.models.types import StrEnumType, UtcDateTime, utcnow


class _Dialect:
    """Stand-in — neither decorator consults the dialect."""


def _bind(type_: UtcDateTime | StrEnumType, value: object) -> object:
    return type_.process_bind_param(value, _Dialect())  # type: ignore[arg-type]


def test_naive_datetime_is_rejected() -> None:
    """Assuming UTC for a naive datetime is how timestamps silently drift by
    hours. Better to refuse at the boundary."""
    with pytest.raises(ValueError, match="naive"):
        _bind(UtcDateTime(), datetime(2026, 7, 27, 12, 0, 0))


def test_aware_datetime_round_trips_in_utc() -> None:
    original = datetime(2026, 7, 27, 12, 34, 56, 123456, tzinfo=UTC)
    stored = _bind(UtcDateTime(), original)
    assert UtcDateTime().process_result_value(stored, _Dialect()) == original  # type: ignore[arg-type]


def test_non_utc_input_is_converted_not_rejected() -> None:
    local = datetime(2026, 7, 27, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    stored = _bind(UtcDateTime(), local)
    assert stored == "2026-07-27T12:00:00.000000Z"


def test_stored_form_is_fixed_width_so_sql_sorting_is_chronological() -> None:
    """Lexicographic order must equal chronological order — otherwise every
    `ORDER BY ts` in the system is subtly wrong."""
    earlier = _bind(UtcDateTime(), datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=UTC))
    later = _bind(UtcDateTime(), datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC))
    assert isinstance(earlier, str) and isinstance(later, str)
    assert len(earlier) == len(later)
    assert earlier < later


def test_utcnow_is_aware() -> None:
    assert utcnow().tzinfo is not None


def test_str_enum_type_accepts_members() -> None:
    column = StrEnumType(LedgerKind)
    assert _bind(column, LedgerKind.RECEIVE) == "receive"
    assert _bind(column, "consume") == "consume"


def test_str_enum_type_rejects_non_members() -> None:
    with pytest.raises(ValueError, match="not a valid LedgerKind"):
        _bind(StrEnumType(LedgerKind), "teleport")


def test_str_enum_type_passes_none_through() -> None:
    assert _bind(StrEnumType(LedgerKind), None) is None


def test_no_result_processing_is_declared() -> None:
    """Validation is write-side only, on purpose.

    The reason for avoiding `CHECK` is that the legal set may grow, so a reader
    that raised on an unknown member would reintroduce exactly the rigidity the
    rule exists to avoid. Not overriding `process_result_value` means
    SQLAlchemy passes values straight through from the underlying `String`.
    The end-to-end behaviour is asserted against a real database in
    `tests/integration/test_schema_invariants.py`.
    """
    assert type(StrEnumType(LedgerKind)).process_result_value is TypeDecorator.process_result_value


def test_enum_members_compare_equal_to_stored_strings() -> None:
    """`StrEnum` members *are* `str`, which is why columns can be plain
    VARCHAR and comparisons still read naturally in application code."""
    assert LedgerKind.SPLIT_OUT == "split_out"
    assert "split_out" in set(LedgerKind)
