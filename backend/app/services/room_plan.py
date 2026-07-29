"""Drawn rooms and placed containers — ADR 0009.

Two facts the schema did not carry, and they are deliberately different shapes.

**The room's outline** is drawn geometry that is not a container: walls, a door,
the bench that holds nothing. It lives in `location_plan_shapes` +
`location_plan_shape_points`, one polyline per shape, one row per vertex, all
integers in the room's own millimetres. There is no geometry library here, no
spatial index, and nothing parses a path string — a room has tens of vertices
and the only question ever asked of them is "draw this".

**A placement** is five nullable integers on the *child* (`plan_x_mm`,
`plan_y_mm`, `plan_rotation_deg`, `plan_width_mm`, `plan_depth_mm`) plus
`plan_parent_id`, the parent those coordinates were authored against.

The one rule worth stating twice: **a placement is valid only while
`plan_parent_id == parent_id`.** A coordinate is meaningless in another room, so
rather than trusting every present and future write path to remember to clear it,
the read decides. `placement_of()` is the only reader of those columns anywhere,
and it returns `None` for a stale one — so a container moved to a different room
by a move endpoint, a bulk import or a hand-written `UPDATE` is *unplaced*, with
no trigger and no hook. `forget_placement()` then clears the dead columns
eagerly wherever a write path notices them, which is tidiness rather than
correctness.

Nothing here is validated against `child_view`. A placement may be authored on a
container drawn as a cabinet face, exactly as ADR 0006 lets any container be
drawn as a floor plan: refusing would be the editor overruling the person holding
the furniture, and the coordinates cost nothing while unused.

**Coordinates are authoring data.** They never reach a `short_id`, a label or a
tag payload — the same rule that keeps hierarchy off a tag, for the same reason:
a container that moves takes its label with it and leaves the coordinate behind.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.storage import (
    ContainerType,
    Location,
    LocationPlanShape,
    LocationPlanShapePoint,
)


@dataclass(frozen=True)
class Placement:
    """Where one child stands in one parent, resolved.

    Only ever constructed for a placement that is both complete and current —
    `placement_of()` returns `None` otherwise, so a caller holding one of these
    never has to re-check either condition.
    """

    location_id: int
    parent_id: int
    x_mm: int
    y_mm: int
    rotation_deg: int
    #: The drawn footprint if one was authored, else the container type's
    #: physical size rounded to whole millimetres, else `None` — which means
    #: "draw a nominal box", not "zero size".
    width_mm: int | None
    depth_mm: int | None


@dataclass(frozen=True)
class Point:
    x_mm: int
    y_mm: int


@dataclass(frozen=True)
class Shape:
    """A drawn polyline and its vertices, in seq order."""

    id: int
    kind: str
    label: str | None
    is_closed: bool
    thickness_mm: int | None
    sort_order: int
    points: list[Point]


@dataclass(frozen=True)
class Extent:
    """The bounding box of everything drawn or placed in one room.

    **Derived, never stored** — the same call the layout editor's `grid_rows`/
    `grid_cols` makes. A stored canvas size would be a second fact to keep in
    step with the shapes it is supposed to contain, and the first thing drawn
    outside it would be invisible rather than obviously outside.
    """

    min_x_mm: int
    min_y_mm: int
    max_x_mm: int
    max_y_mm: int


# ---------------------------------------------------------------------------
# Placements
# ---------------------------------------------------------------------------


def _type_footprint(container_type: ContainerType | None) -> tuple[int | None, int | None]:
    """The physical box a type occupies on a floor, in whole millimetres.

    `front_width_mm` and `inner_length_mm` are the honest pair: what you see
    standing in front of a cabinet, and how deep it reaches into the room.
    `inner_*` is inside dimensions and therefore an under-estimate of the
    outside, which is stated rather than corrected by a guessed wall thickness.
    """
    if container_type is None:
        return None, None
    width = container_type.front_width_mm
    depth = container_type.inner_length_mm
    return (
        round(width) if width is not None else None,
        round(depth) if depth is not None else None,
    )


def placement_of(session: Session, location: Location) -> Placement | None:
    """This container's placement in its current parent, or `None`.

    `None` covers three genuinely different situations that a floor plan draws
    the same way — as a container sitting in the room's "not placed yet" tray:

    * never dragged anywhere (`plan_x_mm IS NULL`),
    * placed, then moved to another parent (`plan_parent_id != parent_id`),
    * a root, which stands in no parent at all.

    They are not distinguished on the wire because the answer to all three is
    the same gesture: drag it somewhere.
    """
    if location.parent_id is None or location.plan_parent_id != location.parent_id:
        return None
    if location.plan_x_mm is None or location.plan_y_mm is None:
        return None
    type_width, type_depth = _type_footprint(
        session.get(ContainerType, location.container_type_id)
        if location.container_type_id is not None
        else None
    )
    return Placement(
        location_id=location.id,
        parent_id=location.parent_id,
        x_mm=location.plan_x_mm,
        y_mm=location.plan_y_mm,
        rotation_deg=location.plan_rotation_deg or 0,
        width_mm=location.plan_width_mm if location.plan_width_mm is not None else type_width,
        depth_mm=location.plan_depth_mm if location.plan_depth_mm is not None else type_depth,
    )


def forget_placement(location: Location) -> None:
    """Clear the placement columns outright.

    Belt to `placement_of()`'s braces: the read guard already makes a stale
    placement invisible, so this exists to stop a row *carrying* coordinates
    that mean nothing, not to make the invalidation work. Call it from anything
    that reparents a location.
    """
    location.plan_parent_id = None
    location.plan_x_mm = None
    location.plan_y_mm = None
    location.plan_rotation_deg = None
    location.plan_width_mm = None
    location.plan_depth_mm = None


def place(
    location: Location,
    *,
    parent_id: int,
    x_mm: int,
    y_mm: int,
    rotation_deg: int = 0,
    width_mm: int | None = None,
    depth_mm: int | None = None,
) -> None:
    """Author one child's coordinate. `parent_id` is stamped, not assumed."""
    location.plan_parent_id = parent_id
    location.plan_x_mm = x_mm
    location.plan_y_mm = y_mm
    location.plan_rotation_deg = rotation_deg
    location.plan_width_mm = width_mm
    location.plan_depth_mm = depth_mm


