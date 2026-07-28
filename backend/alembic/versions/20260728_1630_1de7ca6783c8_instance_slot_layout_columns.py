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


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("locations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("row_span", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(
            sa.Column("col_span", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("size_class", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("inner_volume_mm3", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("locations", schema=None) as batch_op:
        batch_op.drop_column("inner_volume_mm3")
        batch_op.drop_column("size_class")
        batch_op.drop_column("col_span")
        batch_op.drop_column("row_span")
