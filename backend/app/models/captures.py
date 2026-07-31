"""Captures: a still taken at the scanner, and what was read off it.

The scanner already decodes barcodes from the live preview and throws every
frame away. That is right for the live loop — a frame is a means to a payload —
but it makes one very common intake situation unrecoverable: a reel label whose
DataMatrix reads fine and whose *printed* manufacturer, date code and quantity
are only legible to a human, standing at the shelf, holding the reel. Those
values get typed in later from memory, or not at all.

A capture is that frame, kept.

**The image is the asset.** This module is shaped exactly like `scan_events`
next door, and for the same reason its docstring gives: the bytes arrive before
anything can interpret them, so the bytes are `NOT NULL` and every derived thing
beside them is nullable and additive. A capture whose barcodes decoded and whose
text was never read is a normal, useful row — not a half-finished one. An OCR
pass that lands a week later appends `capture_regions` rows and changes nothing
else.

**Two tables, not one.** `captures` owns the image and the one fact that is
about the *pass* rather than about any region (`text_status` — see
`CaptureTextStatus`, which exists so "found no text" and "never looked" stay
distinguishable). `capture_regions` owns one outline each. A region cannot be a
column set on `captures` because there are an unbounded number of them, and
`text_status` cannot live on a region because a pass that produced no regions is
precisely the case it has to describe.

**No image bytes here.** The still goes through `app.services.blobstore` like
every other file, so `captures.sha256` is a foreign key into `documents` and
de-duplication, the 64 MiB ceiling and the five-byte magic check are all
inherited rather than reimplemented. Two captures of the same unchanged label
are one blob and two rows, which is correct: they were taken at different times
and may have been read differently.

Deliberately **not** a ledger: no `RAISE(ABORT)` triggers. A capture is evidence
a person chose to keep, and deleting a blurry one must stay a one-click mistake
to fix rather than a compensating row. `stock_ledger` is the thing money and
counts hang off; this is a notebook.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CaptureRegionKind, CaptureTextStatus
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: Matches `scan_events.symbology`, deliberately — a region's symbology is
#: copied straight onto the resolve it triggers, and two different widths would
#: mean a value that fits one table and is truncated in the other.
_SYMBOLOGY_LENGTH = 32


class Capture(Base):
    """One still, plus whether anyone has tried to read the writing on it."""

    __tablename__ = "captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, index=True
    )

    #: The stored image. By row id, like `document_links`, even though the API
    #: addresses a capture's image by sha256 the way every other document call
    #: does — the hash is the *client's* handle on a blob, the id is the
    #: database's. `CASCADE` for the same reason the link table uses it: there is
    #: no meaningful outline over a file that no longer exists.
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The pixel dimensions the regions below were measured against. Stored
    #: rather than read back off the image because the overlay has to scale
    #: quads onto whatever size the image is *rendered* at, and asking the API to
    #: decode a JPEG to answer that would put an image pipeline in the API
    #: process — the one thing ADR 0005 exists to prevent.
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Which reader took it. Nullable and `SET NULL` for the same reason
    #: `scan_events.source_id` is: an unregistered browser must still be able to
    #: capture, and retiring a device must not erase what it photographed.
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("scan_sources.id", ondelete="SET NULL"), index=True
    )
    #: Plain string, no FK — a browser is not a `devices` row and should not have
    #: to become one to keep a photograph. Mirrors `pending_intakes.device_id`.
    device_id: Mapped[str | None] = mapped_column(String(64))

    text_status: Mapped[str] = mapped_column(
        StrEnumType(CaptureTextStatus),
        nullable=False,
        default=CaptureTextStatus.NOT_ATTEMPTED,
        server_default=CaptureTextStatus.NOT_ATTEMPTED.value,
    )

    note: Mapped[str | None] = mapped_column(Text)


class CaptureRegion(Base):
    """One outline on one capture, and the value read inside it.

    The quad is stored as eight explicit integers rather than a JSON blob. It is
    a fixed-arity value with no optional members, so JSON would buy nothing and
    cost the thing this schema is careful about everywhere else: a typed,
    non-null column that a migration can reason about. It is also why there is no
    `CHECK` — there are none anywhere in this schema (see `CLAUDE.md`), and a
    degenerate quad is a display bug, not a corruption.

    Corners are in the image's own pixel space, in the order the decoder gave
    them (`zxing-wasm` reports top-left, top-right, bottom-right, bottom-left;
    an OCR bounding box is squared off into the same order). Rotation therefore
    survives: a reel label read sideways draws a sideways outline, which is a
    real signal to the user that the decode was of the thing they think it was.
    """

    __tablename__ = "capture_regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: `CASCADE`, unlike every other FK here: a region is meaningless without the
    #: image it outlines, and there is nothing to preserve by orphaning it.
    capture_id: Mapped[int] = mapped_column(
        ForeignKey("captures.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[str] = mapped_column(StrEnumType(CaptureRegionKind), nullable=False)

    #: Verbatim, control characters and all — an ECIA payload *is* its GS/RS/EOT
    #: separators, and this is the string that gets posted to
    #: `/api/scan/resolve`. Same reasoning as `scan_events.raw_payload`, which is
    #: where it ends up.
    text: Mapped[str] = mapped_column(Text, nullable=False)

    #: Whatever the decoder called the format. NULL on a `TEXT` region. Free text
    #: rather than an enum for the reason `scan_events.symbology` gives: the set
    #: is decided by hardware and libraries we do not control.
    symbology: Mapped[str | None] = mapped_column(String(_SYMBOLOGY_LENGTH))

    #: 0-100, and **only** ever set on a `TEXT` region. A barcode has no
    #: meaningful confidence — it checksummed or it did not — and storing a
    #: fabricated 100 for one would invite a UI that ranks a guessed word
    #: alongside a verified payload.
    confidence: Mapped[int | None] = mapped_column(Integer)

    #: The resolve this region triggered, when one ran. `SET NULL` on delete so
    #: pruning the event log does not erase the outline. NULL is normal: text
    #: regions are not resolved, and a barcode captured offline never reached
    #: the resolver.
    scan_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("scan_events.id", ondelete="SET NULL"), index=True
    )

    #: Reading order as the client found it, so the chip list is stable between
    #: reloads. Not a uniqueness constraint — two OCR lines can legitimately tie,
    #: and refusing the row over a tie would lose a line to keep a number tidy.
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    x0: Mapped[int] = mapped_column(Integer, nullable=False)
    y0: Mapped[int] = mapped_column(Integer, nullable=False)
    x1: Mapped[int] = mapped_column(Integer, nullable=False)
    y1: Mapped[int] = mapped_column(Integer, nullable=False)
    x2: Mapped[int] = mapped_column(Integer, nullable=False)
    y2: Mapped[int] = mapped_column(Integer, nullable=False)
    x3: Mapped[int] = mapped_column(Integer, nullable=False)
    y3: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        # The only query there is: one capture's regions, in reading order.
        # Composite rather than a bare index on `capture_id`, whose leading
        # column this already serves.
        Index("ix_capture_regions_capture_order", "capture_id", "order_index"),
    )
