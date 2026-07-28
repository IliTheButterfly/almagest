"""`/api/scan/resolve` and `/api/scan/alias` — intake's two endpoints.

One resolve endpoint, not one per format. The client decodes a symbol in the
browser and posts the payload; deciding what it *is* happens here, once, in an
order the design fixes. A phone camera, a HID wedge and the bench station all
post to the same route, which is why adding a reader is a config change.

The pair is a loop, not two features. `resolve` returns `suggest_bind` for
anything it could not pin down, `alias` records what the user says it was, and
the next scan of that payload resolves at step 2 of the chain for ever. That is
what makes an unwritten parser — LCSC's, today — cost taps instead of blocking
intake.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.limits import CountMilli, RowId
from app.db.base import Base
from app.db.session import get_db
from app.models.catalog import Part
from app.models.enums import AliasKind, EntityType
from app.models.scanning import ScanSource
from app.models.stock import StockLot
from app.models.storage import Location
from app.models.types import utcnow
from app.services.scanning import aliases, codes, resolver
from app.services.scanning.aliases import CODE_NORM_MAX_LENGTH
from app.services.scanning.describe import EntityDescription, describe

router = APIRouter(prefix="/api/scan", tags=["scan"])

#: Generous upper bound on a scanned payload. A dense ECIA reel label is a couple
#: of hundred bytes and a QR maxes out near 3 kB, so anything past this is a
#: reader fault or an abusive caller, not a label. Rejecting it is the one place
#: this endpoint says no — and it says no *before* touching the database, so a
#: huge body cannot become a huge row.
PAYLOAD_MAX_LENGTH = 4096

#: `barcode_aliases.symbology` is `String(32)`, and free text on purpose: the
#: names come from decoders and readers this project does not control.
SYMBOLOGY_MAX_LENGTH = 32

#: Entity types with a table to check against today. The rest (`supplier_part`,
#: `project`, `device`) arrive in later phases, and a bind targeting one is
#: accepted unchecked rather than refused — the alias table has always been
#: polymorphic by design, and blocking a legitimate forward-looking binding would
#: be worse than a dangling one, which resolves to a bare `"<type> <pk>"` label.
_CHECKABLE: dict[str, type[Base]] = {
    EntityType.PART: Part,
    EntityType.LOCATION: Location,
    EntityType.STOCK_LOT: StockLot,
}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class ScanTarget(BaseModel):
    entity_type: str
    entity_pk: RowId
    label: str
    #: Derived at read time, never taken from the tag — a container that moved
    #: would make an encoded path a lie.
    label_path: str | None = None
    short_id: str | None = None
    display: str | None = None


class ScanCandidate(BaseModel):
    target: ScanTarget
    via: str = Field(description="How this candidate was found. Display text, never parsed.")
    alias_id: int | None = None
    hit_count: int | None = None


class ScanParsed(BaseModel):
    """Fields read off the payload. Pre-fills the intake form; never authority."""

    mpn: str | None = None
    supplier_part_number: str | None = None
    manufacturer: str | None = None
    quantity_milli: int | None = None
    lot_code: str | None = None
    date_code: str | None = None
    country_of_origin: str | None = None
    purchase_order: str | None = None
    serial: str | None = None
    ean: str | None = None
    di_fields: dict[str, list[str]] = Field(default_factory=dict)
    confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)


class ScanExistingLot(BaseModel):
    lot_id: RowId
    location_id: RowId
    location_name: str
    location_label_path: str | None = None
    qty_milli: int
    status: str
    batch_code: str | None = None


class ScanResolveRequest(BaseModel):
    code: str = Field(
        max_length=PAYLOAD_MAX_LENGTH,
        description="The decoded payload, verbatim, control characters included.",
    )
    symbology: str | None = Field(
        default=None,
        max_length=SYMBOLOGY_MAX_LENGTH,
        description="Whatever the decoder calls the format it read. Recorded, not validated.",
    )
    source_slug: str | None = Field(
        default=None,
        description="`scan_sources.slug`. An unknown slug is recorded as no source, never refused.",
    )


class ScanResolveResponse(BaseModel):
    status: str = Field(description="`resolved`, `ambiguous` or `unknown`.")
    decoded_kind: str = Field(
        description=(
            "Which handler claimed the payload: short_id, alias, ecia, lcsc, mpn, ean, "
            "unknown. Independent of `status` — a label can decode perfectly and still "
            "resolve to nothing."
        )
    )
    normalized: str = Field(description="The key this payload would bind under.")
    suggest_bind: bool = Field(
        description="True on `ambiguous`/`unknown`: offer POST /api/scan/alias."
    )
    target: ScanTarget | None = None
    candidates: list[ScanCandidate] = Field(default_factory=list)
    parsed: ScanParsed | None = None
    existing_lots: list[ScanExistingLot] = Field(
        default_factory=list,
        description=(
            "Lots of the matched part with quantity above zero, so the UI can branch to "
            "known-part re-stock before enrichment or dimensions run."
        ),
    )
    latency_ms: int
    scan_event_id: int


class ScanAliasRequest(BaseModel):
    code: str = Field(max_length=PAYLOAD_MAX_LENGTH)
    symbology: str = Field(
        max_length=SYMBOLOGY_MAX_LENGTH,
        description="Required here, unlike on resolve: an alias records what carried it.",
    )
    entity_type: EntityType
    entity_pk: RowId
    alias_kind: AliasKind = Field(
        default=AliasKind.WHOLE_PAYLOAD,
        description=(
            "What part of the label `code` is. Binding a whole payload recognises that one "
            "package; binding the supplier SKU out of it recognises the next reel of the "
            "same part on its first scan."
        ),
    )
    hint_qty_milli: CountMilli | None = None
    hint_batch: str | None = Field(default=None, max_length=128)
    parsed_json: str | None = Field(
        default=None,
        description="The parse this binding was taught from, so a better parser can be diffed.",
    )
    source_slug: str | None = None


class ScanAliasResponse(BaseModel):
    alias_id: int
    code_norm: str
    created: bool = Field(description="False when an identical binding already existed.")
    hit_count: int
    alias_kind: str
    target: ScanTarget
    scan_event_id: int


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _target(entity: EntityDescription) -> ScanTarget:
    return ScanTarget(
        entity_type=entity.entity_type,
        entity_pk=entity.entity_pk,
        label=entity.label,
        label_path=entity.label_path,
        short_id=entity.short_id,
        display=entity.display,
    )


def _candidate(candidate: resolver.Candidate) -> ScanCandidate:
    return ScanCandidate(
        target=_target(candidate.entity),
        via=candidate.via,
        alias_id=candidate.alias_id,
        hit_count=candidate.hit_count,
    )


def _parsed(parsed: resolver.ParsedFields | None) -> ScanParsed | None:
    if parsed is None:
        return None
    return ScanParsed(
        mpn=parsed.mpn,
        supplier_part_number=parsed.supplier_part_number,
        manufacturer=parsed.manufacturer,
        quantity_milli=parsed.quantity_milli,
        lot_code=parsed.lot_code,
        date_code=parsed.date_code,
        country_of_origin=parsed.country_of_origin,
        purchase_order=parsed.purchase_order,
        serial=parsed.serial,
        ean=parsed.ean,
        di_fields={di: list(values) for di, values in parsed.di_fields.items()},
        confidence=parsed.confidence,
        warnings=list(parsed.warnings),
    )


def _resolve_source(db: Session, slug: str | None) -> int | None:
    """Attribute the scan to a registered reader, and note that it is alive.

    An unknown slug is not an error: a scan from an unregistered reader still has
    to be recorded, which is exactly why `scan_events.source_id` is nullable.
    Touching `last_seen_at` here is nearly free and is the only place a reader
    that has gone quiet would ever show up.
    """
    if not slug:
        return None
    source = db.execute(select(ScanSource).where(ScanSource.slug == slug)).scalar_one_or_none()
    if source is None:
        return None
    source.last_seen_at = utcnow()
    return source.id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/resolve", response_model=ScanResolveResponse)
def resolve_scan(request: ScanResolveRequest, db: Session = Depends(get_db)) -> ScanResolveResponse:
    """Identify a scanned payload. **Never rejects one.**

    An unrecognised code comes back `unknown` with `suggest_bind`, not as an
    error — a scan that gets refused teaches the user to stop scanning, and
    intake friction is what kills systems like this one.
    """
    source_id = _resolve_source(db, request.source_slug)
    resolution = resolver.resolve(
        db, request.code, symbology=request.symbology, source_id=source_id
    )
    db.commit()

    return ScanResolveResponse(
        status=resolution.status,
        decoded_kind=resolution.decoded_kind,
        normalized=resolution.normalized,
        suggest_bind=resolution.suggest_bind,
        target=None if resolution.target is None else _target(resolution.target.entity),
        candidates=[_candidate(candidate) for candidate in resolution.candidates],
        parsed=_parsed(resolution.parsed),
        existing_lots=[
            ScanExistingLot(
                lot_id=lot.lot_id,
                location_id=lot.location_id,
                location_name=lot.location_name,
                location_label_path=lot.location_label_path,
                qty_milli=lot.qty_milli,
                status=lot.status,
                batch_code=lot.batch_code,
            )
            for lot in resolution.existing_lots
        ],
        latency_ms=resolution.latency_ms,
        scan_event_id=resolution.scan_event_id,
    )


@router.post("/alias", response_model=ScanAliasResponse)
def bind_barcode_alias(
    request: ScanAliasRequest, db: Session = Depends(get_db)
) -> ScanAliasResponse:
    """Teach the system what a payload means. The other half of the loop.

    Idempotent: re-binding the same code to the same entity updates the row and
    counts as a hit rather than failing, because a user who binds the same label
    twice is confirming it.
    """
    code_norm = codes.normalize_code(request.code)
    if not code_norm:
        # The one refusal. A binding keyed on nothing would shadow every payload
        # that normalises away, which is the opposite of learning.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "empty_code", "message": "the code normalises to an empty key"},
        )
    if len(code_norm) > CODE_NORM_MAX_LENGTH:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "code_too_long",
                "message": f"normalised code exceeds {CODE_NORM_MAX_LENGTH} characters",
            },
        )

    model = _CHECKABLE.get(request.entity_type)
    if model is not None and db.get(model, request.entity_pk) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_target",
                "message": f"no {request.entity_type} with id {request.entity_pk}",
            },
        )

    alias, created = aliases.upsert(
        db,
        code_norm=code_norm,
        symbology=request.symbology,
        entity_type=request.entity_type,
        entity_pk=request.entity_pk,
        alias_kind=request.alias_kind,
        parsed_json=request.parsed_json,
        hint_qty_milli=request.hint_qty_milli,
        hint_batch=request.hint_batch,
    )

    entity = describe(db, request.entity_type, request.entity_pk)
    event = resolver.record_bind(
        db,
        request.code,
        entity=entity,
        symbology=request.symbology,
        source_id=_resolve_source(db, request.source_slug),
    )
    db.commit()

    return ScanAliasResponse(
        alias_id=alias.id,
        code_norm=alias.code_norm,
        created=created,
        hit_count=alias.hit_count,
        alias_kind=alias.alias_kind,
        target=_target(entity),
        scan_event_id=event.id,
    )
