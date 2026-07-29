"""parameter_template.is_seed — freeze a shared field definition's identity

Part-type authoring gives `parameter_template` its first write path, and with it
the first way to *edit* a definition the rest of the system hard-codes. Renaming
`capacitance` or changing its `base_unit` from farads is not an edit, it is a
silent data corruption: every `parameter_value` row's `value_min`/`value_max`
were computed under the old quantity, so they keep looking authoritative while
answering range queries in the wrong unit.

One nullable-free boolean, no `CHECK`, defaulted false, so every row that
already exists stays editable — the seeds are marked by
`app.scripts.seed_demo`, which is the only thing that creates them (there has
never been a `parameter_template` row in a migration).

Not the clone-on-edit shape `container_types.is_seed` drives: see
`app.models.parameter.ParameterTemplate.is_seed` for why a cloned *field*
definition is the failure rather than the point.

Revision ID: 3b7c1a94ef20
Revises: 65de2c398164
Create Date: 2026-07-29 16:00:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3b7c1a94ef20"
down_revision: Union[str, Sequence[str], None] = "65de2c398164"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "parameter_template",
        sa.Column("is_seed", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Plain `DROP COLUMN`, never `batch_alter_table`: batch mode rebuilds the
    # table through a rename, and `parameter_value.template_id` /
    # `parameter_choice.template_id` reference it, so on a populated database the
    # drop-and-rename trips the FK and leaves `_alembic_tmp_parameter_template`
    # behind. `tests/integration/test_migration_round_trip.py` is the guard.
    op.drop_column("parameter_template", "is_seed")
