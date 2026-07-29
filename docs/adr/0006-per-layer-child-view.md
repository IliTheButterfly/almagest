# ADR 0006 — Each layer of the storage tree carries its own view type

**Status:** accepted
**Date:** 2026-07-29
**Extends:** [ADR 0002](0002-recursive-container-types.md), which it does not replace.

## Context

The storage editor is recursive, and the request that prompted this was that
each layer should have its own *type*: "grid, a type for the view for workshop,
a type for the view in workbench cabinet."

Three levels, three different pictures, one tree. ADR 0002 established that a
container type answers **two independent questions** — what grid it presents to
its children (`child_layout`, `grid_rows/cols`, `grid_pitch_mm`) and what
footprint it occupies in its parent (`footprint_*`). Neither of them answers
this one:

> **How should this container's children be drawn?**

The evidence that it is a third question and not a restatement of the first:

- A Raaco cabinet and a Gridfinity bin both answer `child_layout=list`. One
  wants drawer fronts in a vertical face; the other is a bin whose children are
  its own dividers, and wants rows. Same answer to the geometry question,
  different drawing. The same split exists on the other side of that enum: a
  baseplate that declares a 42 mm pitch and a grid that declares none both
  answer `child_layout=grid`, and only the first is a tray seen from above.
  (Both seeded off-the-shelf cabinets are `child_layout='list'` with a canvas,
  not `grid` — the grid-without-a-pitch case is real but unseeded, and
  `test_an_unmeasured_grid_derives_a_cabinet_face` is where it lives.)
- A workshop presents *no* grid — its children are cabinets standing on a floor,
  placed rather than slotted — and still has to render as something better than a
  bullet list. `child_layout=none` was the only thing the schema could say about
  it, and "none" is not a picture.
- The renderer already existed (`frontend/src/components/ContainerLayout.tsx`)
  and drew exactly two things: a grid when the slot labels parsed, a flow when
  they did not. That is a statement about *label legibility*, not about what the
  furniture is.

## Decision

**A third axis: `ChildView`, on `container_types.child_view` and
`locations.child_view`.** Five members, each a genuinely different drawing:

| Member | The picture | Empty positions |
|---|---|---|
| `floor_plan` | Furniture standing in a space. A flow of cards. | none — a room has no position to be empty |
| `shelf_run` | One horizontal run per shelf level; rows authored, columns not | none |
| `cabinet_face` | Drawer fronts, wide and short, slot label carried like the card in the real drawer | **drawn** |
| `grid_cells` | A tray seen from above; square cells of a measured grid | **drawn** |
| `list` | Rows. Dividers in a bin, a bag of bags | none |

**Not more `ChildLayout` members**, and the argument is mechanical rather than
aesthetic. `app.services.assignment` selects containers with
`ContainerType.child_layout == ChildLayout.GRID` in order to materialise a free
cell for a scan that would otherwise have nowhere to land. Every drawing kind
added to that enum would fall out of that predicate, so declaring a cabinet
`child_layout='cabinet_face'` would quietly remove it from auto-assignment —
a scan escalating to the `INBOX` because somebody picked a skin. Two axes cannot
fail that way: a cabinet still presents a grid whatever it looks like.
`ChildLayout` therefore stays exactly three members, and a test pins that.

**The value lives on the type, with a per-instance override** — the precedent
`locations` already sets three times over. `esd_safe`, `is_placeable` and
`fill_factor` are all nullable columns on `locations` meaning "use the container
type", against a non-null default on `container_types`; `LocationRead` reports
both the raw override and the resolved `effective_esd_safe`, because an editor
cannot offer "stop overriding this" without being able to tell a pin from a
coincidence. `child_view` copies that shape exactly, including reporting both.

**One difference from `esd_safe`: this is not inherited down the ancestor
chain.** ESD safety is a physical property that genuinely propagates — a cabinet
lined with dissipative foam makes its drawers safe — whereas a drawing is a fact
about one level's own children. Choosing a floor plan for a room must not
silently redraw every drawer inside it.

**A third rung under the type default: derivation.** `NULL` does not mean
"unknown", it means *derive it from the geometry this row already declares*
(`app.services.views.derive_child_view`):

```
measured grid (a declared pitch)        → grid_cells
unmeasured grid, or list with a canvas  → cabinet_face
list with no canvas                     → list
occupies a footprint, presents nothing  → list      (a bin's dividers)
neither presents nor occupies a grid    → floor_plan (a room; also: no type at all)
```

