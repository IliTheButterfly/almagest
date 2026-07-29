"""glyph — a small pictogram per container, type default with instance override

Iliana's request: "I want containers to have icons/pictures so I can easily
attribute them to how they look. This can be both put on a template and edited
per container." Two distinct things answer that, and only one of them is a
schema change.

The **photograph** — what a drawer actually looks like, shot from a phone
standing in front of it — is not a new column anywhere. Phase 4 already built a
content-addressed document store for exactly this shape of problem
(`docs/adr/0005-extraction-runs-outside-the-api.md`), `document_links` is already
polymorphic by `(entity_type, entity_pk)`, and `DocumentRole.PHOTO` ("what the
thing looks like") already exists — added for parts, reused here unchanged for
`container_type` and `location`. A container's photo is simply a document linked
in that role, with the same "exactly one primary" rule `app.services.documents`
already maintains.

This migration is the *other* half: the **glyph**, a single pictogram cheap
enough to render at every node of a dense tree — the recursive container map can
draw ninety-six cells, and loading ninety-six photographs to do it would be
absurd (see `app.models.enums.ContainerGlyph`). It is genuinely a new fact with
nowhere to live, so it is two nullable `VARCHAR` columns, no `CHECK`, same
type-default/instance-override shape `esd_safe`/`is_placeable`/`fill_factor`/
`child_view` already use on these two tables.

**No backfill, and no third "derived" rung this time — unlike `child_view`.**
NULL means "no glyph chosen", full stop; there is no geometric fact on either
table that implies what a container *looks like*, so there is nothing to derive
it from. The renderer's answer to "both rungs are NULL" is a neutral placeholder,
which is a real, honest state rather than a guess standing in for one.

Revision ID: 65de2c398164
Revises: c31b7a5e9d04
Create Date: 2026-07-29 14:13:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "65de2c398164"
down_revision: Union[str, Sequence[str], None] = "c31b7a5e9d04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("container_types", sa.Column("glyph", sa.String(length=32), nullable=True))
    op.add_column("locations", sa.Column("glyph", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Plain `DROP COLUMN`, not `batch_alter_table` — see
    # `20260729_0930_c31b7a5e9d04_child_view_per_layer_drawing.py`'s downgrade for
    # why: batch mode rebuilds `locations` via a rename, and SQLite re-parses
    # every trigger on that table mid-rename, which fails against
    # `trg_stock_ledger_dirty_occupancy`. SQLite has supported a plain column drop
    # since 3.35, which touches no trigger.
    op.drop_column("locations", "glyph")
    op.drop_column("container_types", "glyph")
