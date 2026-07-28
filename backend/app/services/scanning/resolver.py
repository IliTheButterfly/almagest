"""The ordered resolver chain. **First match wins, and the order is the spec.**

    internal short ID -> barcode_aliases -> ECIA/MH10.8.2 -> LCSC
                      -> bare MPN -> EAN/UPC -> unknown

Two things about that order are load-bearing rather than arbitrary:

* **Aliases outrank every parser.** A binding the user taught by hand must not be
  overruled by a parser's inference, however confident. The user knows which reel
  is in their hand; the parser is reading a label that may be printed by a
  distributor whose DI conventions we guessed at.
* **Step 1 claims only what it actually resolves.** A short ID is 8 symbols with
  a mod-37 check, which an arbitrary vendor code passes roughly one time in
  thirty — not rare enough to ignore. If step 1 claimed a well-formed-but-unbound
  code and stopped, binding that code as an alias would be *unreachable* for ever
  after, because step 1 runs first. Yielding to step 2 on a miss is what keeps the
  learning loop able to fix any mistake this chain makes. The dedicated
  provisioning path for a genuinely blank tag is `/s/{short_id}`, which knows the
  payload is ours because the URL is.

A handler returns `None` for "not my format, try the next" and a `HandlerResult`
for "mine". A result with **no candidates is still a match**: it means the payload
was decoded but resolved to nothing — a known-format label for a part we have
never stocked. `scan_events` keeps those two facts separately (`decoded_kind`
versus `action_taken`), because a rising share of undecodable payloads means a
parser is missing while a rising share of decoded-but-unresolved ones means the
catalogue is behind.

Not implemented here, deliberately: the duplicate hold-off. `ScanAction.SUPPRESSED`
exists for it, but the design puts the 3-second payload-hash hold-off in the
browser decoder next to the frame voting, where it can drop a repeat without a
round trip. A server-side debounce would also make an honest rescan — the same
reel, next month — indistinguishable from a stutter.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from ecia_barcode import EciaLabel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Manufacturer, Part
from app.models.enums import EntityType, ScanAction, ScanDecodedKind
from app.models.scanning import ScanEvent
from app.models.stock import StockLot
from app.models.storage import Location
from app.services import shortid
from app.services.scanning import aliases, codes, ecia, lcsc
from app.services.scanning.describe import EntityDescription, describe

ResolutionStatus = Literal["resolved", "ambiguous", "unknown"]

#: A bare MPN is short, printed, and drawn from a keyboard-typeable alphabet.
#: The bounds are generous: `4R7` at the low end is a real part number, and 64
#: characters is longer than any MPN in a distributor catalogue.
_MPN_MIN_LENGTH = 2
_MPN_MAX_LENGTH = 64


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedFields:
    """What a handler read off the payload, independent of what it resolved to.

    Carried on `ambiguous` and `unknown` responses so the UI can pre-fill the
    bind prompt and the intake form from a label whose part we do not hold yet —
    and on `resolved` ones too, because that is where the reel's quantity, lot
    and date code come from. Hints for a human to confirm, never authority: the
    ledger records what the user commits, not what a label claimed.
    """

    mpn: str | None = None
    supplier_part_number: str | None = None
    manufacturer: str | None = None
    #: Milli-units, like every other quantity in the schema.
    quantity_milli: int | None = None
    lot_code: str | None = None
    date_code: str | None = None
    country_of_origin: str | None = None
    purchase_order: str | None = None
    serial: str | None = None
    ean: str | None = None
    #: The raw DI map for a structured label, kept whole because a field this
    #: system does not model yet is still worth showing and still worth mining.
    di_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: The parser's own confidence, when it has one. `None` means the handler
    #: does not express confidence rather than "certain".
    confidence: float | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """One thing the payload might refer to."""

    entity: EntityDescription
    #: How this candidate was found, for display next to an ambiguous choice
    #: ("taught binding" reads very differently from "matched a part number").
    #: Advisory text, never parsed.
    via: str
    #: Set when a `barcode_aliases` row produced this candidate, so an
    #: unambiguous resolution can be counted against it.
    alias_id: int | None = None
    hit_count: int | None = None


@dataclass(frozen=True)
class ExistingLot:
    """Stock of the matched part that already exists somewhere.

    Deliberately carries no short IDs. The known-part re-stock screen shows
    identity, path and quantity; looking up a printed ID per lot would be one
    query each for something nothing displays.
    """

    lot_id: int
    location_id: int
    location_name: str
    location_label_path: str | None
    qty_milli: int
    status: str
    batch_code: str | None


@dataclass(frozen=True)
class HandlerResult:
    """A handler's claim on a payload. No candidates still means "mine"."""

    candidates: tuple[Candidate, ...] = ()
    parsed: ParsedFields | None = None


