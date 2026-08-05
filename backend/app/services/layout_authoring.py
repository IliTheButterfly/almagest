"""Layout authoring — the canvas of slots a container type presents, and the
copy each physical instance makes of it.

Three ideas carry this module, straight from `docs/PLAN.md` and ADR 0002:

**Pure grids cost nothing to store.** `generate_grid` computes a uniform
type's layout from `grid_rows x grid_cols x slot_label_scheme` alone; nothing
is written to `container_type_slot_templates` until the first merge, label
override or per-cell size class, at which point `replace_type_slots`
materialises the whole canvas as explicit rows and flips
`materialize_slots=True` — a one-way flip. `effective_slots_for_type` is the
single door both states are read through, so nothing outside this module ever
needs to know which one a given type is in.

**Instances own their own copy.** `instantiate` copies a type's *current*
effective layout into concrete `locations` rows and never links back to the
type — editing the type afterwards touches none of what was already built.
That is what keeps the change guard below simple: it only ever has to reason
about one instance's own children, never about a type that might still be
generating cells on the fly for some other instance.

**The change guard.** `apply_layout_to_location` classifies every difference
between an instance's current children and a proposed new layout into safe
(relabel, size-class/volume edit, a scheme change that doesn't move a cell),
guarded (a slot would be deleted, and it still holds stock or a bound tag —
raises `GuardedLayoutChange` with the full list), or refused outright (an
existing slot's *label* reappears at a *different* region, which would be
silently reinterpreting what a tag or a lot thinks that label still means).
A shrink or a merge is always a delete-then-recreate of confirmed-empty slots;
a surviving slot never has its region reassigned in place.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.enums import EntityType, SizeClass, SlotLabelScheme, TagGranularity
from app.models.stock import StockLot
from app.models.storage import ContainerType, ContainerTypeSlotTemplate, Location, LocationTag
from app.services import shortid
from app.services.capacity import grid_incompatibility
from app.services.tree import location_tree

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LayoutError(ValueError):
    """A layout edit that is structurally invalid, or refused outright.

    Distinct from `GuardedLayoutChange`: this is "the request cannot mean what
    it says" (an out-of-bounds cell, two slots overlapping, a slot's identity
    being reinterpreted), not "the request is fine but something in the way
    needs to move first".
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AffectedSlot:
    """One slot a layout edit would need to delete, but cannot yet.

    Carried in full on `GuardedLayoutChange` so a 409 response can list every
    blocked slot in one round trip — "move contents to a holding location
    first" needs the whole list, not one slot discovered at a time.
    """

    location_id: int
    slot_label: str
    reasons: tuple[str, ...]


class GuardedLayoutChange(Exception):
    """The edit is otherwise valid, but would delete a slot that still holds
    stock or a bound tag."""

    def __init__(self, affected: Sequence[AffectedSlot]) -> None:
        super().__init__(f"{len(affected)} slot(s) hold content and block this layout change")
        self.affected = list(affected)


# ---------------------------------------------------------------------------
# The canvas, as data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotSpec:
    """One compartment: a base cell, or a merged rectangular region.

    Deliberately decoupled from both `ContainerTypeSlotTemplate` and
    `Location` — it is what a *generated* grid produces too, with no database
    row behind it at all, which is the whole point of a pure grid.
    """

    row_idx: int
    col_idx: int
    slot_label: str
    row_span: int = 1
    col_span: int = 1
    size_class: SizeClass | None = None
    inner_volume_mm3: float | None = None


def _spec_key(spec: SlotSpec) -> tuple[int, int, int, int, str, SizeClass | None, float | None]:
    return (
        spec.row_idx,
        spec.col_idx,
        spec.row_span,
        spec.col_span,
        spec.slot_label,
        spec.size_class,
        spec.inner_volume_mm3,
    )


def _rect_overlaps(
    spec: SlotSpec, row_idx: int, col_idx: int, row_span: int, col_span: int
) -> bool:
    return not (
        spec.col_idx + spec.col_span <= col_idx
        or spec.col_idx >= col_idx + col_span
        or spec.row_idx + spec.row_span <= row_idx
        or spec.row_idx >= row_idx + row_span
    )


