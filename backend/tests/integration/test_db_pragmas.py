"""Guards on the SQLite build and per-connection pragmas.

These look trivial and are not. `foreign_keys` is OFF by default in SQLite and
is per-connection, so a dropped pragma silently disables referential integrity
across the whole schema with no error anywhere. FTS5 is a compile-time option;
the parametric search design assumes it exists.
"""

from __future__ import annotations

from sqlalchemy import Engine, text


def test_foreign_keys_are_enforced(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_journal_mode_is_wal(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"


def test_busy_timeout_is_set(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000


def test_fts5_is_available(engine: Engine) -> None:
    """part_fts and datasheet_fts require FTS5 compiled into the sqlite3 build."""
    with engine.connect() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)"))
        conn.execute(text("DROP TABLE _fts5_probe"))