@dataclass(frozen=True)
class ScanResolution:
    status: ResolutionStatus
    #: Which handler claimed the payload. Independent of `status` on purpose.
    decoded_kind: ScanDecodedKind
    #: The `barcode_aliases.code_norm` key this payload would bind under.
    normalized: str
    suggest_bind: bool
    latency_ms: int
    scan_event_id: int
    target: Candidate | None = None
    #: Every match, including the single one when `status` is `resolved`, so a
    #: client has one place to look regardless of outcome.
    candidates: tuple[Candidate, ...] = ()
    parsed: ParsedFields | None = None
    existing_lots: tuple[ExistingLot, ...] = ()


Handler = Callable[[Session, str], HandlerResult | None]


# ---------------------------------------------------------------------------
# Handlers, in order
# ---------------------------------------------------------------------------


def _handle_short_id(session: Session, payload: str) -> HandlerResult | None:
    """Step 1. An internal short ID, bare or inside the `/s/{id}` tag URL.

    Claims only on a hit — see the module docstring for why a well-formed miss
    must fall through instead of ending the chain.
    """
    binding = shortid.resolve(session, codes.short_id_candidate(payload))
    if binding is None:
        return None

    entity = describe(session, binding.entity_type, binding.entity_pk, short_id=binding.short_id)
    return HandlerResult(candidates=(Candidate(entity=entity, via="short_id"),))


def _handle_alias(session: Session, payload: str) -> HandlerResult | None:
    """Step 2. A binding the user taught for this exact payload."""
    rows = aliases.lookup(session, codes.normalize_code(payload))
    if not rows:
        return None

    return HandlerResult(
        candidates=tuple(
            Candidate(
                entity=describe(session, alias.entity_type, alias.entity_pk),
                via=f"alias:{alias.alias_kind}",
                alias_id=alias.id,
                hit_count=alias.hit_count,
            )
            for alias in rows
        )
    )


def _handle_ecia(session: Session, payload: str) -> HandlerResult | None:
    """Step 3. An ECIA EIGP-114 / ANSI MH10.8.2 distributor label."""
    label = ecia.parse(payload)
    if label is None:
        return None

    parsed = ParsedFields(
        mpn=label.customer_part_number or label.supplier_part_number,
        supplier_part_number=label.supplier_part_number,
        manufacturer=label.manufacturer,
        quantity_milli=ecia.quantity_milli(label),
        lot_code=label.lot_code,
        date_code=label.date_code,
        country_of_origin=label.country_of_origin,
        purchase_order=label.purchase_order,
        serial=label.serial,
        di_fields=dict(label.fields),
        confidence=label.confidence,
        warnings=tuple(label.warnings),
    )
    return HandlerResult(candidates=_ecia_candidates(session, label), parsed=parsed)


def _ecia_candidates(session: Session, label: EciaLabel) -> tuple[Candidate, ...]:
    """Resolve a parsed label to things we hold.

    Taught bindings win inside this handler too, for the same reason step 2
    precedes step 3 overall. That is also what makes binding a *supplier SKU*
    worth doing: the whole payload of the next reel differs (new lot, new
    quantity), but its `1P` does not, so a SKU binding is what makes the second
    reel of a part resolve on its first scan.
    """
    values = ecia.mpn_candidates(label)

    taught: list[Candidate] = []
    for value in values:
        for alias in aliases.lookup(session, codes.normalize_code(value)):
            taught.append(
                Candidate(
                    entity=describe(session, alias.entity_type, alias.entity_pk),
                    via=f"ecia_alias:{alias.alias_kind}",
                    alias_id=alias.id,
                    hit_count=alias.hit_count,
                )
            )
    if taught:
        return _dedupe(taught)

    parts = _parts_by_mpn(session, values)
    parts = _narrow_by_manufacturer(session, parts, label.manufacturer)
    return _dedupe(
        Candidate(entity=describe(session, EntityType.PART, part.id), via="ecia_mpn")
        for part in parts
    )


