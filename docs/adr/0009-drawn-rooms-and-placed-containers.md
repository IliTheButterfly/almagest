# ADR 0009 — Draw the room; place the containers in it

**Status:** accepted
**Date:** 2026-07-29
**Closes:** the "Deferred" paragraph of [ADR 0006](0006-per-layer-child-view.md),
which named this exact gap and refused to guess at it.

## Context

The request was "for the room layout, we should be able to draw a room and lay
containers out in it."

ADR 0006 introduced `child_view=floor_plan` — "furniture standing in a space" —
and was explicit that the *positions* were not built:

> `floor_plan` today flows its cards in the order the containers were created,
> because `locations` stores no x/y for a container standing in a room. […]
> drawing a fake plan from creation order would be the "guessed grid puts a
> drawer somewhere it is not" failure that `slots.ts` already refuses to commit.

What the schema had was `row_idx`/`col_idx`/`row_span`/`col_span` — integer cells
on a parent's **slot canvas** — plus `size_class` and `sort_order`. Nothing
positional in a *space*: no x/y, no rotation, no room extent, no wall. Two
distinct facts were missing, and conflating them is the trap:

1. **The room's own shape.** Not a bounding box. Rooms have alcoves, and the
   alcove is usually exactly where the bench goes; a workshop's usable wall is
   the thing you lay a bench against. A width/depth pair cannot say that.
2. **Where each child stands.** ADR 0006's rule stands: a floor plan has "no
   empty positions, because a space has none". So a placement is a *coordinate*,
   never a slot, and it must not be smuggled into the slot columns.

## Decision

**Two shapes, because they are two different kinds of fact.**

### (a) The outline: `location_plan_shapes` + `location_plan_shape_points`

One row per drawn thing, one row per vertex, every coordinate an INTEGER
millimetre in the location's own frame. `kind` is a `PlanShapeKind` on a plain
`VARCHAR` — `outline`, `wall`, `door`, `window`, `fixture`, `zone` — plus a free
`label`, an `is_closed` flag, an advisory `thickness_mm` and a `sort_order`.

Every kind is the same primitive: an ordered polyline. A closed one is the room
outline or a zone; a two-point one is a wall run or a door swing; a four-point
closed one is the sink. One representation means one renderer branch and no
special cases, and it is the shape that admits an alcove without asking anything
of the schema.

**A drawn wall is not a location, and this table exists to keep it that way.** A
wall has no `short_id`, holds no stock, resolves from no scan, and must never
appear in the tree. Had it been a `locations` row with a kind on it, the physical
tree would contain furniture nobody can put anything in, and
`app.services.assignment` would have to learn to skip it — a new way for a scan
to land somewhere absurd, bought for nothing. `ON DELETE CASCADE` on
`location_id`, uniquely among references to `locations` in this schema: every
other one is `RESTRICT` because deleting a cabinet must never silently take its
drawers and their contents with it, and a drawing of a wall has no contents.

### (b) The placement: six nullable INTEGER columns on the child

`plan_x_mm`, `plan_y_mm`, `plan_rotation_deg`, `plan_width_mm`, `plan_depth_mm`,
and `plan_parent_id`. Per-parent authoring data, on the child, in millimetres.

**All of it nullable, and NULL is a real state.** A container added to a room and
never dragged anywhere is *unplaced*, and the API says so in its own field
(`unplaced_location_ids`) rather than by omission. Defaulting to (0, 0) would put
every pre-existing container in the same corner of every room and look authored —
the same failure ADR 0006 refused for guessed grids. So there is no backfill.
"Return this to the tray" is likewise its own request field, not a coordinate,
because no coordinate means "nowhere".

Coordinates are **signed**: the origin is wherever the person drawing put it, and
requiring it to be a corner of the room would make the first wall they drew the
wrong one.

`plan_width_mm`/`plan_depth_mm` fall back to the container type's
`front_width_mm`/`inner_length_mm` when null, which is the common case; they exist
because the type library says a Raaco is 306 mm wide and the shelf it is bolted to
is not in the type library.

### The reparent rule: `plan_parent_id == parent_id`, checked on read

A coordinate is meaningless in another room. Rather than trusting every present
and future write path to clear it, **the read decides**:
`app.services.room_plan.placement_of()` is the only reader of those columns
anywhere, and it returns `None` unless `plan_parent_id` still equals the row's
current `parent_id`. A container reparented by a future move endpoint, a bulk
import or a hand-written `UPDATE` is therefore unplaced, with no trigger, no hook
and nothing to remember. `TreeRepository.move()` additionally clears the dead
columns, which is tidiness, not what makes it correct — and
`test_the_invalidation_survives_a_reparent_that_never_heard_of_it` reparents with
raw SQL, leaving the columns intact, to prove the difference.

`plan_parent_id` is a plain `INTEGER` with no foreign key. Practically, SQLite
cannot add one without rebuilding `locations`, and that rebuild is what
`20260729_0930_c31b7a5e9d04`'s downgrade note records as failing: batch mode
renames the table and SQLite re-parses every trigger on it mid-rename, against
`trg_stock_ledger_dirty_occupancy`. Semantically the two agree — this is a
*witness* ("these coordinates were authored while my parent was N"), so a value
pointing at a deleted row is not a broken link, it is exactly the stale placement
the column exists to detect.

### The routes

* `GET /api/locations/{id}/plan` — shapes, placements, unplaced children, and a
  derived `extent`. Never a 404 for an undrawn room: the editor has to be the
  thing you draw the first wall in. `extent` is null for an empty room, because a
  default canvas would make the client draw a box that is not there.