def children_of(session: Session, parent: Location) -> list[Location]:
    return list(
        session.execute(
            select(Location)
            .where(Location.parent_id == parent.id)
            .order_by(Location.sort_order, Location.id)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def shapes_of(session: Session, parent: Location) -> list[Shape]:
    rows = list(
        session.execute(
            select(LocationPlanShape)
            .where(LocationPlanShape.location_id == parent.id)
            .order_by(LocationPlanShape.sort_order, LocationPlanShape.id)
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    by_shape: dict[int, list[Point]] = {row.id: [] for row in rows}
    for point in (
        session.execute(
            select(LocationPlanShapePoint)
            .where(LocationPlanShapePoint.shape_id.in_(by_shape))
            .order_by(LocationPlanShapePoint.shape_id, LocationPlanShapePoint.seq)
        )
        .scalars()
        .all()
    ):
        by_shape[point.shape_id].append(Point(x_mm=point.x_mm, y_mm=point.y_mm))
    return [
        Shape(
            id=row.id,
            kind=row.kind,
            label=row.label,
            is_closed=row.is_closed,
            thickness_mm=row.thickness_mm,
            sort_order=row.sort_order,
            points=by_shape[row.id],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class ShapeDraft:
    """One shape as the editor sends it. Ids are not carried on purpose."""

    kind: str
    points: list[Point]
    label: str | None = None
    is_closed: bool = False
    thickness_mm: int | None = None


def replace_shapes(session: Session, parent: Location, drafts: list[ShapeDraft]) -> list[Shape]:
    """Replace a location's entire drawn plan in one write.

    **Whole-plan replacement rather than per-shape CRUD**, and that is the same
    decision the slot layout editor already made: a drawing session ends with
    "this is the room now", not with a stream of inserts and deletes whose order
    matters. It also means the client never has to hold shape ids, so redrawing a
    wall is not a diff — and a batched save cannot half-apply.

    Shape ids therefore change on every save. Nothing references them: they are
    not a `short_id`, they are not printed, and no tag carries one.
    """
    existing = list(
        session.execute(
            select(LocationPlanShape.id).where(LocationPlanShape.location_id == parent.id)
        )
        .scalars()
        .all()
    )
    if existing:
        # Points first: the FK is `ON DELETE CASCADE`, but SQLite only honours
        # that with `PRAGMA foreign_keys=ON`, and leaving correctness to a pragma
        # set elsewhere is how orphan rows appear in a fresh deployment.
        session.execute(
            delete(LocationPlanShapePoint).where(LocationPlanShapePoint.shape_id.in_(existing))
        )
        session.execute(delete(LocationPlanShape).where(LocationPlanShape.id.in_(existing)))
    for order, draft in enumerate(drafts):
        shape = LocationPlanShape(
            location_id=parent.id,
            kind=draft.kind,
            label=draft.label,
            is_closed=draft.is_closed,
            thickness_mm=draft.thickness_mm,
            sort_order=order,
        )
        session.add(shape)
        session.flush()
        for seq, point in enumerate(draft.points):
            session.add(
                LocationPlanShapePoint(shape_id=shape.id, seq=seq, x_mm=point.x_mm, y_mm=point.y_mm)
            )
    session.flush()
    return shapes_of(session, parent)


# ---------------------------------------------------------------------------
# Extent
# ---------------------------------------------------------------------------


def extent(shapes: list[Shape], placements: list[Placement]) -> Extent | None:
    """The bounding box of a room's drawing, or `None` for an empty one.

    `None` is a real answer and must stay reportable: a room with nothing drawn
    and nothing placed has no size, and inventing a default canvas would make the
    client draw a box that is not there.
    """
    xs: list[int] = []
    ys: list[int] = []
    for shape in shapes:
        for point in shape.points:
            xs.append(point.x_mm)
            ys.append(point.y_mm)
    for placement in placements:
        xs.append(placement.x_mm)
        ys.append(placement.y_mm)
        # The footprint is included when known so a cabinet standing at the
        # right-hand wall is not half outside the extent the client sizes its
        # canvas from.
        xs.append(placement.x_mm + (placement.width_mm or 0))
        ys.append(placement.y_mm + (placement.depth_mm or 0))
    if not xs or not ys:
        return None
    return Extent(min_x_mm=min(xs), min_y_mm=min(ys), max_x_mm=max(xs), max_y_mm=max(ys))
