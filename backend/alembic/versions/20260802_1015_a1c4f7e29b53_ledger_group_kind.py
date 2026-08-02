"""group_kind — why a `group_uuid`'s rows are grouped, and so what undo means

Undoing one line of a committed work-panel tab reversed the entire commit.
`_rows_to_undo` resolved the `client_op_id` to its row, saw a `group_uuid`, and
expanded to every row in the group. That expansion is *correct* for a partial
move — `split_out -N` and `split_in +N` cannot be reversed separately without
leaving the stock duplicated across two bins — but a tab commit gives every line
the same group, so naming one line reversed all of them.

The two shapes are indistinguishable after the fact. A caller cannot tell "a
move" from "a two-line commit" by looking at signs, keys or counts, so the answer
has to be **recorded when the group is minted** rather than inferred. Of the six
places that mint a `group_uuid`, exactly one is an aggregate: `stock.batch_movements`.
`empty_bin` is atomic — it strips the key off every row after the first, so
undoing by that key means "undo the whole emptying", which is what a person
emptying a bin wants.

One additive nullable column, no `CHECK` (`sa.String` here, `StrEnumType` at the
model layer, per CLAUDE.md). **NULL reads as atomic**, which is exactly the
behaviour every row written before today was written under; history is not
reinterpreted retroactively. No backfill: rows already written by a tab commit
keep the whole-commit expansion they were created with.

`stock_ledger` rejects UPDATE and DELETE by trigger, but `ALTER TABLE ... ADD
COLUMN` is DDL and not a row UPDATE, so the triggers do not fire and no row is
rewritten.

Revision ID: a1c4f7e29b53
Revises: 170f9ba6566e
Create Date: 2026-08-02 10:15:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1c4f7e29b53"
down_revision: Union[str, Sequence[str], None] = "170f9ba6566e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 32 is the length this schema gives a StrEnum column everywhere else.
    op.add_column("stock_ledger", sa.Column("group_kind", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("stock_ledger", "group_kind")
