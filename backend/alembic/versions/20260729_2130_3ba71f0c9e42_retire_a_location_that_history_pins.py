"""retired_at on locations — removing a container that the ledger will not let go

Iliana: "I also noticed that I wasn't able to remove items in the workshop."
There was no delete for a location anywhere, and adding one runs straight into a
constraint that is not a mistake:

* `stock_lots.location_id` is `ON DELETE RESTRICT`, and nothing in this system
  ever deletes a lot row — not even a fully consumed one.
* `stock_ledger.{from,to}_location_id` are `RESTRICT` against a table whose
  UPDATE and DELETE are refused by trigger.

So a drawer that has *ever* held anything can never be deleted, and it never
should be: the alternative is deleting history. But refusing outright would mean
a used drawer stays on screen forever with no way to remove it, which is the
complaint that started this.

Hence one nullable timestamp. A location nothing names is deleted outright — the
common case, an empty cell stamped out of a template. A location the ledger, a
printed label or a stuck-on tag names is **retired**: the row and all its history
stay exactly where they are, and the container leaves the tree, the room plan,
its parent's slot canvas and auto-assignment. `app.services.removal` decides
which of the two happens and says so in the response; nothing here guesses.

Nullable timestamp rather than a boolean flag, matching `stock_lots.retired_at`,
which already means precisely this for a lot: it is NULL for almost every row,
it answers "when" for free, and clearing it is the undo (`POST
/api/locations/{id}/restore`).

**No `CHECK`, and nothing to backfill.** NULL is the pre-existing state of every
row and already means "not removed", so the migration is additive in the strict
sense — an older build reading a newer database simply sees every location,
including the retired ones, which is a stale view and not a wrong one.

Revision ID: 3ba71f0c9e42
Revises: efecd5550625
Create Date: 2026-07-29 21:30:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3ba71f0c9e42"
down_revision: Union[str, Sequence[str], None] = "efecd5550625"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("locations", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_locations_retired_at", "locations", ["retired_at"])


def downgrade() -> None:
    """Downgrade schema."""
    # Plain `DROP COLUMN`, never `batch_alter_table` — see
    # `20260729_0930_c31b7a5e9d04_child_view_per_layer_drawing.py`: batch mode
    # rebuilds `locations` via a rename, and SQLite re-parses every trigger on
    # the table mid-rename, which fails against
    # `trg_stock_ledger_dirty_occupancy`. A plain column drop (SQLite >= 3.35)
    # touches no trigger.
    #
    # Downgrading loses the distinction between a retired container and a live
    # one, so every retired drawer reappears in the tree. That is recoverable by
    # hand and not data loss; it is noted because it is not obvious.
    op.drop_index("ix_locations_retired_at", table_name="locations")
    op.drop_column("locations", "retired_at")
