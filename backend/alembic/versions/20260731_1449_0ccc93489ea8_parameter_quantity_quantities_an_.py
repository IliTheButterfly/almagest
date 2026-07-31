"""parameter_quantity — quantities an install defines itself

A numeric field's `base_unit` had to name one of the quantities
`elec-value-parser` ships, because that is what the grammar can read a value
under. Good for the electrical ones and now for light, mass, length and a few
ratios — but an inventory is allowed to care about something nobody
anticipated (bytes of flash, turns of wire, hours of runtime), and until this
table the only way to add one was to edit the library and redeploy.

One table, no foreign keys, and deliberately **not** a link to
`parameter_template`: fields reference a quantity by the `base_unit` *string*,
exactly as they reference a shipped one, so nothing about how a field is
stored or parsed changes with where its quantity was defined. The cost of that
choice is that a delete cannot be a cascade — `app.services.quantities.delete`
refuses while any field is measured in it, because a field naming a quantity
that had gone would refuse every value from then on.

`created_at` renders as `sa.String` because `alembic/env.py` renders custom
types that way: a migration must never import from `app`.

Revision ID: 0ccc93489ea8
Revises: 3b7c1a94ef20
Create Date: 2026-07-31 14:49:53.408215+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0ccc93489ea8"
down_revision: Union[str, Sequence[str], None] = "3b7c1a94ef20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "parameter_quantity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("symbol_aliases_json", sa.Text(), nullable=True),
        sa.Column("word_aliases_json", sa.Text(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("allow_zero", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("allow_negative", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("allow_prefix", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parameter_quantity")),
        # UNIQUE on `name` and no CHECK anywhere: the name is what a field's
        # `base_unit` holds, so two rows spelling one quantity differently would
        # be two definitions of the same unit, parsed differently.
        sa.UniqueConstraint("name", name=op.f("uq_parameter_quantity_name")),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the definitions, which is what a downgrade of this table means: any
    field still measured in one of them can no longer read a value, and that is
    visible immediately rather than silently — the parser refuses an unregistered
    quantity outright. The `parameter_value` rows themselves are untouched.
    """
    op.drop_table("parameter_quantity")
