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
from app.models.enums import CapacityModel, ChildLayout, SizeClass, SlotLabelScheme
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
    label` (`BIN 4K7T-92MQ / C-07`), which needs no column either.
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
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Per-instance overrides of the container type's defaults. NULL means
    #: "use the type", and for `esd_safe` specifically, NULL means "walk the
    #: ancestors and take the nearest non-NULL".
    esd_safe: Mapped[bool | None] = mapped_column(Boolean)
    is_placeable: Mapped[bool | None] = mapped_column(Boolean)
    fill_factor: Mapped[float | None] = mapped_column(Float)

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
    written_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
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
