# ADR 0002 — Container types are recursive; Gridfinity is the reference case

**Status:** accepted
**Date:** 2026-07-28

## Context

The requirement is to author **templates for storage solutions** generally, with
Gridfinity as the case that covers most consumer storage — and specifically to
support **stacked Gridfinity bins** plus a drawer mounting system built around
them.

That last part is the load-bearing observation: the containment chain is deep,
and *the same pattern repeats at every level*.

```
room → cabinet → drawer → baseplate → bin → divider
                                       ↑
                          a stacked bin sits on the bin below it
```

Every one of those arrows is "a thing that presents a grid of units" containing
"a thing that occupies some of those units". `container_type_slot_templates`
already handles irregular compartments, and `locations` is already an adjacency
list with no depth limit — so the tree recursion exists. What is missing is that
a container type currently cannot say **what footprint it occupies in its
parent**, only what layout it offers its children.

Verified Gridfinity spec: **42 mm grid pitch, 41.5 mm bin footprint, 7 mm height
unit**; the magnet variant uses 6 × 2 mm discs inset from the corners.

## Decision

**A container type answers two questions independently:**

| Question | Expressed as |
|---|---|
| What grid do I present to my children? | `grid_rows`, `grid_cols` (already exist) + `grid_pitch_mm`, `grid_height_unit_mm` |
| What footprint do I occupy in my parent's grid? | `footprint_cols`, `footprint_rows`, `footprint_height_u` |

Decoupling them is the whole trick. A Gridfinity bin occupies 2 × 1 units of its
parent baseplate **and** presents its own 1 × 3 grid of internal dividers. Those
are unrelated facts, and a schema that conflated them into one "layout" field
could not express a bin that is both a child and a parent — which is exactly
what every level of a real Gridfinity setup is.

**Stacking is not a special case.** A bin's *top face* is a mounting surface, so
a stacked bin is simply a child of the bin below it in the ordinary
`locations` tree. No new relation, no stack table, no depth cap.

**A new capacity model, `grid_units`.** The existing `slots` model counts
compartments, which is wrong here: a 2 × 1 bin consumes two units, not one
slot. `grid_units` measures consumed **area** against `grid_rows × grid_cols`.

**Physical/generator data stays in `container_type_physical`** per PLAN.md, with
`generator_params_json` holding the OpenSCAD parameter set for
`kennetek/gridfinity-rebuilt-openscad`. That is what makes STL generation
reproducible rather than a hand-curated library of files.

## Consequences

**Entirely additive.** New nullable columns, one new `capacity_model` member,
one new capacity strategy class. This is the no-`CHECK`-enum rule paying for
itself exactly as intended: adding `grid_units` to `CapacityModel` is one line
in `app/models/enums.py`, not a SQLite table rebuild. Had `capacity_model` been
a `CHECK` constraint or an `sa.Enum`, this ADR would have required a migration
that rebuilt `container_types` and every table referencing it.

**Validation the layout editor must enforce:**

- a child's footprint must fit inside the parent's declared grid dimensions;
- consumed units must not exceed available units — though per the capacity
  invariant this is **advisory**: an over-capacity put-away is accepted and
  flagged `is_overfull`, never rejected;
- `grid_pitch_mm` must agree between a baseplate and the bins placed on it, or
  the bins physically will not seat. This is the one geometric constraint worth
  making a hard error, because unlike capacity it is not a preference.

**Non-Gridfinity storage still works unchanged.** An Akro-Mils or Raaco cabinet
leaves the pitch columns NULL and keeps using `container_type_slot_templates`
for its "44 small + 4 large" mix. Gridfinity is the *reference* case because it
is regular enough to generate, not a privileged one.

**Deferred:** the actual 3D-printed bins, and therefore any real-world check of
the 42 mm/7 mm assumptions. The honest cost noted in PLAN.md stands — populating
a lab is 100–300 bins at 30 min–2 h of print time each, so standardise on very
few footprint variants.

## Rejected alternative

**A separate "stack" relation, or a fixed cabinet → drawer → bin hierarchy with
named levels.** Both encode the current furniture into the schema. The drawer
mounting system does not exist yet and its geometry is unknown; a fixed
hierarchy would need a migration the first time something is one level deeper
than expected, which for a system meant to "survive adding shelves, boxes,
trays and drawers indefinitely" is the wrong failure mode.
