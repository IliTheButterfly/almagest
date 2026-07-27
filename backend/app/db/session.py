"""Engine, connection pragmas and the request-scoped session.

Synchronous SQLAlchemy on purpose. The datastore is SQLite on a ReadWriteOnce
volume with exactly one writer, so async buys nothing here and costs a second
set of idioms. FastAPI runs `def` handlers in a threadpool, which is the right
shape for a blocking driver.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

#: Applied to every new connection. `foreign_keys` is per-connection and OFF by
#: default in SQLite, so forgetting it silently disables referential integrity.
#: `journal_mode=WAL` persists in the database file, but is set anyway so a fresh
#: file is correct from its first connection.
_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("foreign_keys", "ON"),
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
)


def apply_pragmas(dbapi_connection: sqlite3.Connection) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for name, value in _PRAGMAS:
            cursor.execute(f"PRAGMA {name}={value}")
    finally:
        cursor.close()


def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        # The pool hands connections between threadpool workers; SQLite's own
        # same-thread check would reject that. Safety is provided by the pool
        # itself never lending one connection to two threads at once.
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        apply_pragmas(dbapi_connection)

    return engine


def _build_default_engine() -> Engine:
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_db_engine(settings.database_url)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_default_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency. One session per request, rolled back on error."""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_testing(database_url: str, data_dir: Path | None = None) -> Engine:
    """Point the process at a different database. Tests only."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
    _engine = create_db_engine(database_url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine
