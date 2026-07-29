"""How a level of the storage tree is drawn — ADR 0006.

One function, three rungs, and the *same* three rungs at every depth. That is
the whole module, and the fact that it is this small is the point: a renderer
that asked "am I at the top?" or "is this a cabinet?" would grow a special case
per level and contradict a schema (`locations.parent_id`, ADR 0002) that has no
named levels at all.

    instance override  →  locations.child_view
    type default       →  container_types.child_view
    derived            →  derive_child_view(), from geometry already declared

The third rung is why nothing needed backfilling when the two columns landed.
A baseplate that declares a 42 mm pitch has already said "cells seen from
above"; a Raaco whose canvas is 30x1 drawers has already said "a face of
drawer fronts". Storing a copy of that would be a second version of the same
fact, free to drift from the geometry it was read off — the same reasoning that
keeps `ShortageKind` and `PromotionOutcome` out of the database.

**Returns `str`, not `ChildView`.** A value written by a newer build passes
through untouched rather than raising in `ChildView(...)`, which is the other
half of the promise the no-`CHECK` rule makes (see
`tests/integration/test_schema_invariants.py::
test_an_unknown_enum_value_already_in_the_database_still_reads`). Callers
compare against `ChildView` members, which works without coercion because
`StrEnum` members *are* `str`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ChildLayout, ChildView
from app.models.storage import ContainerType, Location


def derive_child_view(container_type: ContainerType | None) -> ChildView:
    """The drawing implied by the geometry a type already declares.

    Read as a cascade over the two ADR 0002 questions:

    * presents a **measured** grid (a declared pitch) → cells seen from above.
      Gridfinity is the reference case, and the pitch is exactly what makes it
      one — a grid whose cells are a known size in millimetres is a tray.
    * presents an unmeasured grid, or a `list` with a declared canvas → a face
      of drawer fronts. Both off-the-shelf cabinets in the seed library are the
      second form: `child_layout='list'` with a 7x8 or 30x1 canvas.
    * says `list` with no canvas → rows. It has already said its children are a
      sequence, so there is nothing to second-guess.
    * presents nothing, but **occupies** a measured footprint → rows. This is a
      Gridfinity bin, whose children are its own dividers; they have an order
      and no geometry worth drawing.
    * presents nothing and occupies nothing → a floor plan. Furniture and
      spaces: a workshop, a room, a bench. Also where a location with no
      container type lands, which is what makes the outermost level of the tree
      fall out of the same rule instead of needing its own.

    An unrecognised `child_layout` — a row written by a newer build — takes the
    last two branches rather than raising, so a level drawn by an older client
    is drawn plainly instead of not at all.
    """
    if container_type is None:
        return ChildView.FLOOR_PLAN

    if container_type.child_layout == ChildLayout.GRID:
        if container_type.grid_pitch_mm is not None:
            return ChildView.GRID_CELLS
        return ChildView.CABINET_FACE

    if container_type.child_layout == ChildLayout.LIST:
        declares_canvas = (
            container_type.grid_rows is not None and container_type.grid_cols is not None
        )
        return ChildView.CABINET_FACE if declares_canvas else ChildView.LIST

    occupies_a_grid = (
        container_type.footprint_cols is not None or container_type.footprint_rows is not None
    )
    return ChildView.LIST if occupies_a_grid else ChildView.FLOOR_PLAN


def resolve_child_view(location: Location | None, container_type: ContainerType | None) -> str:
    """The effective view for `location`'s own children.

    `location=None` is **the world**: the outermost level, whose children are
    the roots of the tree. It resolves through the identical "no container type"
    branch as a location that simply has none, so the top of the tree is drawn by
    the same rule as everything under it rather than by a hardcoded default at
    depth 0.
    """
    if location is not None and location.child_view is not None:
        return location.child_view
    if container_type is not None and container_type.child_view is not None:
        return container_type.child_view
    return derive_child_view(container_type)


def child_canvas(container_type: ContainerType | None) -> tuple[int | None, int | None]:
    """The `(rows, cols)` canvas a type presents to its children, or `(None, None)`.

    Straight off `grid_rows`/`grid_cols` — no default, no inference. It is
    reported for its own sake rather than because a particular view needs it: a
    level that declares no canvas must *say* so, because the client refuses to
    draw a slotted view without one instead of guessing a column count.

    Carried on the wire for one specific reason. `derive_child_view` above reads
    these same two columns to promise `cabinet_face` — "a Raaco's 30x1 canvas has
    already said 'a face of drawer fronts'" (ADR 0006) — and a client that could
    not see them could not honour that promise for a type whose slot labels are a
    plain sequence, because a sequential label carries an order and no column. The
    fact that decides the picture and the fact that makes it drawable are the same
    fact, so they travel together.
    """
    if container_type is None:
        return (None, None)
    return (container_type.grid_rows, container_type.grid_cols)


@dataclass(frozen=True)
class ChildDrawing:
    """Everything the recursive map needs in order to draw one level's children:
    which picture, and the canvas to draw it on."""

    view: str
    grid_rows: int | None
    grid_cols: int | None


def resolve_child_drawings(
    session: Session, locations: Iterable[Location]
) -> dict[int, ChildDrawing]:
    """`resolve_child_view` and `child_canvas` for a whole tree, in one extra query.

    `GET /api/locations/tree` returns every node in one response, so resolving
    per node would be one `container_types` lookup per location — the N+1 the
    rest of that route is carefully built to avoid. The two answers are resolved
    together rather than in two passes because they read the *same* type row, and
    a second pass would be a second query for it.
    """
    rows = list(locations)
    type_ids = {row.container_type_id for row in rows if row.container_type_id is not None}
    types: dict[int, ContainerType] = {}
    if type_ids:
        types = {
            container_type.id: container_type
            for container_type in session.execute(
                select(ContainerType).where(ContainerType.id.in_(type_ids))
            ).scalars()
        }

    drawings: dict[int, ChildDrawing] = {}
    for row in rows:
        container_type = (
            types.get(row.container_type_id) if row.container_type_id is not None else None
        )
        grid_rows, grid_cols = child_canvas(container_type)
        drawings[row.id] = ChildDrawing(
            view=resolve_child_view(row, container_type),
            grid_rows=grid_rows,
            grid_cols=grid_cols,
        )
    return drawings


def resolve_child_views(session: Session, locations: Iterable[Location]) -> dict[int, str]:
    """Just the view, for callers that want nothing else."""
    return {
        location_id: drawing.view
        for location_id, drawing in resolve_child_drawings(session, locations).items()
    }
