"""The scanning tables and the FTS5 indexes.

Everything here runs against a database built by the **real migrations**, which
for this stage is not a stylistic preference: `part_fts`, `datasheet_fts` and the
three triggers that keep `part_fts` current exist *only* in a migration. They are
invisible to the models by necessity, so `create_all()` would produce a schema in
which every assertion below passes or fails for the wrong reason.

The column-set test is the load-bearing one. An FTS5 table cannot have columns
added or removed; changing the set means dropping the table and rebuilding the
index from scratch. That is why `param_digest` is created before anything writes
to it, and this test is what stops a future edit quietly "cleaning up" the unused
column.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory, reset_engine_for_testing
from app.models.enums import (
    AliasKind,
    EntityType,
    ScanAction,
    ScanDecodedKind,
    ScanSourceKind,
)
from app.models.scanning import BarcodeAlias, ScanEvent, ScanSource
from tests.factories import make_manufacturer, make_part

#: PLAN.md, "Other tables". Ordered, because FTS5 column filters
#: (`part_fts MATCH 'mpn:lm358'`) and any future `bm25()` weighting are both
#: positional.
EXPECTED_PART_FTS_COLUMNS = ["mpn", "description", "manufacturer", "keywords", "param_digest"]


def _columns(db: Session, table: str) -> list[str]:
    return [row[1] for row in db.execute(text(f"PRAGMA table_info({table})")).all()]


def _matches(db: Session, table: str, query: str) -> set[int]:
    rows = db.execute(
        text(f"SELECT rowid FROM {table} WHERE {table} MATCH :q"), {"q": query}
    ).scalars()
    return set(rows)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_part_fts_has_exactly_the_designed_column_set(db: Session) -> None:
    """Including `param_digest`, which nothing populates yet.

    Provisioning an unused column is normally a smell — for an ordinary table,
    adding one later is free. FTS5 inverts that: the column set is fixed at
    creation, so getting it right now is the difference between a future feature
    and a full reindex. Deleting the column because it "isn't used" is the
    mistake this test exists to catch.
    """
    assert _columns(db, "part_fts") == EXPECTED_PART_FTS_COLUMNS


def test_datasheet_fts_is_a_separate_single_column_index(db: Session) -> None:
    """Separate because one family datasheet covers dozens of MPNs, so folding
    its text into `part_fts` would store the same prose once per part."""
    assert _columns(db, "datasheet_fts") == ["text"]

    # No trigger can keep it current — the text arrives from an extraction
    # pipeline, and there is not even a `documents` table to trigger on yet — so
    # it must at least accept a write keyed by a future `documents.id`.
    db.execute(
        text("INSERT INTO datasheet_fts (rowid, text) VALUES (:id, :body)"),
        {"id": 4242, "body": "Absolute maximum ratings: supply voltage 36 V"},
    )
    assert _matches(db, "datasheet_fts", "ratings") == {4242}


def test_no_check_constraint_arrived_with_the_new_tables(db: Session) -> None:
    """Narrower twin of `test_schema_invariants`' global sweep, so a violation
    introduced here points at this stage instead of at the whole schema. Worth
    restating for the FTS tables in particular: they are `type = 'table'` in
    `sqlite_master` along with their shadow tables, so they are inside the reach
    of the rule and not a loophole in it."""
    rows = db.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL")
    ).all()
    new_surface = [
        (row.name, row.sql)
        for row in rows
        if row.name.startswith(("barcode_aliases", "scan_sources", "scan_events"))
        or "fts" in row.name
    ]
    # Guard against the filter above silently matching nothing.
    assert len(new_surface) >= 5

    offenders = [name for name, sql in new_surface if "CHECK" in sql.upper()]
    assert offenders == []


# ---------------------------------------------------------------------------
# The part_fts sync triggers
# ---------------------------------------------------------------------------


def test_insert_trigger_indexes_a_new_part(db: Session) -> None:
    manufacturer = make_manufacturer(db, name="Würth Elektronik")
    part = make_part(
        db,
        name="mystery part from a salvage bin",
        mpn="LM358N",
        description="dual operational amplifier",
        keywords="opamp jellybean",
        manufacturer_id=manufacturer.id,
    )
    db.commit()

    assert _matches(db, "part_fts", "amplifier") == {part.id}
    assert _matches(db, "part_fts", "mpn:LM358N") == {part.id}
    assert _matches(db, "part_fts", "jellybean") == {part.id}
    # Diacritics are folded, because nobody types the umlaut into a search box.
    assert _matches(db, "part_fts", "wurth") == {part.id}


def test_a_part_with_only_a_name_is_still_findable(db: Session) -> None:
    """`name` has no FTS column of its own — the design fixes the column set and
    does not list it — so the triggers fold it into the `description` bucket.
    That is not cosmetic: `name` is the only text the intake fast path requires,
    so without this a scanned-in stub would be unfindable by the words on it."""
    part = make_part(db, name="mystery part from a salvage bin")
    db.commit()

    assert _matches(db, "part_fts", "salvage") == {part.id}


def test_update_trigger_reindexes_the_changed_text(db: Session) -> None:
    part = make_part(db, name="op-amp", description="dual operational amplifier")
    db.commit()

    part.description = "quad comparator"
    db.commit()

    assert _matches(db, "part_fts", "amplifier") == set()
    assert _matches(db, "part_fts", "comparator") == {part.id}


def test_update_trigger_preserves_param_digest(db: Session) -> None:
    """The whole reason the update trigger is an `UPDATE` and not the usual FTS
    delete-and-reinsert. `param_digest` will be written by a pipeline that has
    no cheap way to recompute one row on demand, so discarding it as a side
    effect of editing a description would lose data with no source to restore
    it from."""
    part = make_part(db, name="op-amp", description="dual operational amplifier")
    db.commit()

    db.execute(
        text("UPDATE part_fts SET param_digest = :digest WHERE rowid = :id"),
        {"digest": "gain_bandwidth 1MHz supply 36V", "id": part.id},
    )
    db.commit()

    part.description = "quad operational amplifier"
    db.commit()

    digest = db.execute(
        text("SELECT param_digest FROM part_fts WHERE rowid = :id"), {"id": part.id}
    ).scalar_one()
    assert digest == "gain_bandwidth 1MHz supply 36V"
    assert _matches(db, "part_fts", "param_digest:1MHz") == {part.id}


def test_delete_trigger_removes_the_index_row(db: Session) -> None:
    """A deleted part that stays in the index is worse than a missing one: it
    shows up in search and then resolves to nothing."""
    part = make_part(db, name="typo part", description="created by mistake")
    db.commit()
    assert _matches(db, "part_fts", "mistake") == {part.id}

    db.delete(part)
    db.commit()

    assert _matches(db, "part_fts", "mistake") == set()
    assert db.execute(text("SELECT COUNT(*) FROM part_fts")).scalar_one() == 0


def test_migration_backfills_parts_that_already_existed(
    db: Session, database_url: str, alembic_config: Config
) -> None:
    """The index has to arrive populated. Rolling the migration back, adding a
    part while neither the table nor its triggers exist, and rolling forward
    again is the only way to reproduce what happens on a real database that has
    been in use — and it exercises the downgrade path at the same time.
    """
    db.close()
    command.downgrade(alembic_config, "-1")

    engine = reset_engine_for_testing(database_url)
    session = get_session_factory()()
    try:
        part = make_part(session, name="acquired before the index existed", mpn="1N4148")
        part_id = part.id
        session.commit()
    finally:
        session.close()
    engine.dispose()

    command.upgrade(alembic_config, "head")

    reset_engine_for_testing(database_url)
    after = get_session_factory()()
    try:
        assert _matches(after, "part_fts", "acquired") == {part_id}
        assert _matches(after, "part_fts", "mpn:1N4148") == {part_id}
    finally:
        after.close()


# ---------------------------------------------------------------------------
# barcode_aliases — the alias-learning table
# ---------------------------------------------------------------------------


def _alias(**kwargs: object) -> BarcodeAlias:
    defaults: dict[str, object] = {
        "code_norm": "0009898989",
        "symbology": "code128",
        "entity_type": EntityType.PART,
        "entity_pk": 1,
        "alias_kind": AliasKind.WHOLE_PAYLOAD,
    }
    defaults.update(kwargs)
    return BarcodeAlias(**defaults)


def test_teaching_the_same_binding_twice_is_refused(db: Session) -> None:
    """So the bind endpoint can be an upsert that bumps `hit_count`. Unlike the
    NULL-heavy compound index deliberately left off `layout_suggestions`, all
    four columns here are NOT NULL, so this constraint really does constrain
    what it looks like it does."""
    db.add(_alias())
    db.commit()

    db.add(_alias())
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_one_code_may_bind_to_several_entities(db: Session) -> None:
    """`code_norm` is deliberately not unique. Two suppliers ship the same EAN
    and a bare MPN can name several rows; that is the resolver's `ambiguous`
    branch, which asks the user, not a data error to be prevented here."""
    db.add(_alias(entity_pk=1))
    db.add(_alias(entity_pk=2, alias_kind=AliasKind.MPN))
    db.commit()

    found = db.execute(select(BarcodeAlias).where(BarcodeAlias.code_norm == "0009898989")).scalars()
    assert {alias.entity_pk for alias in found} == {1, 2}


def test_symbology_is_free_text_so_a_new_carrier_needs_no_schema_change(db: Session) -> None:
    """The one place a StrEnum is deliberately *not* used. Symbology names come
    from hardware and decoder libraries we do not control, and the design
    promises `nfc_uid`/`nfc_ndef` need no DDL — so an unfamiliar name must be
    storable rather than rejected."""
    db.add(_alias(symbology="nfc_ndef", entity_type=EntityType.LOCATION))
    db.add(_alias(symbology="a-reader-we-have-never-heard-of", alias_kind=AliasKind.TAG_UID))
    db.commit()

    stored = db.execute(select(BarcodeAlias.symbology)).scalars().all()
    assert "nfc_ndef" in stored
    assert "a-reader-we-have-never-heard-of" in stored


def test_alias_kind_is_still_validated_in_python(db: Session) -> None:
    """Free-text symbology is the exception, not the new rule: `alias_kind` is
    ours to define, so a typo in it is a bug and must fail on write."""
    db.add(_alias(alias_kind="whole-payload"))
    with pytest.raises(StatementError, match="not a valid AliasKind"):
        db.flush()
    db.rollback()


def test_quantity_hints_are_milli_integers(db: Session) -> None:
    info = {row[1]: row[2] for row in db.execute(text("PRAGMA table_info(barcode_aliases)")).all()}
    assert info["hint_qty_milli"].upper() == "INTEGER"


# ---------------------------------------------------------------------------
# scan_events — the raw log
# ---------------------------------------------------------------------------

#: A DigiKey-style ECIA payload, control characters intact: RS (0x1E) and GS
#: (0x1D) separators, EOT (0x04) terminator.
ECIA_PAYLOAD = "[)>\x1e06\x1dP595-LM358N\x1d1P296-1395-1-ND\x1dQ2500\x1d1TLOT4711\x1e\x04"


def test_an_undecodable_payload_is_still_recorded_verbatim(db: Session) -> None:
    """The point of the table. A vendor format nobody parses yet is a parser
    waiting to be written; a discarded payload is gone. That includes the
    control characters — an ECIA payload *is* its separators, and stripping them
    is exactly the lossy step that would make the format unmineable."""
    event = ScanEvent(
        raw_payload=ECIA_PAYLOAD,
        payload_sha256="0" * 64,
        symbology="datamatrix",
    )
    db.add(event)
    db.commit()

    stored = db.execute(select(ScanEvent)).scalar_one()
    assert stored.raw_payload == ECIA_PAYLOAD
    # Defaults describe "nothing was made of this", which is a prompt to bind,
    # never an error.
    assert stored.decoded_kind == ScanDecodedKind.UNKNOWN
    assert stored.action_taken == ScanAction.UNRESOLVED
    assert stored.resolved_entity_type is None
    assert stored.resolved_entity_pk is None
    assert stored.latency_ms is None


def test_retiring_a_scanner_keeps_its_history(db: Session) -> None:
    """`SET NULL`, not `CASCADE`: what was read is worth more than which device
    read it, and an event from an unregistered reader has to be recordable
    anyway — hence a nullable `source_id`."""
    source = ScanSource(
        slug="bench-phone", display_name="Bench phone", kind=ScanSourceKind.BROWSER_CAMERA
    )
    db.add(source)
    db.flush()
    db.add(
        ScanEvent(
            source_id=source.id,
            raw_payload="4K7T92M8",
            payload_sha256="1" * 64,
            decoded_kind=ScanDecodedKind.SHORT_ID,
            action_taken=ScanAction.RESOLVED,
            resolved_entity_type=EntityType.LOCATION,
            resolved_entity_pk=1,
            latency_ms=12,
        )
    )
    db.commit()

    db.delete(source)
    db.commit()

    event = db.execute(select(ScanEvent)).scalar_one()
    assert event.source_id is None
    assert event.raw_payload == "4K7T92M8"
    assert event.decoded_kind == ScanDecodedKind.SHORT_ID


def test_the_same_payload_may_be_scanned_repeatedly(db: Session) -> None:
    """`payload_sha256` is indexed for the duplicate hold-off, not unique:
    rescanning the same reel next month is normal, and a rejected insert would
    lose the second scan."""
    for _ in range(3):
        db.add(ScanEvent(raw_payload=ECIA_PAYLOAD, payload_sha256="2" * 64))
    db.commit()

    assert db.execute(text("SELECT COUNT(*) FROM scan_events")).scalar_one() == 3