def _handle_lcsc(_session: Session, payload: str) -> HandlerResult | None:
    """Step 4. LCSC's proprietary format — a documented stub that never claims.

    See `app.services.scanning.lcsc`: we have no real samples, and a guessed
    format would produce confidently wrong part identifications, which is far
    worse than falling through to `unknown` and letting the user bind the label
    once.
    """
    # Dispatched for real rather than skipped, so implementing the parser is
    # filling in a body instead of rewiring the chain. Its return type is `None`
    # today, which is why the call cannot simply be returned.
    lcsc.parse(payload)
    return None


def _handle_mpn(session: Session, payload: str) -> HandlerResult | None:
    """Step 5. The payload is nothing but a manufacturer part number.

    **Claims only when a part actually matches.** An MPN-shaped string that
    matches nothing is indistinguishable from any other opaque code, so claiming
    it would swallow payloads step 6 can still classify — every 13-digit numeric
    code looks exactly as much like a part number as it does like an EAN. Making
    the claim conditional on a hit is what keeps the documented order meaningful
    instead of making the step after it unreachable.
    """
    if not _looks_like_bare_mpn(payload):
        return None
    key = codes.normalize_mpn(payload)
    parts = _parts_by_mpn_keys(session, (key,))
    if not parts:
        return None

    return HandlerResult(
        candidates=_dedupe(
            Candidate(entity=describe(session, EntityType.PART, part.id), via="mpn")
            for part in parts
        ),
        parsed=ParsedFields(mpn=payload.strip()),
    )


def _handle_ean(_session: Session, payload: str) -> HandlerResult | None:
    """Step 6. A retail GTIN — EAN-8/13, UPC-A or ITF-14.

    Classifies, and resolves nothing. There is deliberately no `parts.ean`
    column: a GTIN identifies a *package* a distributor happened to ship, not a
    part definition, and mapping one to a thing we hold is exactly the job
    `barcode_aliases` exists for. So this handler's whole contribution is to let
    the bind prompt say "retail barcode" instead of "unrecognised", and to make
    the mining query able to tell the two apart. Once bound, the code resolves at
    step 2 — with the same normalisation, so no lookup is needed here.
    """
    digits = codes.normalize_code(payload)
    if not codes.is_gtin(digits):
        return None
    return HandlerResult(parsed=ParsedFields(ean=digits))


#: **The order is the specification.** A test asserts this literal matches the
#: dispatch table below, so reordering the handlers cannot pass review as a
#: refactor — it has to be an edit to this tuple, which is an edit to the spec.
HANDLER_ORDER: Final[tuple[ScanDecodedKind, ...]] = (
    ScanDecodedKind.SHORT_ID,
    ScanDecodedKind.ALIAS,
    ScanDecodedKind.ECIA,
    ScanDecodedKind.LCSC,
    ScanDecodedKind.MPN,
    ScanDecodedKind.EAN,
)

