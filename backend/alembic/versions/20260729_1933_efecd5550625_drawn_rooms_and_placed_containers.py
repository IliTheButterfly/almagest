"""drawn rooms and placed containers

Iliana's request: "For the room layout, we should be able to draw a room and lay
containers out in it." Two facts the schema could not carry, and ADR 0009 argues
they are deliberately different shapes.

**The room's outline is a table, not a column.** `location_plan_shapes` plus
`location_plan_shape_points` — one polyline per drawn thing, one row per vertex,
every coordinate an INTEGER millimetre. Rooms have alcoves, so this could never
have been a width/depth pair; and a wall, a door or a bench that holds nothing is
**not a location**, so it must not become a `locations` row with a kind on it. A
drawn wall has no `short_id`, holds no stock, and must never appear in the tree
or resolve from a scan.

Points are rows rather than a JSON/WKT/SVG-path blob for the reason the rest of
this schema already applies to `parameter_value`: the smallest thing that can be
wrong should be the smallest thing that can be inspected, and a blob needs a
parser on both sides whose first bug is silent. There is no geometry library, no
spatial index and no polygon type here — a room has tens of vertices and the only
question ever asked of them is "draw this".

**A placement is six nullable columns on the child.** `plan_x_mm`, `plan_y_mm`,
`plan_rotation_deg`, `plan_width_mm`, `plan_depth_mm`, and `plan_parent_id` — the
parent those coordinates were authored against. ADR 0006 says a floor plan has
"no empty positions, because a space has none", and that stays true: this is a
coordinate, not a slot, and `row_idx`/`col_idx` remain cells on a parent's slot
canvas with nothing to do with it.

**All of it nullable, and no backfill.** An unplaced child is a real state — added
to the room, never dragged anywhere — and defaulting to (0, 0) would put every
existing container in the same corner of every room and look authored.

`plan_parent_id` is a plain `INTEGER` with **no foreign key**, which is the one
thing in here worth pausing on. Practically, SQLite cannot add a foreign key
without rebuilding the table, and rebuilding `locations` is what
`20260729_0930_c31b7a5e9d04`'s downgrade note records as failing: batch mode
renames the table and SQLite re-parses every trigger on it mid-rename, against
`trg_stock_ledger_dirty_occupancy`. Semantically it is a *witness* rather than a
reference — "these coordinates were authored while my parent was N" — so a value
pointing at a row that no longer exists is not a broken link, it is exactly the
stale placement the column exists to detect (`app.services.room_plan.
placement_of`). Every column here is therefore a bare `ADD COLUMN`, which touches
no trigger.

No `CHECK` anywhere: `location_plan_shapes.kind` is a plain `VARCHAR(32)` carrying
`PlanShapeKind`, so a new drawable thing stays one enum member and a renderer
branch.

Revision ID: efecd5550625
Revises: 65de2c398164
Create Date: 2026-07-29 19:33:10.222182+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "efecd5550625"
down_revision: Union[str, Sequence[str], None] = "65de2c398164"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The placement columns, in the order the model declares them. Named once so
#: `upgrade` and `downgrade` cannot disagree about the set.
_PLACEMENT_COLUMNS = (
    ("plan_parent_id", sa.Integer()),
    ("plan_x_mm", sa.Integer()),
    ("plan_y_mm", sa.Integer()),
    ("plan_rotation_deg", sa.Integer()),
    ("plan_width_mm", sa.Integer()),
    ("plan_depth_mm", sa.Integer()),
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "location_plan_shapes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        # `PlanShapeKind`, as a plain VARCHAR. No CHECK, no sa.Enum.
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("is_closed", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("thickness_mm", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("updated_at", sa.String(length=27), nullable=False),
        # CASCADE, unlike almost every other reference to `locations` in this
        # schema. Those are RESTRICT because deleting a cabinet must never
        # silently take its drawers and their contents with it — a drawing of a
        # wall has no contents, and refusing to delete a room because somebody
        # once drew a door on it would be absurd.
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["locations.id"],
            name=op.f("fk_location_plan_shapes_location_id_locations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_location_plan_shapes")),
    )
    op.create_index(
        op.f("ix_location_plan_shapes_location_id"),
        "location_plan_shapes",
        ["location_id"],
        unique=False,
    )
    op.create_index(
        "ix_plan_shapes_location_sort",
        "location_plan_shapes",
        ["location_id", "sort_order"],
        unique=False,
    )

    op.create_table(
        "location_plan_shape_points",
        sa.Column("shape_id", sa.Integer(), nullable=False),
        # Part of the primary key, so the polyline's order is stored rather than
        # inferred from insertion — which `ORDER BY rowid` would be, and which a
        # re-authored shape would quietly scramble.
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("x_mm", sa.Integer(), nullable=False),
        sa.Column("y_mm", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["shape_id"],
            ["location_plan_shapes.id"],
            name=op.f("fk_location_plan_shape_points_shape_id_location_plan_shapes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("shape_id", "seq", name=op.f("pk_location_plan_shape_points")),
    )

    # Plain `ADD COLUMN`, never `batch_alter_table` — see the module docstring:
    # batch mode rebuilds `locations` via a rename and SQLite re-parses every
    # trigger on that table mid-rename, which fails against
    # `trg_stock_ledger_dirty_occupancy`.
    for name, type_ in _PLACEMENT_COLUMNS:
        op.add_column("locations", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Plain `DROP COLUMN` for the same reason, supported by SQLite since 3.35 and
    # touching no trigger.
    for name, _type in reversed(_PLACEMENT_COLUMNS):
        op.drop_column("locations", name)

    op.drop_table("location_plan_shape_points")
    op.drop_index("ix_plan_shapes_location_sort", table_name="location_plan_shapes")
    op.drop_index(
        op.f("ix_location_plan_shapes_location_id"), table_name="location_plan_shapes"
    )
    op.drop_table("location_plan_shapes")
