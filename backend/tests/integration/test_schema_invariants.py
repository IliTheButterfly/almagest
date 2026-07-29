"""Structural guards on the schema itself.

These assert properties of the *migrated database*, not of the models, because
that is where the properties actually have to hold. Each one corresponds to a
rule in CLAUDE.md that is cheap to state and expensive to retrofit once
violated.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models.catalog import Part
from tests.factories import make_location, make_part


def _table_sql(db: Session) -> dict[str, str]:
    rows = db.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL")
    ).all()
    return {row.name: row.sql for row in rows}


def test_no_check_constraints_anywhere(db: Session) -> None:
    """The single rule that keeps every deferred feature purely additive.

    SQLite cannot alter a `CHECK`, so a `CHECK` enum turns "add a new kind" into
    a full table rebuild. `sa.Enum` is the trap here — it silently emits
    `VARCHAR + CHECK`, so the violation looks like ordinary model code.
    """
    offenders = [
        name
        for name, sql in _table_sql(db).items()
        if "CHECK (" in sql.upper() or "CHECK(" in sql.upper()
    ]
    assert offenders == []


def test_enum_columns_are_plain_varchar(db: Session) -> None:
    enum_columns = [
        ("stock_ledger", "kind"),
        ("stock_ledger", "source"),
        ("stock_lots", "status"),
        ("container_types", "capacity_model"),
        ("container_types", "child_layout"),
        ("object_ids", "entity_type"),
        ("parameter_template", "value_type"),
        ("parameter_template", "substitution_direction"),
        ("parameter_value", "provenance"),
        # The candidate table's three: a new provider or a new review reason has
        # to stay a one-line change in `app.models.enums`, on a table that will
        # be holding every automated observation the system has ever made.
        ("parameter_value_candidate", "source"),
        ("parameter_value_candidate", "status"),
        ("parameter_value_candidate", "review_reason"),
        ("barcode_aliases", "entity_type"),
        ("barcode_aliases", "alias_kind"),
        ("scan_sources", "kind"),
        ("scan_events", "decoded_kind"),
        ("scan_events", "action_taken"),
        ("projects", "status"),
        ("project_builds", "status"),
        ("stock_allocations", "state"),
    ]
    for table, column in enum_columns:
        info = db.execute(text(f"PRAGMA table_info({table})")).all()
        declared = {row[1]: row[2] for row in info}
        assert column in declared, f"{table}.{column} is missing"
        assert declared[column].upper().startswith("VARCHAR"), (
            f"{table}.{column} is {declared[column]}, expected VARCHAR"
        )


def test_an_unknown_enum_value_already_in_the_database_still_reads(db: Session) -> None:
    """Adding an enum member must stay a one-line change.

    The point of refusing `CHECK` is that the legal set grows over time. This
    proves the other half of that promise: a row written by a *newer* build,
    carrying a `kind` this build has never heard of, loads without error
    instead of poisoning every query that touches the table.
    """
    part = make_part(db)
    location = make_location(db)
    db.execute(
        text(
            "INSERT INTO stock_lots (part_id, location_id, status, qty_milli_cached,"
            " qty_reserved_milli_cached, created_at, updated_at)"
            " VALUES (:p, :l, 'active', 0, 0, '2026-01-01T00:00:00.000000Z',"
            " '2026-01-01T00:00:00.000000Z')"
        ),
        {"p": part.id, "l": location.id},
    )
    lot_id = db.execute(text("SELECT id FROM stock_lots")).scalar_one()
    db.execute(
        text(
            "INSERT INTO stock_ledger (ts, lot_id, part_id, kind, delta_milli,"
            " qty_after_milli, source)"
            " VALUES ('2026-01-01T00:00:00.000000Z', :lot, :part, 'teleported', 5, 5, 'manual')"
        ),
        {"lot": lot_id, "part": part.id},
    )
    db.commit()

    kind = db.execute(text("SELECT kind FROM stock_ledger")).scalar_one()
    assert kind == "teleported"


def test_writing_an_unknown_enum_value_through_the_model_is_refused(db: Session) -> None:
    """The other direction: *this* build must not invent members."""
    from app.models.stock import StockLedger

    part = make_part(db)
    db.add(StockLedger(part_id=part.id, kind="teleported", delta_milli=1, qty_after_milli=1))
    # SQLAlchemy wraps the type's ValueError; the message survives, which is
    # what makes the failure diagnosable.
    with pytest.raises(StatementError, match="not a valid LedgerKind"):
        db.flush()
    db.rollback()


def test_foreign_keys_are_actually_enforced(db: Session) -> None:
    """`foreign_keys` is OFF by default in SQLite *and* per-connection, so this
    verifies the pragma reached the connection the ORM is using — not just that
    the constraints were declared."""
    db.add(Part(name="orphan", part_kind_id=999_999))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_quantities_are_integer_columns(db: Session) -> None:
    """Milli-units as INTEGER, so ledger sums stay exact forever. A REAL column
    here would accumulate float error across 200k additions."""
    info = {row[1]: row[2] for row in db.execute(text("PRAGMA table_info(stock_ledger)")).all()}
    assert info["delta_milli"].upper() == "INTEGER"
    assert info["qty_after_milli"].upper() == "INTEGER"

    lots = {row[1]: row[2] for row in db.execute(text("PRAGMA table_info(stock_lots)")).all()}
    assert lots["qty_milli_cached"].upper() == "INTEGER"
    assert lots["unit_cost_micro"].upper() == "INTEGER"


def test_ledger_seq_uses_autoincrement(db: Session) -> None:
    sql = _table_sql(db)["stock_ledger"]
    assert "AUTOINCREMENT" in sql.upper()


def test_parameter_value_uniqueness_is_present(db: Session) -> None:
    """Load-bearing: it guarantees each join contributes at most one row, so a
    multi-predicate parametric query never fans out into a cross product."""
    indexes = db.execute(text("PRAGMA index_list(parameter_value)")).all()
    unique_cols = set()
    for row in indexes:
        if row[2]:  # unique flag
            cols = db.execute(text(f"PRAGMA index_info('{row[1]}')")).all()
            unique_cols.add(tuple(sorted(c[2] for c in cols)))
    assert ("part_id", "template_id") in unique_cols


def test_reference_data_is_seeded(db: Session) -> None:
    """`parts.part_kind_id` is NOT NULL, so a database with no part_kinds could
    not hold a single part."""
    for table, minimum in [("part_kinds", 5), ("units", 4), ("packagings", 5), ("cache_state", 5)]:
        count = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        assert count >= minimum, f"{table} has {count} rows"


def test_a_part_needs_only_a_name_and_a_kind(db: Session) -> None:
    """The intake fast path depends on this. An unrecognised distributor label
    has to become a legal row in one tap, or the user abandons the scan — which
    is the failure mode that killed every abandoned system in this space."""
    part = make_part(db, name="mystery part from a salvage bin")
    db.commit()

    assert part.id is not None
    assert part.mpn is None
    assert part.category_id is None
    assert part.manufacturer_id is None


def test_locations_have_no_short_id_column(db: Session) -> None:
    """One shared ID space in `object_ids`, deliberately. A second copy here
    would be two sources of truth that can disagree, and absence of an
    `object_ids` row already expresses "this slot has no printed label"."""
    columns = {row[1] for row in db.execute(text("PRAGMA table_info(locations)")).all()}
    assert "short_id" not in columns


def test_tree_cache_columns_exist_on_both_trees(db: Session) -> None:
    """Physical storage and logical taxonomy are the same structure, so the
    mixin must have applied to both."""
    for table in ("locations", "part_categories"):
        columns = {row[1] for row in db.execute(text(f"PRAGMA table_info({table})")).all()}
        assert {"parent_id", "depth", "id_path", "label_path"} <= columns


def test_sibling_slot_labels_are_unique_but_only_where_set(db: Session) -> None:
    parent_id = make_location(db, name="Cabinet").id
    make_location(db, name="Drawer 1", parent_id=parent_id, slot_label="A1")
    db.commit()

    with pytest.raises(IntegrityError):
        make_location(db, name="Drawer 1 again", parent_id=parent_id, slot_label="A1")
    db.rollback()

    # The index is partial, so any number of siblings may carry no slot label —
    # a cabinet can hold loose containers alongside its numbered drawers.
    make_location(db, name="Loose box 1", parent_id=parent_id)
    make_location(db, name="Loose box 2", parent_id=parent_id)
    db.commit()