* `PUT /api/locations/{id}/plan/shapes` — **replaces the whole drawing.** A
  drawing session ends with "this is the room now", not with a stream of inserts
  and deletes whose order matters. The client never holds shape ids, so redrawing
  a wall is not a diff, and a batched save cannot half-apply. An empty list erases
  the plan, which is a real edit.
* `PUT /api/locations/{id}/plan/placements` — **batched.** Dragging five things
  and then saving is one request. Ids must be current children; an id in both
  `placements` and `unplace_location_ids`, or placed twice, is a 422 rather than a
  guess about which half was meant.

Nothing is validated against `child_view`, following ADR 0006's rule exactly:
drawing a room on a container that renders as a cabinet face is allowed and simply
unused. Refusing would be the editor overruling the person holding the furniture.

**Coordinates never reach a printed or tag payload** — the same rule that keeps
hierarchy off a tag, and the same reason: a cabinet on castors takes its label with
it and leaves the coordinate behind, and unlike the database a tag cannot be
corrected without holding it.

## Consequences

Additive. Two new tables and six nullable columns; no backfill, no `CHECK`
anywhere, and `PlanShapeKind` grows by one line plus a renderer branch. That is
the no-`CHECK` rule paying for itself for the fifth time, after
`CapacityModel.GRID_UNITS`, the scanning enums, `AllocationState.STAGED` and
`ChildView`.

`row_idx`/`col_idx` are untouched and still mean cells on a slot canvas. A
container can carry both a slot position and a floor-plan coordinate without
either meaning anything to the other, which is what makes a cabinet legible in
its room *and* a drawer legible in its cabinet.

Shape ids change on every save. Nothing may ever reference one — not a
`short_id`, not a print job, not a tag — and the whole-plan replacement is what
guarantees that stays cheap to keep true.

## Honest limits — what this cannot express

* **No third dimension.** A plan is a floor: `plan_x_mm`/`plan_y_mm` place a
  footprint, and a cabinet bolted to a wall at 1400 mm looks identical to one on
  the floor. Shelf *levels* remain `shelf_run`'s job, one level down the tree.
* **Rotation is not applied to the footprint.** A placed box's drawn extent uses
  its width/depth axis-aligned, so a cabinet at 45° reports a bounding box that
  is wrong by up to its diagonal. The `extent` is a canvas-sizing convenience, not
  a collision surface, and correcting it would be the first geometry function in
  the codebase.
* **Nothing detects overlap.** Two cabinets can be drawn in the same square metre.
  That is consistent with the rest of the schema — capacity is advisory and a scan
  is never rejected — and a drawn plan is a weaker claim than capacity, not a
  stronger one.
* **A placement is not remembered per parent.** Move a cabinet to the garage and
  back and it returns unplaced, not to its old spot. Remembering would need a
  `location_placements(child, parent)` table, and the coordinate is stale the
  moment the furniture moves anyway — a resurrected one is a lie that looks
  authored.
* **`plan_depth_mm` falls back to `inner_length_mm`**, which is an *inside*
  dimension and therefore under-states the outside of a cabinet by its wall
  thickness. Stated rather than corrected by a guessed constant. Draw the
  footprint if it matters.
* **The outline is not validated as a polygon.** Self-intersecting, non-planar,
  wound either way, one point repeated — all accepted, all drawn as given.
  Validating would need the geometry library this ADR is refusing.
* **No scale, no north, no units on screen.** Millimetres are the storage unit and
  the client's problem to render.

## Rejected alternatives

**A polygon column on `locations`** (`outline_json`, a WKT string, an SVG path).
Rejected on three counts. It can express only one shape per room, so the wall
inside the room and the bench that holds nothing have nowhere to live. It needs a
parser on both sides, and the first bug in a hand-rolled path parser is silent —
the same argument that keeps `parameter_value` as rows. And it makes the room's
drawing a property of the room *row*, which is precisely the conflation of "the
space I am" with "the furniture I contain" that this ADR is splitting.

**Walls as `locations` rows** with a `PlanShapeKind`. Tempting because the tree
already renders recursively, and wrong for the reason above: a wall would acquire a
`short_id` slot, appear in every tree render, count as a child, and become an
auto-assignment candidate. Every one of those is a new place for a scan to land
somewhere absurd.

**A geometry library or a spatial index.** `shapely`, an R-tree, SQLite's
`rtree` module. A room holds tens of vertices and the only question ever asked of
them is "draw this". This project's stated dominant risk is a solo maintainer
drowning in an over-engineered stack; a dependency that exists to answer questions
nobody has asked is exactly that.

**Storing the room's extent.** A `width_mm`/`depth_mm` pair on the room, or a
cached bounding box. It is derivable from the shapes and placements in one pass,
and a stored copy is a second fact to keep in step with the drawing it is supposed
to contain — with the failure mode that the first thing drawn outside it becomes
invisible rather than obviously outside. Derived, exactly as `LayoutRead`'s
`grid_rows`/`grid_cols` already are.

**Floats or metres.** Millimetre integers, like every other quantity in this
schema. A float coordinate accumulates error under repeated drag-and-save, and
sub-millimetre precision in a room is a claim no drawing tool can honour.

**Per-shape and per-placement CRUD routes.** A five-box rearrangement becomes five
requests that can partially fail, leaving the room in a state nobody authored.
