"""Which pictogram a container is drawn with.

Two rungs, not three — the deliberate contrast with `app.services.views`, which
this module is otherwise a sibling of:

    instance override  →  locations.glyph
    type default       →  container_types.glyph

There is no derived rung. `resolve_child_view` can fall back to reading a type's
declared geometry because a 42 mm pitch really does imply "a tray seen from
above"; nothing about a type's geometry implies what it *looks like*, so a
"derived glyph" would be a guess wearing the shape of a fact. `None` all the way
down is therefore a real, terminal answer — "no glyph chosen" — and the caller
(the frontend's dense recursive map, `LocationRead`, `ContainerTypeRead`) renders
a neutral placeholder for it rather than inventing one.

**Returns `str | None`, not `ContainerGlyph | None`.** Exactly the promise
`resolve_child_view` makes for the same reason: a glyph name written by a newer
build passes through untouched rather than raising in `ContainerGlyph(...)`, and
the frontend's own fallback map (`frontend/src/lib/locations/glyphs.ts`) treats an
unrecognised name the same as `None` — a placeholder, never a crash.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.storage import ContainerType, Location


def resolve_glyph(location: Location | None, container_type: ContainerType | None) -> str | None:
    """The effective glyph for one container: its own override, else its
    type's, else `None`."""
    if location is not None and location.glyph is not None:
        return location.glyph
    if container_type is not None:
        return container_type.glyph
    return None


def resolve_glyphs(session: Session, locations: Iterable[Location]) -> dict[int, str | None]:
    """`resolve_glyph` for a whole tree, in one extra query.

    Same shape as `app.services.views.resolve_child_views`, and for the same
    reason: `GET /api/locations/tree` returns every node in one response, so
    resolving per node would be one `container_types` lookup per location.
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
    return {
        row.id: resolve_glyph(
            row, types.get(row.container_type_id) if row.container_type_id is not None else None
        )
        for row in rows
    }
