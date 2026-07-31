"""multi-valued enum fields — a set of options per part, without a second value row

A list field could hold exactly one option per part, because an enum value *is*
`parameter_value.choice_id`. Some attributes are genuinely plural: a connector
that is both through-hole and surface-mount, a module with two interfaces.

The obvious implementation — a second `parameter_value` row — is the one thing
this schema cannot do. `UNIQUE(part_id, template_id)` is load-bearing: it is what
lets a multi-predicate parametric query be plain `JOIN`s that each contribute at
most one row. A second row per part would turn every such query into a cross
product, silently, and worse the more attributes are filtered on. So the value row
stays unique and the multiplicity goes in `parameter_value_choice`, which search
matches with `EXISTS` — a semi-join, which cannot fan out however many options a
part holds.

The child table is populated for **every** enum value, not only multi-valued ones,
which is why this migration backfills it from the existing `choice_id` rows: one
code path for search and for facet counting beats one per arity.
`parameter_value.choice_id` keeps its meaning — the single-valued answer — and is
left null on a multi-valued field, so a consumer that reads it cannot mistake one
option out of several for the whole answer.

`allow_multiple` is added with a plain `op.add_column`, never
`batch_alter_table`: batch mode rebuilds the table through a rename, and
`parameter_value.template_id` / `parameter_choice.template_id` reference
`parameter_template`, so on a populated database the drop-and-rename trips the FK
and leaves `_alembic_tmp_parameter_template` behind. That is the trap
`20260730_1200_3b7c1a94ef20` documents, and
`tests/integration/test_migration_round_trip.py` is the guard for both.

Revision ID: 9a8abc54dfaa
Revises: 0ccc93489ea8
Create Date: 2026-07-31 15:38:08.609319+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9a8abc54dfaa"
down_revision: Union[str, Sequence[str], None] = "0ccc93489ea8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "parameter_value_choice",
        sa.Column("value_id", sa.Integer(), nullable=False),
        sa.Column("choice_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["choice_id"],
            ["parameter_choice.id"],
            name=op.f("fk_parameter_value_choice_choice_id_parameter_choice"),
            # RESTRICT, matching `parameter_value.choice_id`: an option parts are
            # filed under is refused with a count, never cascaded away.
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["value_id"],
            ["parameter_value.id"],
            name=op.f("fk_parameter_value_choice_value_id_parameter_value"),
            ondelete="CASCADE",
        ),
        # The composite PK is the uniqueness that matters: a part holds an option
        # once or not at all.
        sa.PrimaryKeyConstraint("value_id", "choice_id", name=op.f("pk_parameter_value_choice")),
    )
    op.create_index("ix_pvc_choice", "parameter_value_choice", ["choice_id"], unique=False)

    op.add_column(
        "parameter_template",
        sa.Column("allow_multiple", sa.Boolean(), nullable=False, server_default="0"),
    )

    # Backfill, so the child table is the whole truth about which options a part
    # holds from the moment this lands. Without it, every enum value written
    # before today would be invisible to a search that asks `EXISTS` — which is a
    # filter that silently stops matching parts it used to match.
    op.execute(
        """
        INSERT INTO parameter_value_choice (value_id, choice_id)
        SELECT id, choice_id FROM parameter_value WHERE choice_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    A value of a multi-valued field has null `choice_id` and its options only in
    the child table, so dropping the table blind would leave those parts with an
    enum value that names nothing. Instead each such value keeps its **lowest**
    option id — an arbitrary but defined choice, and one option of the set is a
    truthful single answer where none is not. The other options are lost, which is
    what downgrading a table that holds them has to mean.
    """
    op.execute(
        """
        UPDATE parameter_value
        SET choice_id = (
            SELECT MIN(choice_id) FROM parameter_value_choice
            WHERE parameter_value_choice.value_id = parameter_value.id
        )
        WHERE choice_id IS NULL
          AND EXISTS (
            SELECT 1 FROM parameter_value_choice
            WHERE parameter_value_choice.value_id = parameter_value.id
          )
        """
    )
    op.drop_index("ix_pvc_choice", table_name="parameter_value_choice")
    op.drop_table("parameter_value_choice")
    # Plain `drop_column`, not batch mode — see the module docstring.
    op.drop_column("parameter_template", "allow_multiple")