#: The kind is supplied by the slot, not by the handler, so a handler cannot
#: misreport which step claimed a payload.
HANDLERS: Final[tuple[tuple[ScanDecodedKind, Handler], ...]] = (
    (ScanDecodedKind.SHORT_ID, _handle_short_id),
    (ScanDecodedKind.ALIAS, _handle_alias),
    (ScanDecodedKind.ECIA, _handle_ecia),
    (ScanDecodedKind.LCSC, _handle_lcsc),
    (ScanDecodedKind.MPN, _handle_mpn),
    (ScanDecodedKind.EAN, _handle_ean),
)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def resolve(
    session: Session,
    payload: str,
    *,
    symbology: str | None = None,
    source_id: int | None = None,
) -> ScanResolution:
    """Run the chain, record the scan, and return what was found.

    **Records a `scan_events` row unconditionally** — resolved, ambiguous,
    unknown or empty. The raw payload of something nobody can parse is a parser
    waiting to be written; discarded, it is gone. The latency goes with it,
    because "scanning is fast enough to keep using" is the design's central claim
    about intake and this is the number that makes it checkable.

    The caller owns the transaction: this flushes so `scan_event_id` is real, and
    commits nothing.
    """
    started = time.perf_counter()

    decoded_kind = ScanDecodedKind.UNKNOWN
    result: HandlerResult | None = None
    for handler_kind, handler in HANDLERS:
        result = handler(session, payload)
        if result is not None:
            decoded_kind = handler_kind
            break

    candidates = result.candidates if result is not None else ()
    parsed = result.parsed if result is not None else None

    target: Candidate | None = None
    if len(candidates) == 1:
        status: ResolutionStatus = "resolved"
        action = ScanAction.RESOLVED
        target = candidates[0]
        if target.alias_id is not None:
            aliases.record_hit(session, target.alias_id)
    elif candidates:
        status, action = "ambiguous", ScanAction.AMBIGUOUS
    else:
        status, action = "unknown", ScanAction.UNRESOLVED

    normalized = codes.normalize_code(payload)
    # A payload that normalises to nothing was whitespace and separators. There
    # is no key to bind it under, and offering to bind one would create a row
    # that shadows every future empty scan.
    suggest_bind = status != "resolved" and bool(normalized)
    if suggest_bind and parsed is None:
        # Uniform contract: `ambiguous` and `unknown` always carry `parsed`, even
        # when no handler recognised the format, so a client never has to branch
        # on its presence.
        parsed = ParsedFields()

    existing_lots = _existing_lots(session, target) if target is not None else ()

    latency_ms = _elapsed_ms(started)
    event = _new_event(
        session,
        payload=payload,
        symbology=symbology,
        source_id=source_id,
        decoded_kind=decoded_kind,
        action=action,
        target=None if target is None else target.entity,
        latency_ms=latency_ms,
    )

    return ScanResolution(
        status=status,
        decoded_kind=decoded_kind,
        normalized=normalized,
        suggest_bind=suggest_bind,
        latency_ms=latency_ms,
        scan_event_id=event.id,
        target=target,
        candidates=candidates,
        parsed=parsed,
        existing_lots=existing_lots,
    )