That is what made the migration a pure column add with **no backfill**: every
one of the **eleven** seed types (three baseplates, five bins, the Akro-Mils and
the two Raacos) already derives the right drawing. A baseplate's 42 mm pitch has
already said "tray seen from above"; a Raaco's 30×1 canvas has already said "a
face of drawer fronts". Writing those into the rows would be a stored copy of a
fact the geometry states, free to drift from it — the same reasoning that keeps
`ShortageKind` and `PromotionOutcome` out of the database.

`test_every_seed_type_derives_the_right_drawing` enumerates all eleven and
asserts the count, so this number cannot go stale the next time the seed library
grows.

**A derived drawing must be drawable by whoever receives it.** The derivation
above reads `grid_rows`/`grid_cols`, so those two columns travel to the client as
`LocationNode.child_grid_rows`/`child_grid_cols`. Without them the two Raacos
were a promise that could not be kept: their drawers are labelled `01`…`30` by
the `sequential` scheme, a sequential label carries an order and no column, and a
client with no canvas has nothing to lay a face out on. The rule is that the fact
which *decides* the picture and the fact which makes it *drawable* are the same
fact, and must not be separable — the alternative considered, weakening the
derivation for label schemes the client cannot place, would have made the picture
depend on how the slots happen to be named.

**`shelf_run` is authored and never derived.** Nothing in the schema
distinguishes a shelf from a cabinet today, and inventing the distinction from a
row count would be a guess presented as a drawing.

**Recursion is enforced by there being one resolver and one renderer.** The rule
is "the view of a level is `resolve_child_view(that level)`", and the outermost
level — the roots of the tree, whose parent is the world — resolves through the
*same* no-container-type branch as any location that simply has none. So there is
no hardcoded default at depth 0 to drift from the rule that applies everywhere
else. On the client, `ContainerLayout` reads
`childViewOf(index, parentId)` and calls itself for every child that has
children; a test renders a workshop, a cabinet and a baseplate at three nested
depths in one pass and asserts the three drawings appear, nested inside one
another in that order.

## Consequences

**Entirely additive**, and that is the no-`CHECK` rule paying for itself for the
fourth time — after `CapacityModel.GRID_UNITS` (ADR 0002), the scanning enums
grown after `scan_events` was already populated, and `AllocationState.STAGED`
(ADR 0004). Two nullable `VARCHAR` columns on tables that hold every container
in the building; a new way to draw a level is one member in
`app/models/enums.py` plus one branch in the renderer. Under a `CHECK`
constraint or `sa.Enum` it would have been a rebuild of `container_types`,
`locations`, and everything with a foreign key into either.

**Nothing is validated against the geometry, on purpose.** `PUT
/api/locations/{id}/child-view` accepts any member. Refusing to draw a cabinet
as a floor plan because its type declares a grid would be the editor overruling
the person holding the cabinet, and the grid machinery still knows where every
slot is either way — only the picture changes.

**The drawing is never encoded in a printed or tag payload.** A view kind is a
property of a level, and levels move; the same rule that keeps hierarchy off a
tag keeps this off it.

**Deferred:** authoring a floor plan's actual *positions*. `floor_plan` today
flows its cards in the order the containers were created, because `locations`
stores no x/y for a container standing in a room. Adding that is another
additive pair of nullable columns; drawing a fake plan from creation order would
be the "guessed grid puts a drawer somewhere it is not" failure that `slots.ts`
already refuses to commit.

## Rejected alternatives

**Growing `ChildLayout`.** The assignment predicate above. It also conflates two
things that change for different reasons: geometry changes when the furniture
changes, a drawing changes when somebody prefers a different picture.

**A view kind stored per *location* only, with no type default.** "Every Raaco
cabinet draws the same way" is a fact about the type, and storing it per instance
would mean setting it forty times and having the forty-first drawer be wrong.

**Deriving the view entirely, with no stored column.** Tempting, since the
derivation covers every case in the seed library — but it makes the drawing
unauthorable, and `shelf_run` is not derivable from anything the schema records.
The derivation is kept as the *last* rung instead, which is what makes the
column safe to leave NULL.

**Inheriting the view down the tree** (nearest non-NULL ancestor, as `esd_safe`
does). It reads as a convenience and is a trap: one edit at the room level would
silently restyle every drawer and bin below it, and the person who made that edit
would have no reason to connect the two.
