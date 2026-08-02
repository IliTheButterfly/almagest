"""front dimensions for the Raaco seeds, so a seeded cabinet can print a card

A fresh install's cabinets could not print. `POST /api/labels/sheets` sizes a
card from `container_types.front_width_mm/front_height_mm` and **raises rather
than guessing** when they are absent, which is right — a made-up default would
print at whatever size happened to be convenient rather than at what the drawer
actually is. The seed library shipped without them, and there was no way to
supply them afterwards: `PATCH /api/container-types/{id}` *clones* a seed rather
than mutating it, and no route repoints a standing location at the clone. So the
answer to "print a card for this drawer" was 422, permanently, for every
container created from the library.

Only the two Raaco types are filled in here, and the numbers are not researched
— they are read off the seed's **own committed description**, which says
"full-width label holders (~18x87 mm cards)". `card_size_mm` subtracts
`LIP_MARGIN_WIDTH_MM`/`HEIGHT_MM` (6 and 4, pinned to PLAN.md's worked example),
so a 93 x 22 mm front yields exactly the 87 x 18 mm card the description names.

**Akro-Mils is deliberately left null.** Its description says "molded label
slots" and gives no card size, and the drawer-front dimensions are not in
PLAN.md either. Inventing a plausible number would produce cards that are subtly
the wrong size and look deliberate — the one failure this column's not-null check
exists to prevent. Somebody with the cabinet in front of them can now measure it
and enter it: `ContainerTypeForm` gained the two fields in the same change.

`UPDATE ... WHERE front_width_mm IS NULL` so an install that already measured its
own is left alone.

Revision ID: d3b8e5f1a274
Revises: c7d9a3e14b62
Create Date: 2026-08-02 18:10:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3b8e5f1a274"
down_revision: Union[str, Sequence[str], None] = "c7d9a3e14b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 93 x 22 mm front - (6, 4) lip margin = the 87 x 18 mm card the seed's own
#: description names.
_RAACO_FRONT_WIDTH_MM = 93.0
_RAACO_FRONT_HEIGHT_MM = 22.0


def upgrade() -> None:
    """Upgrade schema."""
    op.get_bind().execute(
        sa.text(
            "UPDATE container_types"
            " SET front_width_mm = :width, front_height_mm = :height"
            " WHERE slug IN ('raaco-c8-30', 'raaco-c10-40')"
            "   AND front_width_mm IS NULL AND front_height_mm IS NULL"
        ),
        {"width": _RAACO_FRONT_WIDTH_MM, "height": _RAACO_FRONT_HEIGHT_MM},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.get_bind().execute(
        sa.text(
            "UPDATE container_types"
            " SET front_width_mm = NULL, front_height_mm = NULL"
            " WHERE slug IN ('raaco-c8-30', 'raaco-c10-40')"
            "   AND front_width_mm = :width AND front_height_mm = :height"
        ),
        {"width": _RAACO_FRONT_WIDTH_MM, "height": _RAACO_FRONT_HEIGHT_MM},
    )
