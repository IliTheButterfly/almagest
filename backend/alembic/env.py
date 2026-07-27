"""Alembic environment.

Two things here are not the generated default and both matter on SQLite:

* ``render_as_batch=True`` — SQLite has no ``ALTER TABLE ... DROP/ALTER COLUMN``,
  so Alembic must rebuild the table. Without batch mode most schema changes are
  simply impossible to express.
* the URL comes from :mod:`app.config`, never from ``alembic.ini``. One source of
  truth for where the database lives, so ``alembic upgrade head`` and the running
  API can never disagree about which file they are pointing at.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from app.config import get_settings
from app.db.base import Base
from app.db.session import create_db_engine

# Importing the models package registers every table on `Base.metadata`;
# autogenerate sees nothing without it.
import app.models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    # `-x url=...` wins, so tests and one-off scripts can retarget without env vars.
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return str(override)
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
