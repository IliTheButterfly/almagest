"""Physical/generator data, provisioning sessions, and the verification walk.

Three concerns that all belong to the act of *bringing storage into existence*:
describing a container well enough to print it, binding tags to the drawers once
they physically exist, and then proving the binding is right.

The third is not optional busywork. No software can stop a person sticking a tag
on the wrong drawer; it can only detect it. And a mis-bound tag is invisible
until it causes a wrong put-away, at which point the stock is somewhere the
system does not think it is.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin
from app.models.enums import ProvisioningDevice, ProvisioningKind, TagPocket
from app.models.types import StrEnumType, UtcDateTime, utcnow


class ContainerTypePhysical(Base):
    """How to make the thing, for types that are printed rather than bought.

    `generator_params_json` can literally *be* the OpenSCAD parameter set for
    `kennetek/gridfinity-rebuilt-openscad`, which is what turns STL generation
    into something reproducible rather than a hand-curated library of files
    nobody can regenerate after editing a dimension.
    """

    __tablename__ = "container_type_physical"

    container_type_id: Mapped[int] = mapped_column(
        ForeignKey("container_types.id", ondelete="CASCADE"), primary_key=True
    )

    #: Gridfinity footprint in grid units. Redundant with
    #: `container_types.footprint_*` on purpose: these are what gets handed to
    #: the generator, and a printed bin's geometry must not silently change
    #: because someone edited the logical footprint of the type.
    gridfinity_u_w: Mapped[int | None] = mapped_column(Integer)
    gridfinity_u_d: Mapped[int | None] = mapped_column(Integer)
    gridfinity_u_h: Mapped[int | None] = mapped_column(Integer)

    stl_ref: Mapped[str | None] = mapped_column(Text)
    generator: Mapped[str | None] = mapped_column(String(64))
    generator_params_json: Mapped[str | None] = mapped_column(Text)

    #: **Bottom by default, and that is the load-bearing choice.** With the
    #: reader antenna under the station platform, a container set down
    #: identifies itself with no scanning gesture at all.
    tag_pocket: Mapped[str] = mapped_column(
        StrEnumType(TagPocket), nullable=False, default=TagPocket.BOTTOM
    )
    #: Friction-fit label slot, "WxH" in mm. A slot rather than adhesive because
    #: permanent adhesive on polypropylene lifts within months and removable
    #: adhesive on a low-surface-energy plastic is worse.
    label_slot_mm: Mapped[str | None] = mapped_column(String(32))


class ProvisioningSession(Base):
    """One walk along a cabinet, binding or verifying tags.

    **There is no stored cursor.** The next slot is always
    `MIN(sort_order)` among the cabinet's children that lack a `location_tags`
    row, computed fresh. Resuming a half-finished cabinet is therefore free, and
    immune to anything bound out of band — a stored cursor would go wrong the
    first time someone bound one drawer from their phone mid-session.
    """

    __tablename__ = "provisioning_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    root_location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(StrEnumType(ProvisioningKind), nullable=False)
    device_kind: Mapped[str | None] = mapped_column(StrEnumType(ProvisioningDevice))

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    #: Counters for the walk's own progress display. Derived from
    #: `location_tags` and `verification_mismatches`, so they are a convenience
    #: rather than a source of truth.
    bound_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text)


class VerificationMismatch(Base):
    """A tag found somewhere it should not be.

    **Never auto-fixed.** The two plausible repairs — rebind this tag here, or
    swap it with the drawer it actually belongs to — have different physical
    consequences, and only the person holding the drawers can tell which
    happened. So this records the reverse lookup ("this tag belongs to B2") and
    stops.
    """

    __tablename__ = "verification_mismatches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("provisioning_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), nullable=False
    )

    expected_tag_uid: Mapped[str | None] = mapped_column(String(32))
    scanned_tag_uid: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Which slot the scanned tag *actually* belongs to, if any. This is the
    #: whole value of the walk: "you have swapped B2 and B3" is actionable in a
    #: way that "something is wrong" is not.
    scanned_resolved_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL")
    )

    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class LabelPrint(Base, TimestampMixin):
    """A record of what was printed, so a reprint matches the original.

    The server always re-fetches current name, path and quantity at print time
    and never trusts client-supplied text, so a stale label is impossible. What
    it cannot re-derive is which template and DPI produced the card already
    sitting in a slot — hence this.
    """

    __tablename__ = "label_prints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_pk: Mapped[int] = mapped_column(Integer, nullable=False)

    template: Mapped[str] = mapped_column(String(64), nullable=False)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    dpi: Mapped[int | None] = mapped_column(Integer)
    width_mm: Mapped[float | None] = mapped_column(Float)
    height_mm: Mapped[float | None] = mapped_column(Float)

    #: Printing is deliberately NOT queued through the offline outbox: labels
    #: are not audit-critical the way ledger movements are, and a queue would
    #: mean a card silently appearing hours later.
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    job_ref: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (Index("ix_label_prints_entity", "entity_type", "entity_pk"),)