def _fully_inside(spec: SlotSpec, row_idx: int, col_idx: int, row_span: int, col_span: int) -> bool:
    return (
        spec.row_idx >= row_idx
        and spec.row_idx + spec.row_span <= row_idx + row_span
        and spec.col_idx >= col_idx
        and spec.col_idx + spec.col_span <= col_idx + col_span
    )


def validate_no_overlaps(
    specs: Sequence[SlotSpec], *, grid_rows: int | None, grid_cols: int | None
) -> None:
    """Every desired slot is an in-bounds rectangle, and no two overlap.

    This **is** "only contiguous rectangles may merge": a merge is legal
    exactly when the regions it replaces tile its target with no gap and no
    slot cut in half, and that claim reduces to "the new set of regions still
    doesn't overlap itself" once the absorbed regions are gone from it. One
    check serves the type-level canvas and the instance-level layout alike.
    """
    for spec in specs:
        if spec.row_span < 1 or spec.col_span < 1:
            raise LayoutError(
                f"slot {spec.slot_label!r} has a non-positive span", reason="invalid_span"
            )
        if spec.row_idx < 0 or spec.col_idx < 0:
            raise LayoutError(
                f"slot {spec.slot_label!r} has a negative position", reason="out_of_bounds"
            )
        if grid_rows is not None and spec.row_idx + spec.row_span > grid_rows:
            raise LayoutError(
                f"slot {spec.slot_label!r} extends past grid_rows={grid_rows}",
                reason="out_of_bounds",
            )
        if grid_cols is not None and spec.col_idx + spec.col_span > grid_cols:
            raise LayoutError(
                f"slot {spec.slot_label!r} extends past grid_cols={grid_cols}",
                reason="out_of_bounds",
            )

    for a, b in itertools.combinations(specs, 2):
        if _rect_overlaps(a, b.row_idx, b.col_idx, b.row_span, b.col_span):
            raise LayoutError(
                f"slots {a.slot_label!r} and {b.slot_label!r} overlap; only contiguous, "
                "non-overlapping rectangles are legal",
                reason="overlap",
            )

    labels = [spec.slot_label for spec in specs]
    if len(labels) != len(set(labels)):
        raise LayoutError("two slots share a slot_label", reason="duplicate_label")


def compute_sort_order(specs: Sequence[SlotSpec]) -> list[tuple[SlotSpec, int]]:
    """`(row_idx, col_idx)` ascending, in steps of 10.

    A merged region's `row_idx`/`col_idx` already *are* its top-left corner by
    construction, so sorting by them sorts a merge by "where a reader's eye
    reaches it" with no special case at all.
    """
    ordered = sorted(specs, key=lambda spec: (spec.row_idx, spec.col_idx))
    return [(spec, index * 10) for index, spec in enumerate(ordered)]


# ---------------------------------------------------------------------------
# The generator — what a pure grid computes instead of storing
# ---------------------------------------------------------------------------


def parse_params(container_type: ContainerType) -> dict[str, Any]:
    if not container_type.slot_label_params_json:
        return {}
    parsed = json.loads(container_type.slot_label_params_json)
    return parsed if isinstance(parsed, dict) else {}


def _row_letters(row_idx: int) -> str:
    """Spreadsheet-style base-26 letters, 0-indexed: 0->'A', 25->'Z', 26->'AA'."""
    letters = ""
    n = row_idx
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return letters


