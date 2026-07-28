"""scanning tables and full-text search

The scanning path (`barcode_aliases`, `scan_sources`, `scan_events`) plus the two
FTS5 indexes, from PLAN.md's "Other tables". The remaining tables in that list
belong to later phases and arrive with the code that uses them: `documents` and
`document_links` with the datasheet store (Phase 4), `projects`/`bom_lines`/
`stock_allocations` with BOMs (Phase 2), `locator_devices`/`locator_channels`
when there is an LED to drive.

The FTS5 part is hand-written and cannot be otherwise. A `CREATE VIRTUAL TABLE
... USING fts5(...)` has no SQLAlchemy model equivalent, so autogenerate can
neither create these nor see them; `alembic/env.py` filters them (and the
`_data`/`_idx`/`_content`/`_docsize`/`_config` shadow tables SQLite manages
alongside each one) out of the comparison so that `alembic check` does not
report them as drift and offer to drop them.

**`part_fts` carries `param_digest` from day one, even though nothing writes it
yet.** An FTS5 table's column set is fixed at creation: changing it means
dropping the table and rebuilding the whole index. Adding a nullable column to
an ordinary table is free, so provisioning ahead of time is normally a smell —
here it is the opposite, and it is the reason the design named that column
before there was any code to fill it.

`datasheet_fts` is a *separate* table rather than a sixth column on `part_fts`,
because one family datasheet covers dozens of MPNs; folding its text into the
part index would store the same PDF's prose once per part. Its `rowid` is the
future `documents.id`. It gets no trigger — datasheet text arrives from an
extraction pipeline, not by mirroring a column, and there is nothing yet to
trigger on.

Three triggers keep `part_fts` in step with `parts`, and two details in them are
deliberate:

* the `UPDATE` trigger issues an `UPDATE`, not the usual FTS `DELETE` +
  `INSERT`, so that a `param_digest` written by a future parameter pipeline
  survives an edit to the part's description. Delete-and-reinsert would silently
  discard a value whose only other source is a full recompute.
* it fires `AFTER UPDATE OF name, mpn, description, keywords, manufacturer_id`
  rather than on any update at all. `parts.hot_score` is rewritten for every row
  nightly, and reindexing the entire catalogue as a side effect of that would be
  pure waste.

One hazard to know about: a future `batch_alter_table("parts")` rebuilds the
table, which drops triggers attached to it. Any migration that does so must
recreate the three below — the same caveat already applies to `stock_ledger`'s
append-only triggers.

Revision ID: dcfe797424e9
Revises: b8c9d4009bdd
Create Date: 2026-07-28 14:02:55.455798+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "dcfe797424e9"
down_revision: Union[str, Sequence[str], None] = "b8c9d4009bdd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Full-text search
# ---------------------------------------------------------------------------
# Column set exactly as PLAN.md specifies, because it is the one thing here that
# cannot be revised cheaply later. Two index-shape options are frozen with it,
# for the same reason — neither can be changed without a full reindex:
#
# * `remove_diacritics 2` (the corrected variant, not the legacy `1`): real
#   manufacturer names carry diacritics, and nobody types "Würth" into a search
#   box with the umlaut.
# * `prefix = '2 3'`: the part search box is a type-ahead, so nearly every query
#   is really a prefix query. FTS5 answers `wur*` without a prefix index by
#   scanning every matching term; a two- and three-character prefix index turns
#   the common case into a seek. It costs an extra copy of the doclist per prefix
#   length, which is nothing across a few thousand short part records.
#
# `datasheet_fts` deliberately gets no prefix index: the same multiplier applied
# to the full text of hundreds of PDFs is a real cost, and searching datasheets
# is a whole-word activity ("thermal resistance"), not a type-ahead.
_CREATE_FTS_TABLES = (
    """
    CREATE VIRTUAL TABLE part_fts USING fts5(
        mpn,
        description,
        manufacturer,
        keywords,
        param_digest,
        tokenize = 'unicode61 remove_diacritics 2',
        prefix = '2 3'
    )
    """,
    """
    CREATE VIRTUAL TABLE datasheet_fts USING fts5(
        text,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
)

# `parts.name` shares the `description` bucket rather than getting a column of
# its own, because the column set above is fixed by the design and `name` is not
# in it. Leaving name out entirely was the alternative and it is untenable: name
# is the *only* text the intake fast path requires, so a part scanned in as
# "mystery part from a salvage bin" would be unfindable by the words on it. An
# FTS column is a search bucket, not storage — `parts` remains the authority for
# both strings — so combining them loses nothing except the ability to weight
# name above description in a future bm25() ranking.
_SEARCH_TEXT = "TRIM({p}name || ' ' || COALESCE({p}description, ''))"

# Denormalised so a text query never has to join. The cost is that renaming a
# manufacturer leaves this column stale until `part_fts` is reindexed with
# `_BACKFILL_PART_FTS` below; that is a stale search hit, never wrong data,
# since `parts.manufacturer_id` stays authoritative.
_MANUFACTURER_NAME = "(SELECT m.name FROM manufacturers AS m WHERE m.id = {p}manufacturer_id)"

