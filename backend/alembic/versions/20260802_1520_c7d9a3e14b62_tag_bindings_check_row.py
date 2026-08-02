"""a cache_state row for the duplicate-tag-binding check

Not a cache. `location_tags` is the record, derived from nothing, so there is
nothing to rebuild it from and `POST /api/system/caches/rebuild` deliberately
cannot touch it. It gets a row here because `cache_state` is what the nightly
pass writes into and what an operator already reads, and one physical tag bound
to two containers is the single condition in this schema that silently sends
somebody to the wrong drawer: `tag_with_uid` resolves a duplicate by lowest id,
so the station identifies a container the tag is not on and commits stock into
it while both containers' pages look correct.

`is_dirty` is seeded 0 rather than 1: a dirty flag means "a rebuild is owed",
and no rebuild exists for this one. Only the check does.

This is the precondition for a future unique index on `location_tags.tag_uid`.
That migration cannot be written blind — `CREATE UNIQUE INDEX` fails outright on
a database that already holds a duplicate, and the bug that could have written
one was live until recently — so somebody first has to know whether one exists
and, if so, which binding is the real one.

Revision ID: c7d9a3e14b62
Revises: a1c4f7e29b53
Create Date: 2026-08-02 15:20:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c7d9a3e14b62"
down_revision: Union[str, Sequence[str], None] = "a1c4f7e29b53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.get_bind().execute(
        sa.text(
            "INSERT INTO cache_state (name, is_dirty, drift_count)"
            " VALUES ('tag_bindings', 0, 0)"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.get_bind().execute(sa.text("DELETE FROM cache_state WHERE name = 'tag_bindings'"))
