"""Scanning: the taught bindings, the readers, and the raw event log.

Three tables that together make intake cheap, which is the one thing this
project cannot afford to get wrong — every abandoned system in this space died
of data-entry friction, not of a missing feature.

`barcode_aliases` is where the system *learns*. The resolver chain (internal
short ID -> alias -> ECIA -> LCSC -> bare MPN -> EAN -> unknown) never rejects a
payload: an unrecognised code comes back with `suggest_bind`, the user says what
it is once, and a row here makes it resolve at step 2 forever after. That
generalises PartsBox's "ID Anything" from self-minted QR codes to arbitrary
vendor payloads.

`scan_events` keeps the **raw payload of every scan, decoded or not**. That is
the whole point: an unparsed vendor format sitting in a table is a parser
waiting to be written, whereas a discarded one is gone. It is also the only
honest measurement of where intake hurts.

Deliberately absent from this module: `part_fts` and `datasheet_fts`. FTS5
virtual tables cannot be described as SQLAlchemy models — and must not be, or
autogenerate would try to `CREATE TABLE` them as ordinary tables — so they live
only in the migration, alongside the triggers that keep `part_fts` in step with
`parts`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin
from app.models.enums import AliasKind, EntityType, ScanAction, ScanDecodedKind, ScanSourceKind
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: Long enough for a full ECIA/MH10.8.2 reel payload (envelope plus a dozen
#: GS-separated fields), which is the longest thing anyone binds. `String` on
#: SQLite carries no length limit anyway; the number documents intent and is
#: what a future Postgres port would need.
_PAYLOAD_LENGTH = 512

#: Free text, **not** a `StrEnumType`, on purpose — and the exception is worth
#: the inconsistency. Symbology names come from whatever decoded the label:
#: zxing-wasm, a HID wedge's own vocabulary, an NFC stack. The set is decided by
#: hardware we do not control, so validating it in Python would mean a scan
#: refused because a reader spelled its format differently — and the design
#: already promises `nfc_uid`/`nfc_ndef` need no schema change to appear here.
_SYMBOLOGY_LENGTH = 32


class BarcodeAlias(Base):
    """One user-taught binding from a scanned code to a thing.

    Targets an entity by (`entity_type`, `entity_pk`) rather than by a real
    foreign key, for the same reason `object_ids` does: the referent may be a
    part, a location, a lot or a `supplier_part`, and the last of those is what
    makes a DigiKey reel label resolve to part + quantity + PO in one scan.

    `code_norm` is **not unique**. Two suppliers can ship the same EAN, and a
    bare MPN can legitimately name several rows; a code matching more than one
    alias is the `ambiguous` branch of the resolver, which asks the user, rather
    than a data error to be prevented here.
    """

    __tablename__ = "barcode_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Normalised for lookup: trimmed, case-folded, and with the human-readable
    #: separators removed. The *raw* form is never needed here because every
    #: scan's raw payload is already kept in `scan_events`.
    code_norm: Mapped[str] = mapped_column(String(_PAYLOAD_LENGTH), nullable=False, index=True)
    symbology: Mapped[str] = mapped_column(String(_SYMBOLOGY_LENGTH), nullable=False)

    entity_type: Mapped[str] = mapped_column(StrEnumType(EntityType), nullable=False)
    entity_pk: Mapped[int] = mapped_column(Integer, nullable=False)

    alias_kind: Mapped[str] = mapped_column(
        StrEnumType(AliasKind),
        nullable=False,
        default=AliasKind.WHOLE_PAYLOAD,
        server_default=AliasKind.WHOLE_PAYLOAD.value,
    )

    #: The parse that produced this binding, as the parser saw it at the time.
    #: Kept so an improved parser can be diffed against every historical
    #: decision instead of being trusted on faith.
    parsed_json: Mapped[str | None] = mapped_column(Text)

    #: Quantity and batch printed on the label, in the label's own terms. Hints,
    #: never authority: they pre-fill the intake form so a reel becomes stock in
    #: one tap, and the ledger records whatever the human confirms.
    hint_qty_milli: Mapped[int | None] = mapped_column(Integer)
    hint_batch: Mapped[str | None] = mapped_column(String(128))

    #: Ranks candidates when a code matches several aliases, and exposes the
    #: bindings nobody ever uses. Cheap to maintain because a resolve already
    #: writes to this row's table.
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: A count alone cannot tell "used constantly" from "used 50 times in 2026",
    #: which is the difference between a binding worth ranking first and one
    #: worth retiring.
    last_hit_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # Teaching the same code the same answer twice is a duplicate, so the
        # bind endpoint is an upsert that bumps `hit_count`. Unlike the
        # deliberately-absent unique index on `layout_suggestions`, this one
        # really does constrain what it appears to: all four columns are NOT
        # NULL, so there are no NULLs to be treated as distinct from each other.
        UniqueConstraint("code_norm", "symbology", "entity_type", "entity_pk"),
    )


class ScanSource(Base, TimestampMixin):
    """A registered scanner, camera or reader.

    Registration exists so a scan can be attributed. With one user and three
    devices that sounds like bookkeeping; it stops sounding like bookkeeping the
    first time one reader starts producing garbage payloads and the log can say
    which.
    """

    __tablename__ = "scan_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    kind: Mapped[str] = mapped_column(StrEnumType(ScanSourceKind), nullable=False)

    #: Same identifier the client sends as `client_operations.device_id`, so a
    #: scan and the write it caused can be tied to one physical device without
    #: a second registry. Not unique: a browser that clears storage returns as a
    #: new device and the old row stays valid history.
    device_id: Mapped[str | None] = mapped_column(String(64), index=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    #: A reader that has gone quiet is a hardware fault, and this is the only
    #: place that would show it.
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    notes: Mapped[str | None] = mapped_column(Text)


class ScanEvent(Base):
    """One scan, recorded whether or not anything could be made of it.

    **The raw payload is the asset.** A vendor format nobody parses yet is a
    parser waiting to be written, but only if the bytes were kept — so this
    table stores what arrived before any interpretation, and every derived
    column beside it is allowed to be NULL.
    """

    __tablename__ = "scan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow, index=True)

    #: Nullable, and `SET NULL` on delete: an event from an unregistered reader
    #: must still be recorded, and retiring a scanner must not erase the history
    #: of what it read.
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("scan_sources.id", ondelete="SET NULL"), index=True
    )

    symbology: Mapped[str | None] = mapped_column(String(_SYMBOLOGY_LENGTH))

    #: Verbatim, control characters and all — an ECIA payload *is* its GS
    #: (0x1D), RS (0x1E) and EOT (0x04) separators, and stripping them is
    #: exactly the lossy step that would make the format unmineable later. A
    #: reader that ever delivers true binary lands here hex-encoded with
    #: `symbology` saying so, which needs no schema change.
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    #: Groups identical payloads for mining, and backs the duplicate hold-off
    #: that stops one label held in front of a camera firing five resolves.
    #: Not unique — rescanning the same reel next month is normal.
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    decoded_kind: Mapped[str] = mapped_column(
        StrEnumType(ScanDecodedKind),
        nullable=False,
        default=ScanDecodedKind.UNKNOWN,
        server_default=ScanDecodedKind.UNKNOWN.value,
    )

    #: NULL means the chain resolved nothing. Polymorphic like every other
    #: entity reference in the scanning path, so a scan of anything at all can
    #: be recorded without knowing in advance what it turned out to be.
    resolved_entity_type: Mapped[str | None] = mapped_column(StrEnumType(EntityType))
    resolved_entity_pk: Mapped[int | None] = mapped_column(Integer)

    action_taken: Mapped[str] = mapped_column(
        StrEnumType(ScanAction),
        nullable=False,
        default=ScanAction.UNRESOLVED,
        server_default=ScanAction.UNRESOLVED.value,
    )

    #: Server-side resolve time. The design's claim is that scanning is fast
    #: enough to keep using; this is the number that makes that claim checkable
    #: instead of a hope. NULL when no resolve ran at all.
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # The mining query is literally "the unknowns, newest first". Composite
        # rather than a bare index on `decoded_kind`, whose leading column this
        # already serves.
        Index("ix_scan_events_kind_ts", "decoded_kind", "ts"),
    )