def record_bind(
    session: Session,
    payload: str,
    *,
    entity: EntityDescription,
    symbology: str | None = None,
    source_id: int | None = None,
    latency_ms: int | None = None,
) -> ScanEvent:
    """Log the moment a binding was taught, as `ScanAction.BOUND`.

    A second row for a payload that already has an `unresolved` one, and that is
    the point: the pair is the evidence that the learning loop closed. Counting
    only the unresolved scan would make intake look permanently broken, and
    counting only the bind would hide how long the code went unrecognised.
    """
    return _new_event(
        session,
        payload=payload,
        symbology=symbology,
        source_id=source_id,
        decoded_kind=ScanDecodedKind.ALIAS,
        action=ScanAction.BOUND,
        target=entity,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _new_event(
    session: Session,
    *,
    payload: str,
    symbology: str | None,
    source_id: int | None,
    decoded_kind: ScanDecodedKind,
    action: ScanAction,
    target: EntityDescription | None,
    latency_ms: int | None,
) -> ScanEvent:
    event = ScanEvent(
        source_id=source_id,
        symbology=symbology,
        raw_payload=payload,
        # UTF-8 fixed so the digest is stable whatever the reader's own encoding
        # was. It groups identical payloads for mining and backs the client's
        # duplicate hold-off; `raw_payload` remains the authority.
        payload_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        decoded_kind=decoded_kind,
        resolved_entity_type=None if target is None else target.entity_type,
        resolved_entity_pk=None if target is None else target.entity_pk,
        action_taken=action,
        latency_ms=latency_ms,
    )
    session.add(event)
    session.flush()
    return event


def _looks_like_bare_mpn(payload: str) -> bool:
    """A single printed token, not a structured record.

    Rejecting anything with a control character is what keeps this from claiming
    a truncated ECIA payload whose envelope was lost.
    """
    text = payload.strip()
    if not (_MPN_MIN_LENGTH <= len(text) <= _MPN_MAX_LENGTH):
        return False
    if any(character < " " or character == "\x7f" for character in text):
        return False
    return all(character.isalnum() or character in "./+-_# " for character in text)


def _parts_by_mpn(session: Session, values: Sequence[str]) -> list[Part]:
    return _parts_by_mpn_keys(session, tuple(codes.normalize_mpn(value) for value in values))


def _parts_by_mpn_keys(session: Session, keys: Sequence[str]) -> list[Part]:
    wanted = {key for key in keys if key}
    if not wanted:
        return []
    return list(
        session.execute(
            # `mpn_norm` is indexed, and ordering by id keeps an ambiguous
            # candidate list stable between calls — the same scan must not
            # reshuffle the choices under the user's finger.
            select(Part).where(Part.mpn_norm.in_(wanted)).order_by(Part.id)
        ).scalars()
    )


def _narrow_by_manufacturer(
    session: Session, parts: list[Part], manufacturer: str | None
) -> list[Part]:
    """Use DI `1V` to break an MPN tie, and only to break one.

    `uq_parts_mpn_norm_manufacturer` means two rows sharing an `mpn_norm` differ
    by manufacturer, so this is the disambiguator by construction. It compares
    *computed* squashed names rather than trusting `manufacturers.name_norm`,
    because no write path fixes that column's normalisation rule yet and a
    mismatch there would silently discard the right candidate.

    Conservative on purpose: an exact squashed match only, so "TI" does not have
    to equal "Texas Instruments", and narrowing to nothing keeps the full list.
    Asking the user is a fine outcome; picking the wrong part is not.
    """
    if len(parts) < 2 or not manufacturer:
        return parts

    wanted = codes.normalize_mpn(manufacturer)
    if not wanted:
        return parts

    ids = {part.manufacturer_id for part in parts if part.manufacturer_id is not None}
    if not ids:
        return parts
    matching = {
        row.id
        for row in session.execute(select(Manufacturer).where(Manufacturer.id.in_(ids))).scalars()
        if codes.normalize_mpn(row.name) == wanted
    }
    narrowed = [part for part in parts if part.manufacturer_id in matching]
    return narrowed or parts


def _dedupe(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """Collapse candidates naming the same row.

    Two DIs on one label can carry the same value, and an MPN can be bound as
    both a whole payload and a supplier SKU. Either would otherwise turn a
    perfectly determined scan into a spurious `ambiguous`.
    """
    seen: dict[tuple[str, int], Candidate] = {}
    for candidate in candidates:
        seen.setdefault((candidate.entity.entity_type, candidate.entity.entity_pk), candidate)
    return tuple(seen.values())


def _existing_lots(session: Session, target: Candidate) -> tuple[ExistingLot, ...]:
    """Stock of the matched part that already exists, quantity above zero.

    **A resolver responsibility, not the UI's.** Workflow 2 branches to
    known-part re-stock *before* enrichment or dimensions run, and it can only do
    that if the resolution itself already says "you have 2400 of these in Drawer
    A3". Leaving it to a follow-up request would mean the client had to know to
    ask, and the fast path would keep paying for screens it should skip.
    """
    part_id = _part_id_of(session, target.entity)
    if part_id is None:
        return ()

    rows = session.execute(
        select(StockLot, Location)
        .join(Location, StockLot.location_id == Location.id)
        .where(StockLot.part_id == part_id, StockLot.qty_milli_cached > 0)
        # Balances come from the cached column, never from summing the ledger.
        .order_by(StockLot.qty_milli_cached.desc(), StockLot.id)
    ).all()

    return tuple(
        ExistingLot(
            lot_id=lot.id,
            location_id=location.id,
            location_name=location.name,
            location_label_path=location.label_path,
            qty_milli=lot.qty_milli_cached,
            status=lot.status,
            batch_code=lot.batch_code,
        )
        for lot, location in rows
    )


def _part_id_of(session: Session, entity: EntityDescription) -> int | None:
    """The part a resolution is about, if any.

    A lot counts: scanning one reel of a part is still a reason to be told about
    the other reel on the shelf.
    """
    if entity.entity_type == EntityType.PART:
        return entity.entity_pk
    if entity.entity_type == EntityType.STOCK_LOT:
        lot = session.get(StockLot, entity.entity_pk)
        return None if lot is None else lot.part_id
    return None