_PART_FTS_TRIGGERS = (
    f"""
    CREATE TRIGGER trg_parts_fts_insert
    AFTER INSERT ON parts
    BEGIN
        INSERT INTO part_fts (rowid, mpn, description, manufacturer, keywords, param_digest)
        VALUES (
            NEW.id,
            NEW.mpn,
            {_SEARCH_TEXT.format(p="NEW.")},
            {_MANUFACTURER_NAME.format(p="NEW.")},
            NEW.keywords,
            NULL
        );
    END
    """,
    f"""
    CREATE TRIGGER trg_parts_fts_update
    AFTER UPDATE OF name, mpn, description, keywords, manufacturer_id ON parts
    BEGIN
        UPDATE part_fts
           SET mpn = NEW.mpn,
               description = {_SEARCH_TEXT.format(p="NEW.")},
               manufacturer = {_MANUFACTURER_NAME.format(p="NEW.")},
               keywords = NEW.keywords
         WHERE rowid = NEW.id;
    END
    """,
    """
    CREATE TRIGGER trg_parts_fts_delete
    AFTER DELETE ON parts
    BEGIN
        DELETE FROM part_fts WHERE rowid = OLD.id;
    END
    """,
)

_PART_FTS_TRIGGER_NAMES = (
    "trg_parts_fts_insert",
    "trg_parts_fts_update",
    "trg_parts_fts_delete",
)

# Populates the index for parts that already exist. Also the reindex recipe:
# `part_fts` is a derived cache like every other one in this schema, so
# `DELETE FROM part_fts;` followed by this statement rebuilds it from `parts` —
# which is what makes a manufacturer rename, or a change to the trigger bodies
# above, a repair job rather than a data-loss event. `param_digest` is NULL
# because nothing computes it yet; a reindex therefore drops any digest, and the
# pipeline that eventually writes it owns recomputing it.
_BACKFILL_PART_FTS = f"""
    INSERT INTO part_fts (rowid, mpn, description, manufacturer, keywords, param_digest)
    SELECT p.id,
           p.mpn,
           {_SEARCH_TEXT.format(p="p.")},
           {_MANUFACTURER_NAME.format(p="p.")},
           p.keywords,
           NULL
      FROM parts AS p
"""


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "barcode_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_norm", sa.String(length=512), nullable=False),
        sa.Column("symbology", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_pk", sa.Integer(), nullable=False),
        sa.Column(
            "alias_kind", sa.String(length=32), server_default="whole_payload", nullable=False
        ),
        sa.Column("parsed_json", sa.Text(), nullable=True),
        sa.Column("hint_qty_milli", sa.Integer(), nullable=True),
        sa.Column("hint_batch", sa.String(length=128), nullable=True),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_hit_at", sa.String(length=27), nullable=True),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_barcode_aliases")),
        sa.UniqueConstraint(
            "code_norm",
            "symbology",
            "entity_type",
            "entity_pk",
            name=op.f("uq_barcode_aliases_code_norm_symbology_entity_type_entity_pk"),
        ),
    )
    with op.batch_alter_table("barcode_aliases", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_barcode_aliases_code_norm"), ["code_norm"], unique=False
        )

    op.create_table(
        "scan_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("last_seen_at", sa.String(length=27), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("updated_at", sa.String(length=27), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scan_sources")),
        sa.UniqueConstraint("slug", name=op.f("uq_scan_sources_slug")),
    )
    with op.batch_alter_table("scan_sources", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_scan_sources_device_id"), ["device_id"], unique=False)

    op.create_table(
        "scan_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.String(length=27), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("symbology", sa.String(length=32), nullable=True),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("decoded_kind", sa.String(length=32), server_default="unknown", nullable=False),
        sa.Column("resolved_entity_type", sa.String(length=32), nullable=True),
        sa.Column("resolved_entity_pk", sa.Integer(), nullable=True),
        sa.Column(
            "action_taken", sa.String(length=32), server_default="unresolved", nullable=False
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["scan_sources.id"],
            name=op.f("fk_scan_events_source_id_scan_sources"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scan_events")),
    )
    with op.batch_alter_table("scan_events", schema=None) as batch_op:
        batch_op.create_index("ix_scan_events_kind_ts", ["decoded_kind", "ts"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_scan_events_payload_sha256"), ["payload_sha256"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_scan_events_source_id"), ["source_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_scan_events_ts"), ["ts"], unique=False)

    # ### end Alembic commands ###

    for statement in _CREATE_FTS_TABLES:
        op.execute(statement)
    for statement in _PART_FTS_TRIGGERS:
        op.execute(statement)
    op.execute(_BACKFILL_PART_FTS)


def downgrade() -> None:
    """Downgrade schema."""
    # Triggers first: they reference `part_fts`, and a trigger left behind
    # pointing at a dropped table fails on the next INSERT INTO parts.
    for name in _PART_FTS_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    # DROP TABLE on a virtual table takes its shadow tables with it.
    op.execute("DROP TABLE IF EXISTS datasheet_fts")
    op.execute("DROP TABLE IF EXISTS part_fts")

    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("scan_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scan_events_ts"))
        batch_op.drop_index(batch_op.f("ix_scan_events_source_id"))
        batch_op.drop_index(batch_op.f("ix_scan_events_payload_sha256"))
        batch_op.drop_index("ix_scan_events_kind_ts")

    op.drop_table("scan_events")
    with op.batch_alter_table("scan_sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scan_sources_device_id"))

    op.drop_table("scan_sources")
    with op.batch_alter_table("barcode_aliases", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_barcode_aliases_code_norm"))

    op.drop_table("barcode_aliases")
    # ### end Alembic commands ###
