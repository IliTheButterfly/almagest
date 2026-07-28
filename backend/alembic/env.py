"""Alembic environment.

Three things here are not the generated default and all of them matter on SQLite:

* ``render_as_batch=True`` — SQLite has no ``ALTER TABLE ... DROP/ALTER COLUMN``,
  so Alembic must rebuild the table. Without batch mode most schema changes are
  simply impossible to express.
* the URL comes from :mod:`app.config`, never from ``alembic.ini``. One source of
  truth for where the database lives, so ``alembic upgrade head`` and the running
  API can never disagree about which file they are pointing at.
* ``include_name`` hides the FTS5 tables. They cannot be SQLAlchemy models, so
  autogenerate would otherwise "helpfully" propose dropping them — see
  :data:`_FTS_TABLES`.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import Connection, String
from sqlalchemy.sql.schema import SchemaItem
from sqlalchemy.types import TypeDecorator

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

#: The FTS5 virtual tables, which exist only in migrations. They cannot be
#: expressed as models — a `CREATE VIRTUAL TABLE ... USING fts5(...)` has no
#: SQLAlchemy equivalent — and each one also brings a family of `_data`,
#: `_idx`, `_content`, `_docsize` and `_config` shadow tables that SQLite
#: manages itself. All of those reflect as perfectly ordinary tables, so
#: without this filter every `alembic check` would report drift and every
#: autogenerate would emit `op.drop_table("part_fts_data")`.
_FTS_TABLES = frozenset({"part_fts", "datasheet_fts"})


def _is_fts_owned(name: str) -> bool:
    return name in _FTS_TABLES or any(name.startswith(f"{fts}_") for fts in _FTS_TABLES)


def _include_name(
    name: str | None,
    type_: NameFilterType,
    parent_names: NameFilterParentNames,
) -> bool:
    """Keep FTS5 tables and their shadow tables out of the comparison.

    Filtering at reflection time rather than with `include_object` is
    deliberate: these tables must be invisible to autogenerate in *both*
    directions, and a name filter never even builds a `Table` for them.
    """
    if type_ == "table" and name is not None:
        return not _is_fts_owned(name)
    return True


def _render_item(
    type_: str,
    obj: SchemaItem | TypeDecorator[object],
    autogen_context: object,
) -> str | bool:
    """Render custom `TypeDecorator` columns as the plain type they store.

    Without this, autogenerate emits `app.models.types.StrEnumType(length=32)`
    — which is broken twice over. It drops the required `enum_cls` argument, so
    the migration raises `TypeError` on import; and it makes a migration depend
    on an application module, so renaming an enum later retroactively breaks a
    migration that already ran in production.

    Rendering `sa.String(length=32)` instead is also simply more honest: the
    database column *is* a VARCHAR. The enum is validated in Python at the
    model layer, and never as a `CHECK` constraint, precisely so that adding a
    member stays a one-line change rather than a SQLite table rebuild.
    """
    if type_ == "type" and isinstance(obj, TypeDecorator):
        impl = obj.impl
        if isinstance(impl, String):
            return f"sa.String(length={impl.length})" if impl.length else "sa.String()"
    return False


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
        render_item=_render_item,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=_render_item,
        include_name=_include_name,
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
