"""The physical storage tree and the container types that shape it."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin, TreeMixin
from app.models.enums import (
    CapacityModel,
    ChildLayout,
    ChildView,
    ContainerGlyph,
    NdefState,
    PlanShapeKind,
    SizeClass,
    SlotLabelScheme,
)
from app.models.types import StrEnumType, UtcDateTime


class ContainerType(Base, TimestampMixin):
    """Shelves, boxes, trays, drawers, bags, reel racks and rooms are all this
    one entity. A new kind of container is a row, not a migration."""

    __tablename__ = "container_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    child_layout: Mapped[str] = mapped_column(
        StrEnumType(ChildLayout), nullable=False, default=ChildLayout.NONE
    )
    grid_rows: Mapped[int | None] = mapped_column(Integer)
    grid_cols: Mapped[int | None] = mapped_column(Integer)

    # --- ADR 0006: and a *third* independent question --------------------------
    #
    # "How should my children be drawn?" A Raaco cabinet and a Gridfinity
    # baseplate both answer `child_layout=grid`, and want entirely different
    # pictures — drawer fronts in a vertical face versus square cells seen from
    # above. A workshop presents no grid at all and still has to render as
    # something better than a bullet list.
    #
    #: NULL means **derive it** from the geometry this type already declares
    #: (`app.services.views.derive_child_view`), which is why no seed row needed
    #: a value backfilled: a baseplate's declared pitch already says "cells seen
    #: from above", and a stored copy of that would be a second version of the
    #: same fact, free to drift from the geometry it was read off.
    #:
    #: Set it to pin the drawing for every instance of the type — "every Raaco
    #: cabinet draws the same way" is a fact about the type, not about one
    #: cabinet. `locations.child_view` then overrides it per instance, the same
    #: type-default-with-instance-override shape `esd_safe`, `is_placeable` and
    #: `default_fill_factor`/`fill_factor` already use.
    child_view: Mapped[str | None] = mapped_column(StrEnumType(ChildView))

    #: The pictogram every instance of this type is drawn with, in the dense
    #: tree view where a real photograph would be too expensive to load ninety-
    #: six of. `locations.glyph` overrides it per instance, the same two-rung
    #: shape as `esd_safe`/`is_placeable`/`fill_factor` — but unlike those and
    #: unlike `child_view`, there is no third rung: nothing about a type's
    #: geometry implies what it *looks like*, so NULL here simply means no
    #: glyph is chosen and the renderer falls back to a neutral placeholder
    #: rather than a guess. See `app.models.enums.ContainerGlyph` for why this
    #: is a separate concern from the photo a phone takes of the real drawer
    #: (`DocumentRole.PHOTO`, attached through `document_links` — no column
    #: for that here, on purpose: Phase 4's document store already models it).
    glyph: Mapped[str | None] = mapped_column(StrEnumType(ContainerGlyph))

    # --- ADR 0002: a container type answers two *independent* questions ------
    #
    # "What grid do I present to my children?" and "what footprint do I occupy
    # in my parent's grid?" Keeping them separate is what lets a type be both a
    # child and a parent, which every level of a stacked Gridfinity setup is: a
    # 2x1 bin occupies two units of its baseplate *and* presents its own 1x3
    # grid of dividers. A single conflated "layout" field cannot say that.
    #
    # Recursion then needs no new machinery — `locations.parent_id` already has
    # no depth limit, and a bin's top face is just another mounting surface, so
    # a stacked bin is an ordinary child.

    #: Physical pitch of the grid this type presents, in mm. Gridfinity is 42.0.
    #: NULL for a container whose compartments are irregular — an Akro-Mils or
    #: Raaco cabinet leaves this unset and keeps using slot templates for its
    #: "44 small + 4 large" mix. Gridfinity is the *reference* case because it is
    #: regular enough to generate, not a privileged one.
    grid_pitch_mm: Mapped[float | None] = mapped_column(Float)
    #: Height of one vertical unit, in mm. Gridfinity is 7.0.
    grid_height_unit_mm: Mapped[float | None] = mapped_column(Float)

    #: Footprint in the *parent's* grid units. A Gridfinity 2x1x6u bin is
    #: (2, 1, 6). NULL means this type does not sit in a measured grid.
    footprint_cols: Mapped[int | None] = mapped_column(Integer)
    footprint_rows: Mapped[int | None] = mapped_column(Integer)
    footprint_height_u: Mapped[int | None] = mapped_column(Integer)

    slot_label_scheme: Mapped[str] = mapped_column(
        StrEnumType(SlotLabelScheme), nullable=False, default=SlotLabelScheme.ROW_ALPHA_COL_NUM
    )
    slot_label_params_json: Mapped[str | None] = mapped_column(Text)

    #: False means the layout is *computed* from grid_rows x grid_cols and this
    #: type stores zero slot-template rows. The first merge, label override or
    #: per-cell size class materialises the whole type into explicit rows and
    #: flips this to True, after which the template table is authoritative and
    #: the generator is never consulted for this type again.
    materialize_slots: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    capacity_model: Mapped[str] = mapped_column(
        StrEnumType(CapacityModel), nullable=False, default=CapacityModel.NONE
    )
    capacity_slots: Mapped[int | None] = mapped_column(Integer)
    max_parts_per_slot: Mapped[int | None] = mapped_column(Integer)
    inner_length_mm: Mapped[float | None] = mapped_column(Float)
    inner_width_mm: Mapped[float | None] = mapped_column(Float)
    inner_height_mm: Mapped[float | None] = mapped_column(Float)

    #: Screws pack tighter than TO-220s, so this belongs to the container, not
    #: the part. Calibratable later from observed "user says full at X" data.
    default_fill_factor: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.55, server_default="0.55"
    )
    full_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.9, server_default="0.9"
    )

    #: NULL means "inherit from the ancestor chain". Marking a whole cabinet
    #: ESD-safe is then one edit rather than one per drawer.
    esd_safe: Mapped[bool | None] = mapped_column(Boolean)
    #: A shelf holds boxes, not loose parts. False keeps stock out of it.
    is_placeable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    max_item_dimension_mm: Mapped[float | None] = mapped_column(Float)
    #: JSON array of part_kind slugs. Not filterable in SQL and rarely set, so
    #: a join table would be machinery with no payoff; assignment reads it in
    #: Python while scoring candidates it has already loaded.
    allowed_part_kinds_json: Mapped[str | None] = mapped_column(Text)

    #: Front face of a drawer, used to size the printed label card.
    front_width_mm: Mapped[float | None] = mapped_column(Float)
    front_height_mm: Mapped[float | None] = mapped_column(Float)

    #: Ships in a data migration as a library of real cabinets. Seed types are
    #: read-only; editing one implicitly clones it.
    is_seed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )


class ContainerTypeSlotTemplate(Base):
    """One compartment of a materialised container type.

    Real assortment boxes are "4 large + 12 small", not rows x cols — no
    manufacturer publishes a clean grid. So the canonical representation is a
    canvas of base cells with merged regions, and a pure grid is just the case
    where this table is empty and the layout is generated instead.
    """

    __tablename__ = "container_type_slot_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    container_type_id: Mapped[int] = mapped_column(
        ForeignKey("container_types.id", ondelete="CASCADE"), nullable=False, index=True
    )

    slot_label: Mapped[str] = mapped_column(String(64), nullable=False)
    row_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    col_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    row_span: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    col_span: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    size_class: Mapped[str | None] = mapped_column(StrEnumType(SizeClass))
    inner_volume_mm3: Mapped[float | None] = mapped_column(Float)

    #: Recomputed on every template save as (row_idx, col_idx) ascending, in
    #: steps of 10. It drives both the provisioning cursor and the label sheet,
    #: so it must follow physical reading order. A merged region sorts by its
    #: top-left corner — exactly where a reader's eye reaches it — so merging
    #: never breaks that order.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("container_type_id", "slot_label"),
        Index("ix_slot_template_type_sort", "container_type_id", "sort_order"),
    )


class Location(Base, TreeMixin, TimestampMixin):
    """A physical place: room, shelf, cabinet, drawer, bin, or a grid cell.

    **This table has no `short_id` column.** `PLAN.md` describes one as
    nullable, and the nullability is the real requirement — nobody will stick
    96 labels on an 8x12 assortment box. But the design also fixes a *single*
    shared ID space in `object_ids`, and carrying a second copy here would be
    two sources of truth that can disagree. Absence of an `object_ids` row
    expresses "no printed ID" exactly, and promoting a slot to a printed ID is
    an insert. Addressing an unlabelled cell stays `parent short_id + slot
    label` (`BIN 4K7T-92M8 / C-07`), which needs no column either.
    """

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    container_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("container_types.id", ondelete="RESTRICT"), index=True
    )

    #: Set for a location that is a generated or materialised cell of its
    #: parent, e.g. `C-07`. NULL for a container that stands on its own.
    slot_label: Mapped[str | None] = mapped_column(String(64))
    row_idx: Mapped[int | None] = mapped_column(Integer)
    col_idx: Mapped[int | None] = mapped_column(Integer)
    #: Footprint within the parent's slot canvas, in base cells. 1x1 for an
    #: ordinary slot; >1 for a merged region. Mirrors
    #: `container_type_slot_templates.row_span/col_span` because an instance
    #: needs the identical fact once it owns its own copy of the layout — the
    #: layout-editor change guard has to know a location's *region* to tell a
    #: safe relabel from a merge that would swallow a neighbour's stock.
    row_span: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    col_span: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    #: Per-slot authoring metadata, editable independently of the container
    #: type once an instance owns its own copy — the layout change guard treats
    #: both as always-safe edits regardless of what the slot holds.
    size_class: Mapped[str | None] = mapped_column(StrEnumType(SizeClass))
    inner_volume_mm3: Mapped[float | None] = mapped_column(Float)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Per-instance overrides of the container type's defaults. NULL means
    #: "use the type", and for `esd_safe` specifically, NULL means "walk the
    #: ancestors and take the nearest non-NULL".
    esd_safe: Mapped[bool | None] = mapped_column(Boolean)
    is_placeable: Mapped[bool | None] = mapped_column(Boolean)
    fill_factor: Mapped[float | None] = mapped_column(Float)

    #: How *this* container's children are drawn (ADR 0006), overriding the
    #: type's answer. NULL means "use the type", exactly as the three overrides
    #: above do — the same precedent, so there is one rule to remember rather
    #: than a second convention for the newest column.
    #:
    #: Unlike `esd_safe` this is **not** walked up the ancestor chain: ESD safety
    #: is a physical property that genuinely propagates downwards (a cabinet
    #: lined with dissipative foam makes its drawers safe), whereas a drawing is
    #: a fact about one level's own children. Inheriting it would mean choosing a
    #: floor plan for a room silently redrew every drawer inside it.
    child_view: Mapped[str | None] = mapped_column(StrEnumType(ChildView))

    #: This one container's own pictogram, overriding the type's — NULL means
    #: "use the type", exactly as `child_view` does, and for the same reason
    #: this is not walked up the ancestor chain either: what a container looks
    #: like is a fact about that container, not something a cabinet imposes on
    #: its drawers. Also not inherited from the type via any derivation — see
    #: `app.models.storage.ContainerType.glyph` — so both this and the type
    #: being unset is a real, renderable state ("no glyph"), not an error.
    glyph: Mapped[str | None] = mapped_column(StrEnumType(ContainerGlyph))

    #: 0..1, how easy this place is to reach. Multiplied by the part's
    #: hot_score in the assignment scorer, so frequently-used parts drift
    #: towards accessible homes.
    access_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5, server_default="0.5"
    )

    #: Capacity is advisory. An over-capacity put-away is **accepted** and this
    #: flag is raised, because a scan that gets rejected teaches the user to
    #: stop scanning.
    is_overfull: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )

    #: The permanent fallback when auto-assignment exhausts every escalation.
    is_staging: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )

    #: Tare belongs to the physical container, not to whatever is in it.
    #: `stock_lots.container_tare_mg` is only a cache of this.
    tare_mg: Mapped[int | None] = mapped_column(Integer)
    tare_sigma_mg: Mapped[float | None] = mapped_column(Float)
    tare_measured_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    tare_source: Mapped[str | None] = mapped_column(String(32))

    #: Drives cycle-count debt and the "never printed" badge respectively.
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_printed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    #: **Removed from the storage tree, but still named by something.**
    #:
    #: A location the user asked to remove is *deleted* whenever nothing
    #: physical or historical names it — no lot has ever sat in it, no ledger
    #: row moved stock through it, no label was printed, no tag is stuck to it.
    #: That is the ordinary case and the one Iliana hit: an empty cell stamped
    #: out of a template.
    #:
    #: The rest cannot be deleted at all. `stock_lots.location_id` and
    #: `stock_ledger.{from,to}_location_id` are `RESTRICT` against a table
    #: nothing may delete from, so a drawer that ever held anything is pinned by
    #: the ledger forever — and that is correct, because deleting history is the
    #: one thing this system must never do. Refusing outright would then leave a
    #: used drawer on screen forever with no way to get rid of it, so those rows
    #: are retired instead: the row stays, the history stays, and the drawer
    #: leaves the tree, the room, its parent's slot canvas and auto-assignment.
    #:
    #: NULL is the overwhelmingly common state, so this is a nullable timestamp
    #: rather than a flag plus a date — the same shape `stock_lots.retired_at`
    #: already uses for the same meaning, and a timestamp answers "when" for
    #: free. Reversible: `POST /api/locations/{id}/restore` clears it. See
    #: `app.services.removal`.
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # --- ADR 0009: where this container stands in its parent's floor plan -----
    #
    # `row_idx`/`col_idx` above are cells on a parent's slot *canvas*. A room has
    # no canvas — ADR 0006's `floor_plan` has "no empty positions, because a
    # space has none" — so a placement is a coordinate, in millimetres, and
    # nothing about it is a slot.
    #
    # **All of it is nullable, and NULL is a real state**: a container that was
    # added to a room but never dragged anywhere is *unplaced*, and must render
    # as such. Defaulting to (0, 0) would put every new box in the same corner
    # and look authored.

    #: **Which parent this coordinate was authored against.** Redundant against
    #: `parent_id` on purpose, and the redundancy *is* the feature: a coordinate
    #: is meaningless in another room, so a placement counts as valid only while
    #: `plan_parent_id == parent_id`. That makes a reparent invalidate the
    #: placement by construction, through any code path — a move endpoint, a
    #: bulk import, a hand-written `UPDATE` — rather than only through the ones
    #: that remembered to clear it. `app.services.room_plan.placement_of()` is
    #: the single reader of that rule.
    #:
    #: **A plain `Integer`, deliberately not a `ForeignKey`.** Two reasons, and
    #: they agree. Practically, SQLite cannot add a foreign key to an existing
    #: table without rebuilding it, and rebuilding `locations` is exactly what
    #: `20260729_0930_c31b7a5e9d04`'s downgrade note says fails: batch mode
    #: renames the table and SQLite re-parses every trigger on it mid-rename,
    #: against `trg_stock_ledger_dirty_occupancy`. Semantically, this is a
    #: *witness* and not a reference — "the coordinates below were authored while
    #: my parent was N" — so a value pointing at a deleted row is not a broken
    #: link, it is precisely the stale placement this column exists to detect.
    #: `object_ids.entity_pk` is the same shape for a related reason.
    plan_parent_id: Mapped[int | None] = mapped_column(Integer)
    #: Position of the footprint's top-left corner, in the parent's own
    #: millimetre coordinates. Signed: the origin is wherever the person drawing
    #: put it, and demanding it be a corner of the room would make the first
    #: wall they drew the wrong one.
    plan_x_mm: Mapped[int | None] = mapped_column(Integer)
    plan_y_mm: Mapped[int | None] = mapped_column(Integer)
    #: Clockwise degrees, 0-359. Integer because a cabinet stands square, at
    #: forty-five degrees across a corner, or it does not matter.
    plan_rotation_deg: Mapped[int | None] = mapped_column(Integer)
    #: The footprint **as drawn**, overriding whatever the container type's
    #: physical dimensions imply. NULL means "use the type's", which is the
    #: common case; these exist because the type says a Raaco is 306 mm wide and
    #: the shelf it is bolted to is not in the type library.
    plan_width_mm: Mapped[int | None] = mapped_column(Integer)
    plan_depth_mm: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # Two drawers cannot share a slot label within one cabinet. Partial,
        # because standalone containers have no slot label at all.
        Index(
            "uq_locations_parent_slot_label",
            "parent_id",
            "slot_label",
            unique=True,
            sqlite_where=slot_label.isnot(None),
        ),
        Index("ix_locations_parent_sort", "parent_id", "sort_order"),
        # Every tree read and every assignment pass filters on this, and the
        # answer is NULL for almost every row — so the index earns its keep by
        # being tiny and by keeping "hide what was removed" off the hot path's
        # conscience.
        Index("ix_locations_retired_at", "retired_at"),
    )


class LocationTag(Base):
    """The NFC tag physically stuck to a container.

    **Nothing mutable is ever written to the tag** — not counts, not fill
    state. Beyond needing the tag in hand to update it, a remote mutation (bulk
    import, reconciliation job, BOM pick) cannot touch a tag it does not
    physically hold, so the tag would go stale while still looking
    authoritative. A tag is a foreign key, not a record.
    """

    __tablename__ = "location_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    #: The 7-byte UID lives in factory-locked pages 0-2, physically separate
    #: from NDEF user memory at page 4. A write that fails partway can corrupt
    #: the NDEF payload but *cannot* touch the UID — so the worst case of
    #: writing is degrading to a UID-only tag, which the verify screen flags.
    tag_uid: Mapped[str | None] = mapped_column(String(32), index=True)
    ndef_url: Mapped[str] = mapped_column(Text, nullable=False)

    #: makeReadOnly() is irreversible and would block the routine relabelling a
    #: hobby inventory needs, so it is opt-in per tag and off by default.
    is_read_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    bind_source: Mapped[str | None] = mapped_column(String(32))

    #: When the *binding* was recorded. Deliberately not renamed despite the
    #: name reading like a claim about the sticker: a bind is a row this server
    #: writes, and the server never holds the tag. What the tag actually holds is
    #: `ndef_state`, reported afterwards by the device that did the writing.
    written_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    #: Whether user memory is known to carry `ndef_url`. Defaults to
    #: `unverified` because that is the honest answer at bind time — the write
    #: has not happened yet, and a reader that cannot write never makes it
    #: happen at all.
    ndef_state: Mapped[str] = mapped_column(
        StrEnumType(NdefState),
        nullable=False,
        default=NdefState.UNVERIFIED,
        server_default=NdefState.UNVERIFIED.value,
    )
    #: When `ndef_state` was last established, by a write read-back or by a
    #: verification walk re-reading the tag. NULL while still `unverified`.
    ndef_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class LocationOccupancy(Base):
    """Cached fill state, one row per location.

    **Dirtied by database triggers**, not application code: `AFTER INSERT ON
    stock_ledger` and `AFTER UPDATE OF location_id ON stock_lots` both flag the
    affected location and every ancestor (so "this shelf is 80% full" stays
    correct without the shelf itself ever holding a lot directly), and `AFTER
    INSERT ON locations` seeds a fresh dirty row so a brand-new location always
    has somewhere for a dirty flag to land. All three live in the migration
    that introduces this table, mirroring how `stock_ledger`'s append-only
    triggers live there rather than in this module.

    A full recompute of every row (`app.db.maintenance.rebuild_location_occupancy`)
    is the escape hatch, exactly as `rebuild_lot_balances` is for lot balances:
    the cache is 100% reconstructible from `stock_lots` and `stock_ledger`, so a
    bug here is a stale number, never lost data.
    """

    __tablename__ = "location_occupancy"

    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), primary_key=True
    )

    #: Snapshot of the strategy that produced this row, so a reader never has
    #: to re-join `container_types` just to know how to label `used`/`capacity`.
    capacity_model: Mapped[str] = mapped_column(StrEnumType(CapacityModel), nullable=False)
    #: NULL means "no defined capacity" (the `none` model, or dimensions that
    #: are not yet filled in) — never a smuggled zero.
    capacity: Mapped[float | None] = mapped_column(Float)
    used: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    fill_ratio: Mapped[float | None] = mapped_column(Float)
    #: The *advisory* full threshold (`container_types.full_threshold`), not
    #: the same thing as `locations.is_overfull`: this can be true at 90% full
    #: while capacity is not literally exceeded.
    is_full: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    is_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1", index=True
    )
    computed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class LocationPlanShape(Base, TimestampMixin):
    """One drawn line on a location's floor plan — a wall, a door, a bench.

    **A drawn wall is not a location** (ADR 0009). It has no `short_id`, holds
    no stock, appears in no tree and resolves from no scan, so it lives here
    rather than becoming a `locations` row with a `PlanShapeKind` on it. The
    alternative considered and rejected was a polygon column on `locations`
    itself, which conflates "the room I am" with "the furniture I contain" and
    can express only one shape per room.

    Geometry is an ordered list of `LocationPlanShapePoint` rows, in the
    location's own millimetre coordinates — **not** an SVG path string, a WKT
    blob or a JSON array. Integers in a small table are queryable, diffable in a
    migration, and need no parser on either side; a blob needs one on both, and
    the first bug in it is silent.
    """

    __tablename__ = "location_plan_shapes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: `CASCADE`, unlike almost every other reference to `locations` in this
    #: schema, which is `RESTRICT`. The reason those are `RESTRICT` is that
    #: deleting a cabinet must never silently take its drawers and their contents
    #: with it. A drawing of a wall has no contents: if the room is gone, so is
    #: its floor plan, and refusing to delete a room because somebody once drew a
    #: door on it would be absurd.
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(StrEnumType(PlanShapeKind), nullable=False)
    #: "north wall", "door to hallway". Free text, for the person drawing.
    label: Mapped[str | None] = mapped_column(String(255))
    #: Whether the last point joins back to the first. An outline and a zone are
    #: closed; a wall run and a door are not.
    is_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: Stroke width in millimetres — a 100 mm stud wall is not a hairline. NULL
    #: means the renderer picks a nominal width for the kind, which is honest:
    #: nobody measures the thickness of a door swing.
    thickness_mm: Mapped[int | None] = mapped_column(Integer)
    #: Draw order, low first. Not a z-index with meaning; just "this hatching
    #: goes under that wall".
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (Index("ix_plan_shapes_location_sort", "location_id", "sort_order"),)


class LocationPlanShapePoint(Base):
    """One vertex of a `LocationPlanShape`, in the location's own millimetres.

    A row per point rather than a coordinate blob, for the same reason
    `parameter_value` is rows rather than a JSON bag: the smallest thing that
    can be wrong should be the smallest thing that can be inspected. There is no
    spatial index and no geometry library — a room holds tens of vertices, and
    every question asked of them so far ("draw this") reads all of them.
    """

    __tablename__ = "location_plan_shape_points"

    shape_id: Mapped[int] = mapped_column(
        ForeignKey("location_plan_shapes.id", ondelete="CASCADE"), primary_key=True
    )
    #: 0-based position in the polyline. Part of the primary key, so the order is
    #: stored rather than inferred from insertion — which `ORDER BY rowid` would
    #: be, and which a re-authored shape would quietly scramble.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    x_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    y_mm: Mapped[int] = mapped_column(Integer, nullable=False)
