"""location_tags.ndef_state — what the sticker actually holds, as opposed to what we meant it to

`bind` stamps `written_at = now()` before any device has touched the tag, because
binding is a row this server owns and writing is something only the device
physically holding the tag can do. So the record claimed a successful write at
the exact moment none had been attempted, and a write that failed partway left a
UID-only tag while the row still read as authoritative. `LocationTag`'s own
docstring already said such a tag is "flagged by the verify screen" — there was
nowhere to flag it.

Two additive columns, no `CHECK` (`sa.String` + `StrEnumType`, per CLAUDE.md), so
every existing binding lands on `unverified`, which is the honest answer for a
row nobody ever confirmed: it says "no device has reported back", not "broken".

Revision ID: b4e2f1c70d38
Revises: 9a8abc54dfaa
Create Date: 2026-07-31 17:20:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b4e2f1c70d38"
down_revision: Union[str, Sequence[str], None] = "9a8abc54dfaa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `sa.String`, never the application's `StrEnumType`/`UtcDateTime`: a
    # migration must not import from `app` (see `alembic/env.py`, which renders
    # both as `sa.String` for exactly this reason). The lengths are the ones those
    # types render to — 32 for a StrEnum, 27 for an ISO-8601 instant — because
    # `alembic check` compares rendered types and a 16 here reads as drift.
    op.add_column(
        "location_tags",
        sa.Column("ndef_state", sa.String(length=32), nullable=False, server_default="unverified"),
    )
    op.add_column(
        "location_tags",
        sa.Column("ndef_checked_at", sa.String(length=27), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Plain `DROP COLUMN`, never `batch_alter_table`: batch mode rebuilds through
    # a rename, and `location_tags.location_id` references `locations`, so on a
    # populated database the rebuild trips the FK and leaves
    # `_alembic_tmp_location_tags` behind. `test_migration_round_trip.py` guards
    # this on a database with rows in it.
    op.drop_column("location_tags", "ndef_checked_at")
    op.drop_column("location_tags", "ndef_state")