def _custom_label_map(
    params: dict[str, Any], grid_rows: int, grid_cols: int
) -> dict[tuple[int, int], str]:
    labels = params.get("labels")
    total = grid_rows * grid_cols
    if not isinstance(labels, list) or len(labels) != total:
        raise LayoutError(
            f"the 'custom' scheme needs exactly {total} labels in slot_label_params_json['labels']",
            reason="invalid_custom_labels",
        )
    return {
        (index // grid_cols, index % grid_cols): str(label) for index, label in enumerate(labels)
    }


def generate_label(
    scheme: SlotLabelScheme,
    params: dict[str, Any],
    row_idx: int,
    col_idx: int,
    *,
    grid_rows: int | None,
    grid_cols: int | None,
) -> str:
    """The label a pure grid would give one cell — also used to relabel a
    just-split base cell back to exactly what a fresh grid would show there."""
    if scheme == SlotLabelScheme.ROW_ALPHA_COL_NUM:
        return f"{_row_letters(row_idx)}{col_idx + 1}"

    if scheme == SlotLabelScheme.SEQUENTIAL:
        cols = grid_cols or 1
        index = row_idx * cols + col_idx + 1
        zero_pad = params.get("zero_pad")
        if zero_pad:
            total = (grid_rows or 1) * (grid_cols or 1)
            width = (
                zero_pad
                if isinstance(zero_pad, int) and not isinstance(zero_pad, bool)
                else len(str(total))
            )
            return str(index).zfill(width)
        return str(index)

    if scheme == SlotLabelScheme.CUSTOM:
        mapping = _custom_label_map(params, grid_rows or 0, grid_cols or 0)
        try:
            return mapping[(row_idx, col_idx)]
        except KeyError:
            raise LayoutError(
                f"cell ({row_idx}, {col_idx}) is outside the custom label grid",
                reason="out_of_bounds",
            ) from None

    raise LayoutError(
        f"unknown slot_label_scheme {scheme!r}", reason="unknown_scheme"
    )  # pragma: no cover


def generate_grid(container_type: ContainerType) -> list[SlotSpec]:
    """The base 1x1 layout a pure (un-materialised) grid computes on the fly:
    one cell per `(row, col)` in `grid_rows x grid_cols`, in reading order.

    This function *is* "pure grids cost nothing to store" — it is the whole
    reason `materialize_slots=False` needs zero rows to mean something.
    """
    rows = container_type.grid_rows or 0
    cols = container_type.grid_cols or 0
    scheme = SlotLabelScheme(container_type.slot_label_scheme)
    params = parse_params(container_type)

    if scheme == SlotLabelScheme.CUSTOM:
        labels = _custom_label_map(params, rows, cols)
        return [
            SlotSpec(row_idx=r, col_idx=c, slot_label=labels[(r, c)])
            for r in range(rows)
            for c in range(cols)
        ]

    return [
        SlotSpec(
            row_idx=r,
            col_idx=c,
            slot_label=generate_label(scheme, params, r, c, grid_rows=rows, grid_cols=cols),
        )
        for r in range(rows)
        for c in range(cols)
    ]


# ---------------------------------------------------------------------------
# Type-level template: read/materialise/merge/split
# ---------------------------------------------------------------------------


def _spec_from_template(row: ContainerTypeSlotTemplate) -> SlotSpec:
    return SlotSpec(
        row_idx=row.row_idx,
        col_idx=row.col_idx,
        slot_label=row.slot_label,
        row_span=row.row_span,
        col_span=row.col_span,
        size_class=SizeClass(row.size_class) if row.size_class else None,
        inner_volume_mm3=row.inner_volume_mm3,
    )


def effective_slots_for_type(session: Session, container_type: ContainerType) -> list[SlotSpec]:
    """The type's current logical layout, whichever half of the design
    produced it — transparent to every caller, which is the point."""
    if container_type.materialize_slots:
        rows = session.execute(
            select(ContainerTypeSlotTemplate)
            .where(ContainerTypeSlotTemplate.container_type_id == container_type.id)
            .order_by(ContainerTypeSlotTemplate.sort_order)
        ).scalars()
        return [_spec_from_template(row) for row in rows]
    return generate_grid(container_type)


def _persist_type_slots(
    session: Session, container_type: ContainerType, desired: Sequence[SlotSpec]
) -> None:
    """Wipe and rewrite `container_type_slot_templates` for `container_type`.

    Always safe to do unconditionally: unlike an instance's `locations` rows,
    a type's template rows carry no incoming foreign key — instantiation
    *copies* them into an instance rather than pointing at them — so there is
    nothing here for the change guard to protect.
    """
    session.execute(
        delete(ContainerTypeSlotTemplate).where(
            ContainerTypeSlotTemplate.container_type_id == container_type.id
        )
    )
    for spec, order in compute_sort_order(desired):
        session.add(
            ContainerTypeSlotTemplate(
                container_type_id=container_type.id,
                slot_label=spec.slot_label,
                row_idx=spec.row_idx,
                col_idx=spec.col_idx,
                row_span=spec.row_span,
                col_span=spec.col_span,
                size_class=spec.size_class,
                inner_volume_mm3=spec.inner_volume_mm3,
                sort_order=order,
            )
        )
    container_type.materialize_slots = True
    session.flush()


def materialize_type(session: Session, container_type: ContainerType) -> None:
    """Freeze the type's current generated layout into explicit rows.

    A no-op if already materialised. The flip is one-way: even splitting
    every merge back to exactly the generated grid never un-sets it — the
    generator is never consulted again for this type once this has run.
    """
    if container_type.materialize_slots:
        return
    _persist_type_slots(session, container_type, generate_grid(container_type))


def _same_as_generated(container_type: ContainerType, desired: Sequence[SlotSpec]) -> bool:
    generated = generate_grid(container_type)
    return {_spec_key(s) for s in generated} == {_spec_key(s) for s in desired}


def replace_type_slots(
    session: Session, container_type: ContainerType, desired: Sequence[SlotSpec]
) -> None:
    """Set the type's layout to exactly `desired`.

    If the type is still pure and `desired` is indistinguishable from what
    `generate_grid` already computes, this is a no-op — writing the identical
    grid back out would materialise a type that never actually changed.
    Anything else — a merge, a relabel, a size class, a scheme/grid-size edit
    that moves cells — materialises to exactly what was asked for.
    """
    validate_no_overlaps(
        desired, grid_rows=container_type.grid_rows, grid_cols=container_type.grid_cols
    )
    if not container_type.materialize_slots and _same_as_generated(container_type, desired):
        return
    _persist_type_slots(session, container_type, desired)


def merge_type_region(
    session: Session,
    container_type: ContainerType,
    *,
    row_idx: int,
    col_idx: int,
    row_span: int,
    col_span: int,
    slot_label: str | None = None,
    size_class: SizeClass | None = None,
    inner_volume_mm3: float | None = None,
) -> SlotSpec:
    """Merge the existing slots inside one rectangle into a single slot.

    Every existing slot the rectangle touches must be **fully** inside it —
    a partial overlap would mean cutting an existing region in half, which is
    refused as `not_contiguous` — and together they must tile it exactly, with
    no gap, or the merge is refused as `gap_in_region`. Materialises on first
    call, per the design.
    """
    current = effective_slots_for_type(session, container_type)
    absorbed: list[SlotSpec] = []
    for spec in current:
        if not _rect_overlaps(spec, row_idx, col_idx, row_span, col_span):
            continue
        if not _fully_inside(spec, row_idx, col_idx, row_span, col_span):
            raise LayoutError(
                f"the merge target crosses slot {spec.slot_label!r}; only contiguous "
                "rectangles may merge",
                reason="not_contiguous",
            )
        absorbed.append(spec)

    if sum(spec.row_span * spec.col_span for spec in absorbed) != row_span * col_span:
        raise LayoutError(
            "the merge target is not exactly covered by existing slots (a gap, or "
            "cells outside the grid)",
            reason="gap_in_region",
        )

    scheme = SlotLabelScheme(container_type.slot_label_scheme)
    params = parse_params(container_type)
    label = slot_label or generate_label(
        scheme,
        params,
        row_idx,
        col_idx,
        grid_rows=container_type.grid_rows,
        grid_cols=container_type.grid_cols,
    )
    merged = SlotSpec(
        row_idx=row_idx,
        col_idx=col_idx,
        row_span=row_span,
        col_span=col_span,
        slot_label=label,
        size_class=size_class,
        inner_volume_mm3=inner_volume_mm3,
    )
    remaining = [spec for spec in current if spec not in absorbed]
    # Validated like any other desired list. An explicit `slot_label` for the
    # merged region can collide with an untouched slot elsewhere on the canvas,
    # and skipping the check here — which `replace_type_slots` does perform —
    # would let a duplicate label through to the UNIQUE(container_type_id,
    # slot_label) constraint as an IntegrityError instead of a clean refusal.
    validate_no_overlaps(
        [*remaining, merged],
        grid_rows=container_type.grid_rows,
        grid_cols=container_type.grid_cols,
    )
    _persist_type_slots(session, container_type, [*remaining, merged])
    return merged


def split_type_region(
    session: Session, container_type: ContainerType, slot_label: str
) -> list[SlotSpec]:
    """Decompose a merged region back to its base cells, labelled exactly as
    a fresh pure grid would label them there — whether or not this type has
    since been materialised for an unrelated reason."""
    current = effective_slots_for_type(session, container_type)
    target = next((spec for spec in current if spec.slot_label == slot_label), None)
    if target is None:
        raise LayoutError(f"no slot labelled {slot_label!r}", reason="unknown_slot")
    if target.row_span == 1 and target.col_span == 1:
        raise LayoutError(f"slot {slot_label!r} is not merged", reason="not_merged")

    scheme = SlotLabelScheme(container_type.slot_label_scheme)
    params = parse_params(container_type)
    base_cells = [
        SlotSpec(
            row_idx=r,
            col_idx=c,
            slot_label=generate_label(
                scheme,
                params,
                r,
                c,
                grid_rows=container_type.grid_rows,
                grid_cols=container_type.grid_cols,
            ),
        )
        for r in range(target.row_idx, target.row_idx + target.row_span)
        for c in range(target.col_idx, target.col_idx + target.col_span)
    ]
    remaining = [spec for spec in current if spec != target]
    _persist_type_slots(session, container_type, [*remaining, *base_cells])
    return base_cells


# ---------------------------------------------------------------------------
# Seed types are read-only: editing one clones it
# ---------------------------------------------------------------------------

#: Copied verbatim by `clone_type`. `id`, `slug`, `is_seed` and the timestamps
#: are excluded on purpose — a clone always gets its own slug and is never
#: itself a seed, however it was reached.
_CLONABLE_FIELDS = (
    "description",
    "child_layout",
    # A clone that lost its pinned drawing would silently redraw every cabinet
    # stamped from it, and the clone path is how a seed type is edited at all.
    "child_view",
    # Same argument as `child_view` just above: a clone that lost its pinned
    # pictogram would silently go back to a placeholder for every cabinet
    # stamped from it. The clone's *photo* — a `document_links` row, not a
    # column — is deliberately **not** carried across; see
    # `app.api.routes.container_types.clone_container_type`.
    "glyph",
    "grid_rows",
    "grid_cols",
    "grid_pitch_mm",
    "grid_height_unit_mm",
    "footprint_cols",
    "footprint_rows",
    "footprint_height_u",
    "slot_label_scheme",
    "slot_label_params_json",
    "materialize_slots",
    "capacity_model",
    "capacity_slots",
    "max_parts_per_slot",
    "inner_length_mm",
    "inner_width_mm",
    "inner_height_mm",
    "default_fill_factor",
    "full_threshold",
    "esd_safe",
    "is_placeable",
    "max_item_dimension_mm",
    "allowed_part_kinds_json",
    "front_width_mm",
    "front_height_mm",
)


def clone_type(
    session: Session, source: ContainerType, *, slug: str, display_name: str | None = None
) -> ContainerType:
    """Copy `source` into a fresh, editable row — including its materialised
    slot template, if it has one. A pure grid's clone stays pure: there is
    nothing to copy but the scalar fields that already describe it."""
    clone = ContainerType(
        slug=slug,
        display_name=display_name or f"{source.display_name} (copy)",
        **{field: getattr(source, field) for field in _CLONABLE_FIELDS},
    )
    session.add(clone)
    session.flush()

    if source.materialize_slots:
        rows = session.execute(
            select(ContainerTypeSlotTemplate)
            .where(ContainerTypeSlotTemplate.container_type_id == source.id)
            .order_by(ContainerTypeSlotTemplate.sort_order)
        ).scalars()
        for row in rows:
            session.add(
                ContainerTypeSlotTemplate(
                    container_type_id=clone.id,
                    slot_label=row.slot_label,
                    row_idx=row.row_idx,
                    col_idx=row.col_idx,
                    row_span=row.row_span,
                    col_span=row.col_span,
                    size_class=row.size_class,
                    inner_volume_mm3=row.inner_volume_mm3,
                    sort_order=row.sort_order,
                )
            )
        session.flush()
    return clone


def default_clone_slug(session: Session, source_slug: str) -> str:
    """`{slug}-copy`, then `-copy-2`, `-copy-3`, ... on collision."""
    candidate = f"{source_slug}-copy"
    suffix = 1
    while (
        session.execute(
            select(ContainerType.id).where(ContainerType.slug == candidate)
        ).scalar_one_or_none()
        is not None
    ):
        suffix += 1
        candidate = f"{source_slug}-copy-{suffix}"
    return candidate


def ensure_editable(session: Session, container_type: ContainerType) -> tuple[ContainerType, bool]:
    """A seed type is read-only; editing one clones it instead of mutating the
    row every fresh install starts with.

    Returns `(type_to_edit, was_cloned)` so a route can tell its caller when
    the id in the response is not the id that was asked for.
    """
    if not container_type.is_seed:
        return container_type, False
    clone = clone_type(
        session, container_type, slug=default_clone_slug(session, container_type.slug)
    )
    return clone, True


# ---------------------------------------------------------------------------
# Instances: their own copy, and the change guard on editing it
# ---------------------------------------------------------------------------


def _reassign_location_sort_order(rows: Sequence[Location]) -> None:
    """The same `(row_idx, col_idx)`-ascending, steps-of-10 rule as
    `compute_sort_order`, restated for `Location` rows because they carry the
    position directly rather than through a round trip via `SlotSpec`."""
    positioned = [loc for loc in rows if loc.row_idx is not None]
    ordered = sorted(positioned, key=lambda loc: (loc.row_idx or 0, loc.col_idx or 0))
    for index, loc in enumerate(ordered):
        loc.sort_order = index * 10


def instantiate(
    session: Session,
    parent: Location | None,
    container_type: ContainerType,
    *,
    count: int,
    naming_pattern: str,
    tag_granularity: TagGranularity,
) -> list[Location]:
    """Create `count` new instances of `container_type` under `parent`, each
    materialising the type's *current* effective layout into its own child
    `locations` rows.

    `parent is None` is the top of the tree, and it is a legitimate destination
    rather than a missing argument: in an empty install the *first* container has
    nowhere to hang off by definition, and it is as likely to be a cabinet or a
    drawn room — something with a layout worth materialising — as a bare box.
    Only the grid check is parent-relative, so there is nothing else to skip.

    Nothing here links back to `container_type` afterwards — editing the type
    later touches none of what this call just built.
    """
    if parent is not None:
        parent_type = (
            session.get(ContainerType, parent.container_type_id)
            if parent.container_type_id
            else None
        )
        incompatibility = grid_incompatibility(parent_type, container_type)
        if incompatibility is not None:
            raise LayoutError(
                f"{container_type.slug!r} cannot sit in {parent.name!r}'s grid: {incompatibility}",
                reason=incompatibility,
            )

    slots = effective_slots_for_type(session, container_type)
    ordered_slots = compute_sort_order(slots)

    created: list[Location] = []
    for n in range(1, count + 1):
        if "{n}" in naming_pattern:
            try:
                name = naming_pattern.format(n=n)
            except (KeyError, IndexError, ValueError) as error:
                # `naming_pattern` is client text handed to str.format, so any
                # placeholder other than {n} — or an unbalanced brace — raises.
                # "Cabinet {n} {oops}" was producing an uncaught KeyError and a
                # bare 500. Only {n} is substituted, so anything else is a
                # malformed pattern and the user needs telling which.
                raise LayoutError(
                    f"naming_pattern {naming_pattern!r} is not a valid template; "
                    "only {n} may be substituted",
                    reason="bad_naming_pattern",
                ) from error
        elif count > 1:
            name = f"{naming_pattern} {n}"
        else:
            name = naming_pattern

        instance = Location(
            name=name,
            parent_id=parent.id if parent is not None else None,
            container_type_id=container_type.id,
        )
        session.add(instance)
        session.flush()
        # The cabinet itself is always tagged — PLAN.md's baseline is "tag the
        # cabinet, pick the drawer on screen"; `SLOT` only adds per-drawer tags
        # on top of that, it never replaces the container's own.
        shortid.allocate(session, EntityType.LOCATION, instance.id)

        for spec, order in ordered_slots:
            child = Location(
                name=spec.slot_label,
                parent_id=instance.id,
                slot_label=spec.slot_label,
                row_idx=spec.row_idx,
                col_idx=spec.col_idx,
                row_span=spec.row_span,
                col_span=spec.col_span,
                size_class=spec.size_class,
                inner_volume_mm3=spec.inner_volume_mm3,
                sort_order=order,
            )
            session.add(child)
            session.flush()
            if tag_granularity == TagGranularity.SLOT:
                shortid.allocate(session, EntityType.LOCATION, child.id)

        created.append(instance)

    session.flush()
    location_tree(session).rebuild_paths()
    return created


def _blocking_reasons(session: Session, location_id: int) -> tuple[str, ...]:
    """Why `location_id` cannot simply be deleted.

    Any lot at all blocks — not only active ones: `stock_lots.location_id` is
    `ON DELETE RESTRICT`, so even a fully consumed historical lot would fail
    the delete at the database layer. Checking here turns that into a 409 with
    a reason instead of a bare `IntegrityError`. A child location (something
    already placed *in* this slot) blocks for the identical reason.
    """
    reasons = []
    if session.execute(
        select(func.count()).select_from(StockLot).where(StockLot.location_id == location_id)
    ).scalar_one():
        reasons.append("has_stock")
    if session.execute(
        select(func.count()).select_from(LocationTag).where(LocationTag.location_id == location_id)
    ).scalar_one():
        reasons.append("has_tag")
    if session.execute(
        select(func.count()).select_from(Location).where(Location.parent_id == location_id)
    ).scalar_one():
        reasons.append("has_children")
    return tuple(reasons)


@dataclass(frozen=True)
class LayoutDiff:
    creates: tuple[SlotSpec, ...]
    #: `(current row, its new content)` — same region, different label and/or
    #: size class and/or volume. Always safe regardless of what the slot holds.
    safe_updates: tuple[tuple[Location, SlotSpec], ...]
    #: Rows whose region no longer appears in the desired layout at all.
    deletes: tuple[Location, ...]
    #: `(current row, the desired slot that reuses its label elsewhere)`.
    #: Never applied — `apply_layout_to_location` raises on any of these.
    reinterpreted: tuple[tuple[Location, SlotSpec], ...]


def diff_instance_layout(current: Sequence[Location], desired: Sequence[SlotSpec]) -> LayoutDiff:
    """Classify every desired slot against an instance's current children.

    A slot's **identity is its region** (`row_idx`, `col_idx` and both spans)
    when the label attached to it is otherwise unclaimed — relabelling in
    place, with a fresh name nobody else currently has, is exactly the safe
    case. But whenever a desired label is currently held by a *different* row
    than the one about to occupy that region, the label check wins: that
    would silently walk an existing identity — whatever a printed card, a
    bound tag or a lot thinks that name still means — to a new position, which
    is refused outright regardless of whether the region side of the request
    also happens to look like an ordinary safe rename of some other slot.
    """
    by_region: dict[tuple[int, int, int, int], Location] = {}
    by_label: dict[str, Location] = {}
    for loc in current:
        if loc.row_idx is None or loc.col_idx is None:
            continue
        by_region[(loc.row_idx, loc.col_idx, loc.row_span, loc.col_span)] = loc
        if loc.slot_label:
            by_label[loc.slot_label] = loc

    creates: list[SlotSpec] = []
    safe_updates: list[tuple[Location, SlotSpec]] = []
    reinterpreted: list[tuple[Location, SlotSpec]] = []
    survived: set[int] = set()
    reinterpreted_ids: set[int] = set()

    for spec in desired:
        region_owner = by_region.get((spec.row_idx, spec.col_idx, spec.row_span, spec.col_span))
        label_owner = by_label.get(spec.slot_label)

        if label_owner is not None and label_owner is not region_owner:
            reinterpreted.append((label_owner, spec))
            reinterpreted_ids.add(label_owner.id)
            continue

        if region_owner is not None:
            survived.add(region_owner.id)
            if (
                region_owner.slot_label,
                region_owner.size_class,
                region_owner.inner_volume_mm3,
            ) != (
                spec.slot_label,
                spec.size_class,
                spec.inner_volume_mm3,
            ):
                safe_updates.append((region_owner, spec))
            continue

        creates.append(spec)

    deletes = [
        loc
        for loc in current
        if loc.row_idx is not None and loc.id not in survived and loc.id not in reinterpreted_ids
    ]

    return LayoutDiff(
        creates=tuple(creates),
        safe_updates=tuple(safe_updates),
        deletes=tuple(deletes),
        reinterpreted=tuple(reinterpreted),
    )


def apply_layout_to_location(
    session: Session, location: Location, desired: Sequence[SlotSpec]
) -> LayoutDiff:
    """Reapply a (possibly edited) layout onto `location`'s own children.

    **Classify, then check every deletion for blocking content, then commit**
    — nothing is deleted until every deletion in the batch is confirmed empty,
    so a request touching ten slots that is blocked on the tenth leaves all
    ten untouched rather than nine gone and one refused.
    """
    current = list(
        session.execute(select(Location).where(Location.parent_id == location.id)).scalars()
    )

    grid_rows = max((spec.row_idx + spec.row_span for spec in desired), default=0)
    grid_cols = max((spec.col_idx + spec.col_span for spec in desired), default=0)
    validate_no_overlaps(desired, grid_rows=grid_rows or None, grid_cols=grid_cols or None)

    diff = diff_instance_layout(current, desired)
    if diff.reinterpreted:
        offender, _ = diff.reinterpreted[0]
        raise LayoutError(
            f"slot {offender.slot_label!r} would be reinterpreted at a different grid "
            "position; delete it and create a new slot instead of reusing its label",
            reason="slot_identity_reinterpreted",
        )

    affected = [
        AffectedSlot(location_id=loc.id, slot_label=loc.slot_label or "", reasons=reasons)
        for loc in diff.deletes
        if (reasons := _blocking_reasons(session, loc.id))
    ]
    if affected:
        raise GuardedLayoutChange(affected)

    for loc, spec in diff.safe_updates:
        loc.slot_label = spec.slot_label
        loc.name = spec.slot_label
        loc.size_class = spec.size_class
        loc.inner_volume_mm3 = spec.inner_volume_mm3

    for loc in diff.deletes:
        # The slot's short id, printed card, photograph and taught aliases go
        # with it, released by `models.events`'s `before_delete` listener. This
        # path used to have to remember, and did not: a card printed for the
        # bottom-left cell resolved to the bottom-right one after a merge and a
        # re-split, because SQLite reuses the freed rowid.
        session.delete(loc)
    session.flush()

    for spec in diff.creates:
        session.add(
            Location(
                name=spec.slot_label,
                parent_id=location.id,
                slot_label=spec.slot_label,
                row_idx=spec.row_idx,
                col_idx=spec.col_idx,
                row_span=spec.row_span,
                col_span=spec.col_span,
                size_class=spec.size_class,
                inner_volume_mm3=spec.inner_volume_mm3,
            )
        )
    session.flush()

    survivors = list(
        session.execute(select(Location).where(Location.parent_id == location.id)).scalars()
    )
    _reassign_location_sort_order(survivors)
    session.flush()
    # Unconditional: a relabel changes `label_path` too, and rebuilding is a
    # single idempotent statement regardless of what actually moved.
    location_tree(session).rebuild_paths()

    return diff
