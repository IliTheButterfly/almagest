"""child_view — how each layer of the storage tree is drawn

ADR 0006, which extends ADR 0002 rather than replacing it. A container type
answered two questions: what grid it presents to its children, and what
footprint it occupies in its parent. This adds the third — how its children
should be *drawn* — because neither of the first two determines it: a Raaco
cabinet and a Gridfinity bin both answer `child_layout='list'` and want
completely different pictures, one a face of drawer fronts and the other a run
of dividers.

**Two nullable columns and no backfill.** NULL is not "unknown" here, it means
"derive it from the geometry this row already declares"
(`app.services.views.derive_child_view`), and that derivation already gives
every existing row — including all eleven seed types — the right drawing: a
declared 42 mm pitch is a tray seen from above, a 30x1 canvas of drawers is a
cabinet face, a bin that occupies a footprint and presents nothing is a list of
dividers, and anything that neither presents nor occupies a grid is furniture
standing in a room. Writing a value into all of them instead would be a stored
copy of a fact the geometry states, free to drift from it.

`locations.child_view` is the instance override, NULL meaning "use the type" —
the same shape as the `esd_safe` / `is_placeable` / `fill_factor` overrides that
already sit on that table.

Both columns are plain `VARCHAR` with no `CHECK`, so adding a way to draw a
level stays a one-line change in `app.models.enums` on two tables that hold
every container in the building.

Revision ID: c31b7a5e9d04
Revises: ad85ca219ffb
Create Date: 2026-07-29 09:30:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c31b7a5e9d04"
down_revision: Union[str, Sequence[str], None] = "ad85ca219ffb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("container_types", sa.Column("child_view", sa.String(length=32), nullable=True))
    op.add_column("locations", sa.Column("child_view", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Plain `ALTER TABLE ... DROP COLUMN` rather than the `batch_alter_table`
    # used elsewhere in this history, and deliberately so: batch mode implements
    # a drop by building `_alembic_tmp_locations`, dropping `locations` and
    # renaming the copy into place — and SQLite re-parses every trigger during
    # that rename, so `trg_stock_ledger_dirty_occupancy` (created back in the
    # capacity migration, referencing `locations`) fails with "no such table:
    # main.locations" at the instant the original is gone. SQLite has supported
    # dropping a column outright since 3.35, which touches no trigger and needs
    # no table copy. `tests/integration/test_scanning_and_fts.py::
    # test_migration_backfills_parts_that_already_existed` steps back through
    # head, so this path is exercised rather than assumed.
    op.drop_column("locations", "child_view")
    op.drop_column("container_types", "child_view")
