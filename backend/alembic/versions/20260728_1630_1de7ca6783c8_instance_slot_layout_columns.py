"""instance slot layout columns

Layout authoring (docs/PLAN.md, "Layout authoring and tag provisioning"):
instances own their own copy of a container type's layout, not a live link
back to it. `container_type_slot_templates` already carries `row_span`,
`col_span` and per-slot size metadata for the *type*; once instantiated, a
concrete `locations` row needs the identical facts about itself, because the
layout change guard (safe relabel vs. a merge that would swallow a neighbour's
stock) has to reason about an instance's own footprint and per-slot overrides
independently of whatever the type looks like today.

Four nullable/defaulted columns, purely additive — no backfill, and existing
rows (span 1x1, no size override) are exactly what the defaults already mean.

Revision ID: 1de7ca6783c8
Revises: 2455fce33ad7
Create Date: 2026-07-28 16:30:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1de7ca6783c8"
down_revision: Union[str, Sequence[str], None] = "2455fce33ad7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Plain `op.add_column`/`op.drop_column`, **not** `op.batch_alter_table`, and the
#: reason is `locations` specifically.
#:
#: Batch mode implements an unsupported ALTER by building `_alembic_tmp_locations`,
#: copying rows across, dropping the original and renaming the copy into place. That
#: last rename is where it dies here: SQLite re-checks every existing trigger body
#: when a table is renamed, and `trg_stock_ledger_dirty_occupancy` (created one
#: revision earlier, in `b8c9d4009bdd`) names `main.locations` — which does not exist
#: at that instant, because the real table has just been dropped. The rename fails
#: with `error in trigger trg_stock_ledger_dirty_occupancy: no such table:
#: main.locations`, and the downgrade leaves the schema wedged mid-rebuild.
#:
#: Nothing here needs a rebuild anyway. SQLite has had native `ADD COLUMN` forever
#: and native `DROP COLUMN` since 3.35 (2021), so both directions are one statement
#: each — which is also what the later `child_view` and `glyph` migrations do, so
#: this is now the one convention rather than two. The rule to carry forward: a
#: migration that genuinely needs a table rebuild on a trigger-referenced table has
#: to drop those triggers first and recreate them after.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("locations", sa.Column("row_span", sa.Integer(), server_default="1", nullable=False))
    op.add_column("locations", sa.Column("col_span", sa.Integer(), server_default="1", nullable=False))
    op.add_column("locations", sa.Column("size_class", sa.String(length=32), nullable=True))
    op.add_column("locations", sa.Column("inner_volume_mm3", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("locations", "inner_volume_mm3")
    op.drop_column("locations", "size_class")
    op.drop_column("locations", "col_span")
    op.drop_column("locations", "row_span")
