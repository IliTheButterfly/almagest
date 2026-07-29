"""seed container type library

A data migration, not a schema change: the Gridfinity baseplate/bin family
(docs/PLAN.md, "3D-printed Gridfinity" — 42 mm grid pitch, 41.5 mm bin
footprint, 7 mm height unit, verified spec) plus the two researched
off-the-shelf cabinets (docs/PLAN.md, "off-the-shelf drawer cabinets") —
Akro-Mils 10144 and the Raaco C8-30/C10-40 pair.

**The Akro-Mils and Raaco drawer *layouts* below are a plausible
reconstruction, not a measurement.** PLAN.md is explicit that "no manufacturer
publishes a rows x cols grid" for these — retailers list a drawer count plus a
size mix ("44 small + 4 large"), never a canvas. The 7x8 canvas and the exact
cells chosen to merge into the four large drawers are this migration's own
invention, built only to land on the right *totals*; the true drawer
dimensions and this specific arrangement have not been checked against actual
hardware. `inner_volume_mm3` is left NULL on every one of their slots rather
than filled with an invented number attached to a real, named product.

Every row here is `is_seed=1`: read-only from the API's point of view — a
`PATCH` or a `PUT .../slot-template` against one of these clones it rather
than editing it in place (`app.services.layout_authoring.ensure_editable`).

Revision ID: 4cada779f255
Revises: 1de7ca6783c8
Create Date: 2026-07-28 17:00:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "4cada779f255"
down_revision: Union[str, Sequence[str], None] = "1de7ca6783c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: `UtcDateTime` stores ISO-8601 UTC text (`app/models/types.py`); migrations
#: cannot import that type, so this is the same format spelled out by hand.
_SEEDED_AT = "2026-07-28T00:00:00.000000Z"

#: Verified Gridfinity spec (ADR 0002 / docs/PLAN.md).
_PITCH_MM = 42.0
_HEIGHT_UNIT_MM = 7.0
_GENERATOR = "kennetek/gridfinity-rebuilt-openscad"

#: Bin footprints to standardise on — PLAN.md's own advice ("standardise on
#: very few footprint variants") applied to the seed itself. `(cols, rows,
#: height_u)` in grid units.
_BIN_FOOTPRINTS = (
    (1, 1, 6),
    (2, 1, 6),
    (1, 1, 3),
    (2, 2, 6),
    (3, 2, 6),
)

#: Baseplates to seed, in grid units.
_BASEPLATE_SIZES = ((2, 2), (4, 4), (4, 6))


def _row_letter(index: int) -> str:
    return chr(ord("A") + index)


def _akro_mils_slots() -> list[dict[str, object]]:
    """44 small + 4 large, on a 7x8 canvas invented to land on that total —
    see the module docstring. Reading order (row_idx, col_idx) ascending is
    already the enumeration order, so `sort_order` is just `index * 10`.
    """
    slots: list[dict[str, object]] = []
    order = 0

    for row in range(5):  # rows 0-4 ("A"-"E"): 8 small cells each = 40
        for col in range(8):
            slots.append(
                {
                    "slot_label": f"{_row_letter(row)}{col + 1}",
                    "row_idx": row,
                    "col_idx": col,
                    "row_span": 1,
                    "col_span": 1,
                    "size_class": "small",
                    "sort_order": order,
                }
            )
            order += 10

    for col in range(4):  # row 5 ("F"): 4 more small cells; cols 4-7 stay a gap
        slots.append(
            {
                "slot_label": f"F{col + 1}",
                "row_idx": 5,
                "col_idx": col,
                "row_span": 1,
                "col_span": 1,
                "size_class": "small",
                "sort_order": order,
            }
        )
        order += 10

    for col in (0, 2, 4, 6):  # row 6 ("G"): 4 large drawers, each 2 base cols wide
        slots.append(
            {
                "slot_label": f"G{col + 1}",
                "row_idx": 6,
                "col_idx": col,
                "row_span": 1,
                "col_span": 2,
                "size_class": "large",
                "sort_order": order,
            }
        )
        order += 10

    return slots


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    container_types = sa.table(
        "container_types",
        sa.column("slug", sa.String),
        sa.column("display_name", sa.String),
        sa.column("description", sa.Text),
        sa.column("child_layout", sa.String),
        sa.column("grid_rows", sa.Integer),
        sa.column("grid_cols", sa.Integer),
        sa.column("grid_pitch_mm", sa.Float),
        sa.column("grid_height_unit_mm", sa.Float),
        sa.column("footprint_cols", sa.Integer),
        sa.column("footprint_rows", sa.Integer),
        sa.column("footprint_height_u", sa.Integer),
        sa.column("slot_label_scheme", sa.String),
        sa.column("slot_label_params_json", sa.Text),
        sa.column("materialize_slots", sa.Boolean),
        sa.column("capacity_model", sa.String),
        sa.column("capacity_slots", sa.Integer),
        sa.column("inner_length_mm", sa.Float),
        sa.column("inner_width_mm", sa.Float),
        sa.column("inner_height_mm", sa.Float),
        sa.column("is_seed", sa.Boolean),
        sa.column("created_at", sa.String),
        sa.column("updated_at", sa.String),
    )
    slot_templates = sa.table(
        "container_type_slot_templates",
        sa.column("container_type_id", sa.Integer),
        sa.column("slot_label", sa.String),
        sa.column("row_idx", sa.Integer),
        sa.column("col_idx", sa.Integer),
        sa.column("row_span", sa.Integer),
        sa.column("col_span", sa.Integer),
        sa.column("size_class", sa.String),
        sa.column("sort_order", sa.Integer),
    )
    physical = sa.table(
        "container_type_physical",
        sa.column("container_type_id", sa.Integer),
        sa.column("gridfinity_u_w", sa.Integer),
        sa.column("gridfinity_u_d", sa.Integer),
        sa.column("gridfinity_u_h", sa.Integer),
        sa.column("generator", sa.String),
        sa.column("generator_params_json", sa.Text),
        sa.column("tag_pocket", sa.String),
    )

    def insert_type(**fields: object) -> int:
        row = {
            "child_layout": "none",
            "slot_label_scheme": "row_alpha_col_num",
            "materialize_slots": False,
            "is_seed": True,
            "created_at": _SEEDED_AT,
            "updated_at": _SEEDED_AT,
            **fields,
        }
        bind.execute(container_types.insert().values(**row))
        # The lightweight `sa.table()` above declares no primary key, so
        # `inserted_primary_key` has nothing to report; `slug` is unique and
        # already committed, so looking it back up is simplest.
        return int(
            bind.execute(
                sa.text("SELECT id FROM container_types WHERE slug = :slug"), {"slug": row["slug"]}
            ).scalar_one()
        )

    # -- Gridfinity baseplates ------------------------------------------------
    for cols, rows in _BASEPLATE_SIZES:
        insert_type(
            slug=f"gridfinity-baseplate-{cols}x{rows}",
            display_name=f"Gridfinity baseplate {cols}x{rows}",
            description=(
                f"{cols * 41.5:.1f} x {rows * 41.5:.1f} mm baseplate, {cols}x{rows} "
                "Gridfinity units. Verified spec: 42 mm grid pitch."
            ),
            child_layout="grid",
            grid_rows=rows,
            grid_cols=cols,
            grid_pitch_mm=_PITCH_MM,
            grid_height_unit_mm=_HEIGHT_UNIT_MM,
            capacity_model="grid_units",
        )

    # -- Gridfinity bins -------------------------------------------------------
    for cols, rows, height_u in _BIN_FOOTPRINTS:
        # PLAN.md's own formula, `(41.5*cols) x (41.5*rows) x (7*height_u)`,
        # less a wall/lip allowance. The allowance (3 mm horizontal, 5 mm
        # vertical) is a rough placeholder, not a measurement — flagged the
        # same way the module docstring flags the cabinet layouts below.
        outer_l, outer_w, outer_h = cols * 41.5, rows * 41.5, height_u * _HEIGHT_UNIT_MM
        type_id = insert_type(
            slug=f"gridfinity-bin-{cols}x{rows}x{height_u}",
            display_name=f"Gridfinity bin {cols}x{rows}x{height_u}u",
            description=(
                f"{outer_l:.1f} x {outer_w:.1f} x {outer_h:.1f} mm outer, "
                f"{cols}x{rows} footprint units, {height_u}u tall."
            ),
            footprint_cols=cols,
            footprint_rows=rows,
            footprint_height_u=height_u,
            grid_pitch_mm=_PITCH_MM,
            grid_height_unit_mm=_HEIGHT_UNIT_MM,
            capacity_model="volume",
            inner_length_mm=outer_l - 3.0,
            inner_width_mm=outer_w - 3.0,
            inner_height_mm=outer_h - 5.0,
        )
        bind.execute(
            physical.insert().values(
                container_type_id=type_id,
                gridfinity_u_w=cols,
                gridfinity_u_d=rows,
                gridfinity_u_h=height_u,
                generator=_GENERATOR,
                generator_params_json=f'{{"gridx": {cols}, "gridy": {rows}, "gridz": {height_u}}}',
                tag_pocket="bottom",
            )
        )

    # -- Akro-Mils 10144: 44 small + 4 large, materialised from the start -----
    akro_id = insert_type(
        slug="akro-mils-10144",
        display_name="Akro-Mils 10144",
        description=(
            "44 small + 4 large drawers; overall 20 x 6-3/8 x 15-13/16 in, "
            "polystyrene, molded label slots. The per-drawer layout below is "
            "reconstructed to match that count, not measured — see this "
            "migration's module docstring."
        ),
        child_layout="list",
        grid_rows=7,
        grid_cols=8,
        slot_label_scheme="row_alpha_col_num",
        materialize_slots=True,
        capacity_model="slots",
        capacity_slots=48,
    )
    for slot in _akro_mils_slots():
        bind.execute(slot_templates.insert().values(container_type_id=akro_id, **slot))

    # -- Raaco C8-30 / C10-40: full-width drawers, a plain pure grid ----------
    # "C8"/"C10" is Raaco's own model-family naming, not a column count — kept
    # as a display slug only, per the "type is a cosmetic prefix, never
    # parsed" rule elsewhere in this schema.
    for slug, display, drawer_count in (
        ("raaco-c8-30", "Raaco C8-30", 30),
        ("raaco-c10-40", "Raaco C10-40", 40),
    ):
        insert_type(
            slug=slug,
            display_name=display,
            description=(
                f"{drawer_count} full-width PP drawers in a painted-steel carcass; "
                "full-width label holders (~18x87 mm cards). Overall external "
                "dimensions are in docs/PLAN.md, not repeated here."
            ),
            child_layout="list",
            grid_rows=drawer_count,
            grid_cols=1,
            slot_label_scheme="sequential",
            slot_label_params_json='{"zero_pad": 2}',
            capacity_model="slots",
            capacity_slots=drawer_count,
        )


def downgrade() -> None:
    """Un-seed — but only the types nothing is standing in.

    `container_type_slot_templates` and `container_type_physical` both carry
    `ON DELETE CASCADE` back to `container_types`, so their rows go with the
    parent. **`locations.container_type_id` does not**: it is `ON DELETE
    RESTRICT`, deliberately, because a container type is the physical fact that
    a drawer *is* a Raaco C8-30 drawer and deleting it out from under 96 live
    drawers would leave them describing nothing. That is the right constraint
    and this migration must not fight it.

    So an unconditional delete of every seed slug raises `FOREIGN KEY
    constraint failed` on any database where somebody actually used one — which
    is every database that got past the first afternoon, and was true of the
    dev database here. Downgrading a *data* seed is best-effort by nature: the
    unused rows go, the in-use ones stay and are named on stdout. Nothing is
    orphaned and nothing is destroyed, which is the only outcome worth having
    for a migration whose whole purpose is to be reversible.
    """
    bind = op.get_bind()
    seed_slugs = (
        [f"gridfinity-baseplate-{cols}x{rows}" for cols, rows in _BASEPLATE_SIZES]
        + [f"gridfinity-bin-{cols}x{rows}x{height_u}" for cols, rows, height_u in _BIN_FOOTPRINTS]
        + ["akro-mils-10144", "raaco-c8-30", "raaco-c10-40"]
    )
    in_use = {
        slug: count
        for slug, count in bind.execute(
            sa.text(
                "SELECT ct.slug, COUNT(l.id) FROM container_types AS ct "
                "JOIN locations AS l ON l.container_type_id = ct.id "
                "WHERE ct.slug IN :slugs GROUP BY ct.slug"
            ).bindparams(sa.bindparam("slugs", value=seed_slugs, expanding=True))
        ).all()
    }
    removable = [slug for slug in seed_slugs if slug not in in_use]
    if removable:
        container_types = sa.table("container_types", sa.column("slug", sa.String))
        bind.execute(container_types.delete().where(container_types.c.slug.in_(removable)))
    if in_use:
        kept = ", ".join(f"{slug} ({count} in use)" for slug, count in sorted(in_use.items()))
        print(f"4cada779f255: kept seed container types still referenced by locations: {kept}")
