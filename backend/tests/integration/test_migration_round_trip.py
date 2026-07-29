"""Every migration downgrades, on a database that has data in it.

`make check-migrations` proves the *upgrade* path and catches model drift. Nothing
proved the downgrade path at all, and three migrations were broken — one of them on
an empty database, two only once real rows exist. That split is why both the
populated and the empty teardown are asserted here rather than just the cheaper one.

The shared cause was `op.batch_alter_table` on a table something else references.
Batch mode implements an unsupported ALTER by building `_alembic_tmp_<table>`,
copying rows over, dropping the original and renaming the copy into place — and on
`locations` and `container_types` both of those last two steps are landmines:

* dropping `container_types` trips `locations.container_type_id`'s `ON DELETE
  RESTRICT` as soon as one container uses one type, and
* renaming a table makes SQLite re-check every trigger body, so
  `trg_stock_ledger_dirty_occupancy` naming `main.locations` fails while the real
  `locations` is momentarily gone.

Both leave the schema wedged mid-rebuild with an `_alembic_tmp_` table behind. The
trigger one needs no data, but the FK ones do — so the fixture below puts a real
container using a real seeded type in place first, and without it two of these three
tests pass against the broken migrations.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _populate(db_file: Path) -> None:
    """One container using one seeded type — which is all the FK failures need.

    Written with raw SQL rather than the ORM on purpose: this test's subject is the
    migrations, and going through the models would couple a downgrade test to
    whatever the models happen to say at head.
    """
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = "2026-01-01T00:00:00Z"
        row = conn.execute("SELECT id FROM container_types WHERE slug = 'raaco-c8-30'").fetchone()
        assert row is not None, "the seed migration should have created raaco-c8-30"
        conn.execute(
            "INSERT INTO locations (name, container_type_id, row_span, col_span, sort_order,"
            " access_score, is_overfull, is_staging, depth, id_path, label_path,"
            " created_at, updated_at)"
            " VALUES ('Round-trip cabinet', ?, 1, 1, 0, 0.5, 0, 0, 0, '', '', ?, ?)",
            (row[0], now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _table_names(db_file: Path) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}
    finally:
        conn.close()


def _db_file(database_url: str) -> Path:
    return Path(database_url.removeprefix("sqlite+pysqlite:///"))


def test_downgrade_to_base_with_data_present(alembic_config: Config, database_url: str) -> None:
    """head -> base -> head, with rows in the tables the broken migrations touched.

    All three phases are asserted, because a half-fixed teardown can leave a
    database that tears down cleanly and then cannot be built back up.
    """
    db_file = _db_file(database_url)
    command.upgrade(alembic_config, "head")
    _populate(db_file)

    command.downgrade(alembic_config, "base")

    left = _table_names(db_file)
    # The wedged-mid-rebuild signature. Named explicitly because the failure mode
    # this test exists for does not raise on the way down — it raises on the way
    # back up, one revision later, long after the useful traceback.
    assert not [name for name in left if name.startswith("_alembic_tmp_")], (
        f"batch_alter_table left a temp table behind: {sorted(left)}"
    )
    assert left <= {"alembic_version", "sqlite_sequence"}, f"tables survived base: {sorted(left)}"

    command.upgrade(alembic_config, "head")
    assert "locations" in _table_names(db_file)


def test_seed_downgrade_keeps_types_that_are_in_use(
    alembic_config: Config, database_url: str
) -> None:
    """`4cada779f255`'s downgrade removes the unused seed types and keeps the rest.

    Deleting a type out from under a live container is what
    `locations.container_type_id`'s `ON DELETE RESTRICT` exists to prevent, and it is
    the right constraint — so un-seeding is best-effort by design. The two halves are
    asserted together because "kept everything" would also pass the first assertion
    alone, and that is not a working downgrade either.
    """
    db_file = _db_file(database_url)
    command.upgrade(alembic_config, "head")
    _populate(db_file)

    command.downgrade(alembic_config, "1de7ca6783c8")

    conn = sqlite3.connect(db_file)
    try:
        surviving = {row[0] for row in conn.execute("SELECT slug FROM container_types")}
    finally:
        conn.close()

    assert "raaco-c8-30" in surviving, "a type a real container uses must not be deleted"
    assert "raaco-c10-40" not in surviving, "unused seed types should have been removed"


def test_downgrade_to_base_on_an_empty_database(alembic_config: Config, database_url: str) -> None:
    """The same teardown with no rows at all.

    Kept alongside the populated case rather than replaced by it: this is the path
    CI and a fresh checkout take, and it should stay green independently of whatever
    the populated fixture happens to insert.
    """
    db_file = _db_file(database_url)
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    assert _table_names(db_file) <= {"alembic_version", "sqlite_sequence"}
