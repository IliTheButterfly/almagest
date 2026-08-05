"""Parts and the reference data they hang off."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin, TreeMixin
from app.models.enums import ResearchState, SizeClass, VolumeSource
from app.models.types import StrEnumType, UtcDateTime, utcnow


class PartKind(Base):
    """Component, tool, consumable, cable... — non-component inventory is in
    scope from the start, so this is a table rather than a component/not flag."""

    __tablename__ = "part_kinds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Unit(Base):
    """Unit of measure for a part's quantity.

    Note this is the *counting* unit (each, metre, gram), unrelated to the
    physical unit of a parameter value, which lives on `parameter_template`.
    """

    __tablename__ = "units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Whether fractional quantities are meaningful. You can hold 2.5 m of
    #: wire; you cannot hold 2.5 resistors. Quantities are stored in milli-units
    #: either way, so this drives validation and display, not storage.
    is_divisible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Manufacturer(Base):
    __tablename__ = "manufacturers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    #: Casefolded, punctuation-stripped, for matching "TI" to "Texas Instruments"
    #: imports without creating duplicate rows.
    name_norm: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    website: Mapped[str | None] = mapped_column(Text)


class PackageType(Base):
    """A physical package (0603, SOT-23, TO-220), with dimension and mass
    defaults that feed the item-dimension cascade."""

    __tablename__ = "package_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: `0402`/`1005` and friends are a *dual-notation* problem, not a unit
    #: problem: the same package has an imperial and a metric name and sources
    #: use them interchangeably. Both are matchable so the user is never asked
    #: which convention a datasheet used.
    imperial_code: Mapped[str | None] = mapped_column(String(32), index=True)
    metric_code: Mapped[str | None] = mapped_column(String(32), index=True)

    length_mm: Mapped[float | None] = mapped_column(Float)
    width_mm: Mapped[float | None] = mapped_column(Float)
    height_mm: Mapped[float | None] = mapped_column(Float)
    typical_mass_mg: Mapped[float | None] = mapped_column(Float)
    size_class: Mapped[str | None] = mapped_column(StrEnumType(SizeClass))
    is_tht: Mapped[bool | None] = mapped_column(Boolean)


class Packaging(Base):
    """How a lot is packaged — reel, cut tape, tube, tray, bag, loose.

    Carries its own volume, which is what makes capacity packaging-aware
    without a separate regime: a reel occupies the reel's volume whether it
    holds 5000 parts or 12.
    """

    __tablename__ = "packagings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    package_volume_mm3: Mapped[float | None] = mapped_column(Float)
    #: Reel/tube racks measure occupancy in positions, not volume.
    pitch_mm: Mapped[float | None] = mapped_column(Float)


class PartCategory(Base, TreeMixin, TimestampMixin):
    """Logical taxonomy. A *separate tree* from `locations`.

    Same structure, different meaning: what a thing *is* has nothing to do with
    where it physically sits, and conflating them is what makes a storage tree
    unusable as a browse tree.
    """

    __tablename__ = "part_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    #: Default size class for parts in this category, used by the dimension
    #: cascade when the package type has nothing to say.
    default_size_class: Mapped[str | None] = mapped_column(StrEnumType(SizeClass))
    default_fill_factor: Mapped[float | None] = mapped_column(Float)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    colour: Mapped[str | None] = mapped_column(String(16))


class PartTag(Base):
    __tablename__ = "part_tags"

    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class Part(Base, TimestampMixin):
    """The *definition* of a thing. Never a quantity and never a location.

    **Only `name` and `part_kind_id` are NOT NULL.** That is a deliberate,
    load-bearing choice, not laziness: the failure mode that killed every
    abandoned system in this space is intake friction, so an unrecognised
    distributor label has to become a legal row in one tap. Everything else is
    curation, deferred to the review queue via `is_stub`.
    """

    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    part_kind_id: Mapped[int] = mapped_column(
        ForeignKey("part_kinds.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: Nullable on purpose. Mandatory taxonomy is an entry tax paid in
    #: abandoned scans.
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("part_categories.id", ondelete="SET NULL"), index=True
    )

    mpn: Mapped[str | None] = mapped_column(String(255), index=True)
    #: Casefolded and punctuation-stripped, for provider-cache lookups and
    #: bare-MPN barcode resolution.
    mpn_norm: Mapped[str | None] = mapped_column(String(255), index=True)
    manufacturer_id: Mapped[int | None] = mapped_column(
        ForeignKey("manufacturers.id", ondelete="SET NULL"), index=True
    )

    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    package_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("package_types.id", ondelete="SET NULL"), index=True
    )
    uom_id: Mapped[int | None] = mapped_column(ForeignKey("units.id", ondelete="SET NULL"))

    #: Set when the row was created from a scan that could not be resolved.
    #: Drives the "N items need curation" dashboard counter.
    #: Created fast and not yet curated: name and kind only, everything else
    #: deferred. **Called "unfinished" everywhere a person can see it** — this
    #: column keeps the older name because renaming it is a migration across the
    #: API schema and the MCP tool surface, and the two must not drift in
    #: meaning: if this flag ever stops meaning "still needs filling in", the
    #: word in the UI has to change with it.
    is_stub: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    # --- dimension cascade: override -> LxWxH -> package -> category -> class
    length_mm: Mapped[float | None] = mapped_column(Float)
    width_mm: Mapped[float | None] = mapped_column(Float)
    height_mm: Mapped[float | None] = mapped_column(Float)
    #: Fraction of the bounding box the part actually occupies.
    shape_factor: Mapped[float | None] = mapped_column(Float)
    unit_volume_mm3: Mapped[float | None] = mapped_column(Float)
    #: Which cascade rule won, so the UI can say "estimated from package 0603"
    #: rather than presenting a guess as a measurement.
    volume_source: Mapped[str | None] = mapped_column(StrEnumType(VolumeSource))

    #: Learned from a hand-counted reference batch. NULL means counting by
    #: weight is refused for this part rather than attempted badly.
    unit_mass_mg: Mapped[float | None] = mapped_column(Float)

    #: Refreshed nightly as the sum of exp(-age_days/45) over consume events,
    #: normalised. Feeds the access term of the assignment score.
    hot_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")

    #: Unmapped provider fields. Reserved for things that are *never filtered* —
    #: anything filterable belongs in `parameter_value`, where it keeps FK
    #: integrity to a template and an index.
    extra_specs_json: Mapped[str | None] = mapped_column(Text)

    # --- the datasheet-research queue (ADR 0017)
    #
    # Five columns and one index rather than a queue table, for the reason
    # `ExtractionState`'s docstring gives about its own: a state on the row it
    # describes cannot fall out of step with itself, and a separate table would
    # need a row per part that might ever want a datasheet plus something to keep
    # it in step. `app.services.research` is the only module that writes these.
    research_state: Mapped[str] = mapped_column(
        StrEnumType(ResearchState),
        nullable=False,
        default=ResearchState.PENDING,
        server_default=ResearchState.PENDING.value,
    )
    #: Counted when the claim is **granted**, not when a failure is reported — so a
    #: worker that dies without reporting still burns one, which is the only way a
    #: part that reliably kills whatever picks it up stops being re-served forever.
    research_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: When the current lease started. NULL unless `research_state` is `claimed`.
    research_claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: A worker's self-declared name. Diagnostics only — nothing branches on it.
    research_claimed_by: Mapped[str | None] = mapped_column(String(64))
    #: The last failure, verbatim from the worker. For a human reading a health
    #: check. Note this is set for `failed` and **not** for `exhausted`: finding no
    #: datasheet is not an error and must not read as one.
    research_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Two rows for the same MPN from the same manufacturer is nearly always
        # a duplicate-import bug. Partial, because both columns are nullable and
        # any number of rows may legitimately have no MPN at all.
        Index(
            "uq_parts_mpn_norm_manufacturer",
            "mpn_norm",
            "manufacturer_id",
            unique=True,
            sqlite_where=mpn_norm.isnot(None),
        ),
        # The research queue, in one index — the same shape and the same reasoning
        # as `ix_documents_extraction_queue`. `research_attempts` sits second
        # because the claim orders by it: fresh parts before retries, so one part
        # nothing can find a datasheet for cannot starve a part just scanned in.
        Index("ix_parts_research_queue", "research_state", "research_attempts", "id"),
    )


class PartSubstitute(Base):
    """A hand-asserted substitution. Directional: A may be replaceable by B
    without the reverse being true (a tighter-tolerance part substitutes for a
    looser one, not vice versa)."""

    __tablename__ = "part_substitutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    substitute_part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("part_id", "substitute_part_id"),)
