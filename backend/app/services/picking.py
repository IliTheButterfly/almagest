"""The pick list: the physical walk that fetches a build's parts.

"Where do I get these" is a different question from "am I short", and the
difference is entirely one of **order**. `locations` caches an `id_path` of
numeric ids wrapped in separators, so sorting stops by it puts every drawer of
one cabinet together — the user crosses the room once. Ordering by BOM line
instead makes them cross it once per line, thirty times for a thirty-line board,
and that is the whole reason this module exists rather than the shortage report
being rendered with a location column bolted on.

Two kinds of stop end up in the same walk, deliberately:

* **a hold this build already has** (`stock_allocations` in `RESERVED`) — a
  specific lot in a specific bin, so the instruction is exact rather than a
  proposal. `allocation_id` comes back with it, which is what lets the caller
  stage it *by id* and consume the hold instead of opening a second one beside
  it;
* **a proposal** for demand nothing is held against yet, drawn from free stock.
  `allocation_id` is NULL, so the caller stages straight from the lot.

Availability is read through `app.services.reservations`, so a lot another build
was promised is never offered twice, and stock sitting in a project's staging
boxes is never offered at all (ADR 0004: those parts are spoken for).

**A line that cannot be picked is listed, never omitted.** An unmatched BOM line
has no part to go and look for, and a short line has less stock than it needs;
both come back in `gaps`, because a pick list that silently dropped them reads as
complete and the user walks to the bench missing parts.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import PATH_SEP
from app.models.catalog import Part
from app.models.enums import AllocationState, EntityType, LotStatus, ShortageKind
from app.models.identity import ObjectId
from app.models.projects import BomLine, ProjectBuild, StockAllocation
from app.models.stock import StockLot
from app.models.storage import Location
from app.services import reservations, staging


@dataclass(frozen=True)
class PickTake:
    """Take this much of this part out of this lot, for this line.

    `allocation_id` is the actionable half: non-NULL means the parts are already
    held for this build, and the next step is a stage *carrying that id*, which
    consumes the hold. NULL means nothing is held and the caller stages from the
    lot directly. Guessing wrong writes a second hold beside the first, so it is
    stated rather than inferred.
    """

    #: NULL for a hold taken against the build as a whole rather than a line —
    #: `reserve` permits that, and dropping such a hold from the walk would send
    #: the user back to the same shelf a second time.
    bom_line_id: int | None
    line_no: int | None
    designators: str | None
    part_id: int
    part_name: str
    part_mpn: str | None
    lot_id: int
    qty_milli: int
    allocation_id: int | None
    #: The line asked for a different part and this one is an accepted alternate
    #: (`bom_line_substitutes`). Called out because substituting is a decision a
    #: human made once and is now being acted on blind, at a drawer.
    is_substitute: bool
    #: The take empties the lot, so the whole package can be carried away rather
    #: than counted out. Cheaper *and* safer — there is no count to get wrong —
    #: which is why the draw order below prefers it.
    whole_lot: bool


@dataclass(frozen=True)
class PickStop:
    """One place to stand, and everything to take while standing there.

    Grouped by location rather than listed one take at a time because the cost
    being optimised is finding a drawer and opening it, not reading a line.
    """

    location_id: int
    label_path: str
    #: The ordering key, exposed so a client can re-sort identically instead of
    #: inventing its own idea of geography. Numeric ids, and never printed on
    #: anything — an encoded path on a label becomes a lie the moment a drawer
    #: changes cabinet.
    id_path: str
    #: The printed code on the bin, when it has one, so a stop can be
    #: scan-verified instead of matched by eye against a label.
    short_id: str | None
    takes: tuple[PickTake, ...]

    @property
    def qty_milli(self) -> int:
        return sum(take.qty_milli for take in self.takes)


@dataclass(frozen=True)
class PickGap:
    """A line the walk cannot finish, and why.

    `SHORT` and `UNIDENTIFIED` stay separate here for the same reason they do in
    the shortage report: one is fixed by ordering parts and the other by a human
    saying what the part *is*, and no quantity is computable for the second at
    all.
    """

    bom_line_id: int
    line_no: int
    part_id: int | None
    kind: ShortageKind
    #: What the line still needs, from the shortage report: demand minus what this
    #: build already holds, has staged or has built in.
    needed_milli: int
    #: How much of `needed_milli` the stops actually cover. Non-zero *together
    #: with* a non-zero `shortfall_milli` is a partly pickable line — the case a
    #: pick list most easily lies about, by listing the takes and saying nothing.
    pickable_milli: int
    shortfall_milli: int


@dataclass(frozen=True)
class PickList:
    build_id: int
    stops: tuple[PickStop, ...]
    gaps: tuple[PickGap, ...]

    @property
    def is_complete(self) -> bool:
        """Nothing outstanding is unaccounted for. **Not** "there is nothing to
        do": an empty walk with no gaps means the build already holds the lot."""
        return not self.gaps

    @property
    def qty_milli(self) -> int:
        return sum(stop.qty_milli for stop in self.stops)


@dataclass(frozen=True)
class _Candidate:
    """One lot that could supply something, and where it is."""

    lot_id: int
    part_id: int
    location_id: int
    id_path: str
    label_path: str
    on_hand_milli: int
    free_milli: int


def pick_list_for_build(session: Session, build: ProjectBuild) -> PickList:
    """The walk, in walking order.

    Built in two passes, because they answer different questions and conflating
    them is how the ordering rule gets lost:

    1. **decide what to take from where** — existing holds first (they name their
       own lot), then proposals drawn from free stock, line by line in `line_no`
       order and netting the pool as they go, exactly as `shortage_for_build`
       does, so the two cannot disagree about who gets scarce stock;
    2. **sort by `locations.id_path`** — see `_stops`. That is the feature.

    Need comes from `reservations.shortage_for_build` rather than being
    recomputed, so the number on the build screen and the walk the user takes are
    the same number — and it is the definition that already accounts for
    `assembly_count`, for staged parts and for holds another build owns.
    """
    report = reservations.shortage_for_build(session, build)
    lines = {line.id: line for line in _bom_lines(session, build.project_id)}
    substitutes = reservations.substitutes_by_line(session, lines.keys())

    takes = _held_takes(session, build, lines)

    # Only a line that still needs something draws from free stock. One whose
    # holds already cover it contributes those holds to the walk and nothing here.
    outstanding = {
        line.bom_line_id: line.needed_milli
        for line in report.lines
        if line.needed_milli > 0 and line.part_id is not None
    }
    part_ids: set[int] = set()
    for line_id in outstanding:
        part_ids.update(_preference(lines[line_id], substitutes.get(line_id, ())))

    candidates = _candidates(session, part_ids)
    free = {candidate.lot_id: candidate.free_milli for candidate in candidates}
    parts = _parts(session, part_ids)

    # Bins the walk already has to open, so drawing more from one of them is free.
    # Seeded from the holds because those stops are already committed — a plan that
    # sent the user to a fourth drawer to avoid cutting tape in one they were
    # standing in front of anyway would be optimising the wrong cost.
    visited = {
        location_id: id_path
        for location_id, id_path, _ in _places(session, {take.lot_id for take in takes}).values()
    }

    covered: dict[int, int] = {}
    for line_id, needed in outstanding.items():
        line = lines[line_id]
        preference = {
            part_id: index
            for index, part_id in enumerate(_preference(line, substitutes.get(line_id, ())))
        }
        drawn = _draw(candidates, free, preference, needed, visited)
        covered[line_id] = sum(qty for _, qty in drawn)
        takes.extend(
            PickTake(
                bom_line_id=line_id,
                line_no=line.line_no,
                designators=line.designators,
                part_id=candidate.part_id,
                part_name=parts[candidate.part_id].name,
                part_mpn=parts[candidate.part_id].mpn,
                lot_id=candidate.lot_id,
                qty_milli=qty,
                allocation_id=None,
                is_substitute=candidate.part_id != line.part_id,
                whole_lot=qty == candidate.on_hand_milli,
            )
            for candidate, qty in drawn
        )

    gaps = tuple(
        PickGap(
            bom_line_id=line.bom_line_id,
            line_no=line.line_no,
            part_id=line.part_id,
            kind=line.kind,
            needed_milli=line.needed_milli,
            pickable_milli=covered.get(line.bom_line_id, 0),
            shortfall_milli=line.needed_milli - covered.get(line.bom_line_id, 0),
        )
        for line in report.lines
        # A DNP line needs nothing and a covered line's need is met by the takes
        # above; what is left is genuinely un-walkable and has to be said out loud.
        if line.needed_milli > covered.get(line.bom_line_id, 0)
    )
    return PickList(build_id=build.id, stops=_stops(session, takes), gaps=gaps)


def _preference(line: BomLine, substitute_part_ids: tuple[int, ...]) -> tuple[int, ...]:
    """The parts that satisfy a line, best first: its own, then its alternates.

    Exactly the order `reservations._net_one_line` accumulates availability in. If
    these two ever diverge, the shortage report and the walk are answering "what
    satisfies this line" differently, and the user acts on the wrong one.
    """
    if line.part_id is None:
        return substitute_part_ids
    return (line.part_id, *substitute_part_ids)


def _bom_lines(session: Session, project_id: int) -> list[BomLine]:
    return list(
        session.execute(
            select(BomLine)
            .where(BomLine.project_id == project_id)
            .order_by(BomLine.line_no, BomLine.id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Pass 1: what to take
# ---------------------------------------------------------------------------


def _held_takes(session: Session, build: ProjectBuild, lines: dict[int, BomLine]) -> list[PickTake]:
    """This build's `RESERVED` holds, as exact instructions.

    Clamped to what the bin can actually hand over —
    `on_hand - (everyone else's holds)`, filled in `(lot_id, id)` order — which is
    **the same arithmetic `reservations._holdings_by_line` uses** to decide how
    much of a hold is deliverable. That parallel is load-bearing rather than
    tidy: the shortage report subtracts the deliverable part of a hold from what
    a line needs, so if this offered the *recorded* quantity instead, the walk and
    the report would together promise the same units twice — once as a hold to go
    and fetch, once as demand a proposal covers.

    A hold on a bin a recount emptied therefore produces no take at all, and the
    line comes back in `gaps` because the report counted that hold as
    undeliverable. Silent is exactly what that must not be, which is why the
    report keeps `undeliverable_milli` as its own number.
    """
    rows = session.execute(
        select(StockAllocation, Part, StockLot)
        .join(Part, Part.id == StockAllocation.part_id)
        .join(StockLot, StockLot.id == StockAllocation.lot_id)
        .where(
            StockAllocation.build_id == build.id,
            StockAllocation.state == AllocationState.RESERVED,
        )
        .order_by(StockAllocation.lot_id, StockAllocation.id)
    ).all()

    own: dict[int, int] = {}
    for allocation, _, lot in rows:
        own[lot.id] = own.get(lot.id, 0) + allocation.qty_milli

    headroom: dict[int, int] = {}
    takes: list[PickTake] = []
    for allocation, part, lot in rows:
        if lot.id not in headroom:
            # Quarantined or retired stock can be held but not carried away, so it
            # is worth no headroom — the same zero `available_by_part` gives it,
            # and the same lot `reserve` refuses outright.
            headroom[lot.id] = (
                0
                if LotStatus(lot.status) is not LotStatus.ACTIVE
                else max(0, lot.qty_milli_cached - (lot.qty_reserved_milli_cached - own[lot.id]))
            )
        qty = min(allocation.qty_milli, headroom[lot.id])
        headroom[lot.id] -= qty
        if qty <= 0:
            continue
        line = None if allocation.bom_line_id is None else lines.get(allocation.bom_line_id)
        takes.append(
            PickTake(
                bom_line_id=allocation.bom_line_id,
                line_no=None if line is None else line.line_no,
                designators=None if line is None else line.designators,
                part_id=allocation.part_id,
                part_name=part.name,
                part_mpn=part.mpn,
                lot_id=lot.id,
                qty_milli=qty,
                allocation_id=allocation.id,
                # A hold already names one part, so whether it is the line's own
                # or an alternate is a decision on record — read off the line
                # rather than re-derived from the substitute table.
                is_substitute=line is not None
                and line.part_id is not None
                and line.part_id != allocation.part_id,
                whole_lot=qty == lot.qty_milli_cached,
            )
        )
    return takes


def _candidates(session: Session, part_ids: set[int]) -> list[_Candidate]:
    """Every `ACTIVE` lot of these parts that is not in a project's staging box.

    `free_milli` comes from `reservations.available`, clamped at zero, so "free"
    has one definition across the report and the walk. The clamp matters for the
    same reason it does in `available_by_part`: a bin whose balance went negative
    is one visible anomaly, and letting it eat another bin's real stock would turn
    that into a fabricated shortage somewhere else.

    The staging exclusion is by **position in the tree**, matching
    `available_by_part` exactly — filtering on `is_staging` would also exclude
    `INBOX`, whose stock is ordinary free stock, and a pick list that refused to
    send anyone to the inbox would send them shopping instead.
    """
    if not part_ids:
        return []
    query = (
        select(StockLot, Location)
        .join(Location, Location.id == StockLot.location_id)
        .where(StockLot.part_id.in_(part_ids), StockLot.status == LotStatus.ACTIVE)
        .order_by(StockLot.id)
    )
    prefix = staging.staging_subtree_prefix(session)
    if prefix is not None:
        query = query.where(Location.id_path.not_like(f"{prefix}%"))
    return [
        _Candidate(
            lot_id=lot.id,
            part_id=lot.part_id,
            location_id=location.id,
            id_path=location.id_path,
            label_path=location.label_path,
            on_hand_milli=lot.qty_milli_cached,
            free_milli=max(0, reservations.available(lot)),
        )
        for lot, location in session.execute(query).all()
    ]


def _draw(
    candidates: list[_Candidate],
    free: dict[int, int],
    preference: dict[int, int],
    needed: int,
    #: `location_id -> id_path` for every bin the walk already stops at. A dict
    #: rather than a set of ids because (3) below needs the path to measure
    #: nearness, and looking it up again would be a second chance for the draw's
    #: idea of where a bin is to differ from the one `_stops` prints.
    visited: dict[int, str],
) -> list[tuple[_Candidate, int]]:
    """Choose lots for one line's outstanding need. **The selection rule.**

    Re-picked each round rather than sorted once, because which candidate is best
    depends on how much is still outstanding. In order:

    1. **the line's own part before any alternate**, alternates in
       `bom_line_substitutes.preference` order — the same order
       `shortage_for_build` accumulates availability in, so the walk cannot hand
       out a substitute the report never counted on;
    2. **a bin the walk already opens.** Taking more from a drawer the user is
       standing in front of costs nothing, so it beats every other consideration
       below — including avoiding a split. This is the "where it does not cost
       extra walking" half of the rule, and without it the plan will happily send
       someone to a fourth drawer to save one cut;
    3. **failing that, the bin nearest one the walk already opens** — nearest
       meaning "shares the most `id_path` ancestors with it", so another drawer of
       an open cabinet beats a drawer across the room. This is still the "extra
       walking" clause, and it is the reason a 1 000-piece lot in the far cabinet
       does **not** win the round below just for being an exact fit: cutting tape
       in a cabinet already open is cheaper than crossing the room to avoid the
       cut. Containment is the only locality this schema knows, and it is the
       strongest one available — the design's whole storage model is "things are
       inside other things";
    4. **an exact fit next.** A lot holding precisely what is left is taken whole:
       no split, and an emptied bin. This is the "prefer whole lots" half, and its
       position under (3) is what "where it does not cost extra walking" means;
    5. **otherwise the largest free quantity first**, which keeps the number of
       stops as low as any plan can, and means at most one lot per line is ever
       split — the last one drawn;
    6. **ties by `id_path` then lot id**, so the plan is deterministic and two
       otherwise equal candidates resolve towards the front of the walk.

    The deliberate limitation: this is greedy, not a knapsack, and nearness is
    containment, not metres — two cabinets side by side are as far apart as two
    cabinets in different rooms, because no column here says otherwise. Ordering
    *between* cabinets would need a floor plan; ordering *within* one is exact,
    and a pick list nobody can predict is worse than one that occasionally walks a
    little further.
    """
    remaining = needed
    taken: list[tuple[_Candidate, int]] = []
    while remaining > 0:
        usable = [
            candidate
            for candidate in candidates
            if candidate.part_id in preference and free[candidate.lot_id] > 0
        ]
        if not usable:
            break
        best = min(
            usable,
            key=lambda candidate: (
                preference[candidate.part_id],
                0 if candidate.location_id in visited else 1,
                -_nearness(candidate.id_path, visited),
                0 if free[candidate.lot_id] == remaining else 1,
                -free[candidate.lot_id],
                candidate.id_path,
                candidate.lot_id,
            ),
        )
        qty = min(remaining, free[best.lot_id])
        free[best.lot_id] -= qty
        remaining -= qty
        # Within one line's draw as well as between lines: the second bin this
        # line opens is a stop the third one may as well reuse.
        visited[best.location_id] = best.id_path
        taken.append((best, qty))
    return taken


def _nearness(id_path: str, visited: dict[int, str]) -> int:
    """Ancestors shared with the closest bin the walk already opens.

    Zero on the first round, when nothing is open yet — so the very first lot of a
    line is chosen on quantity alone, and only later rounds are pulled towards the
    cabinet already standing open.

    Counted in whole `id_path` segments rather than characters: `/1/` and `/12/`
    share a character and no ancestor at all, and a character count would rank the
    two as neighbours purely because of how their ids were numbered.
    """
    if not visited:
        return 0
    segments = _segments(id_path)
    return max(_shared(segments, _segments(other)) for other in visited.values())


def _segments(id_path: str) -> list[str]:
    return [segment for segment in id_path.split(PATH_SEP) if segment]


def _shared(left: list[str], right: list[str]) -> int:
    shared = 0
    for one, other in zip(left, right, strict=False):
        if one != other:
            break
        shared += 1
    return shared


# ---------------------------------------------------------------------------
# Pass 2: the order to walk it in
# ---------------------------------------------------------------------------


def _stops(session: Session, takes: list[PickTake]) -> tuple[PickStop, ...]:
    """Group by location and sort by `id_path`. **This is the feature.**

    `id_path` is `/1/4/12/`, so a plain string sort is a depth-first walk of the
    storage tree: every drawer of one cabinet lands together, and one cabinet is
    finished before the next is opened. Sorting by BOM line instead — the obvious
    thing, since that is the order the takes were computed in — is what turns one
    lap of the room into one lap per line.

    The honest limitation: the sort is lexicographic over numeric ids, so `/10/`
    precedes `/2/` and the order *between* cabinets is stable rather than
    physical. **Grouping is exact and grouping is what the walk needs** — no
    column in this schema knows which cabinet is nearer the door, and `sort_order`
    only orders siblings, so deriving a real floor order would mean a second path
    cache to keep in step for a gain nobody can measure.
    """
    if not takes:
        return ()
    places = _places(session, {take.lot_id for take in takes})
    short_ids = _short_ids(session, {place[0] for place in places.values()})

    grouped: dict[int, list[PickTake]] = {}
    for take in takes:
        grouped.setdefault(places[take.lot_id][0], []).append(take)

    return tuple(
        PickStop(
            location_id=location_id,
            label_path=label_path,
            id_path=id_path,
            short_id=short_ids.get(location_id),
            # Within one bin, by package and then by line: the lots are the
            # physical things being picked up, and a line number is a label on why.
            takes=tuple(
                sorted(
                    grouped[location_id],
                    key=lambda take: (
                        take.lot_id,
                        -1 if take.line_no is None else take.line_no,
                        take.part_id,
                    ),
                )
            ),
        )
        for location_id, id_path, label_path in sorted(
            {places[take.lot_id] for take in takes}, key=lambda place: (place[1], place[0])
        )
    )


def _places(session: Session, lot_ids: set[int]) -> dict[int, tuple[int, str, str]]:
    """Where each of these lots is: `lot_id -> (location id, id_path, label_path)`.

    One query, shared by the draw (which needs to know which bins the walk already
    opens) and by the grouping below (which needs the path to sort and to print).
    Two lookups would be two chances for the plan's idea of a lot's location to
    differ from the one it prints.
    """
    if not lot_ids:
        return {}
    return {
        lot_id: (location_id, id_path, label_path)
        for lot_id, location_id, id_path, label_path in session.execute(
            select(StockLot.id, Location.id, Location.id_path, Location.label_path)
            .join(Location, Location.id == StockLot.location_id)
            .where(StockLot.id.in_(lot_ids))
        ).all()
    }


def _short_ids(session: Session, location_ids: set[int]) -> dict[int, str]:
    """The printed code on each bin, where there is one.

    Most generated grid cells have none — the design deliberately does not put a
    label on all 96 drawers of a box — so a missing key here is the normal case
    and never an error.
    """
    if not location_ids:
        return {}
    rows = session.execute(
        select(ObjectId.entity_pk, ObjectId.short_id).where(
            ObjectId.entity_type == EntityType.LOCATION,
            ObjectId.entity_pk.in_(location_ids),
            ObjectId.is_primary.is_(True),
        )
    ).all()
    return {int(entity_pk): short_id for entity_pk, short_id in rows}


def _parts(session: Session, part_ids: set[int]) -> dict[int, Part]:
    if not part_ids:
        return {}
    rows = session.execute(select(Part).where(Part.id.in_(part_ids))).scalars()
    return {part.id: part for part in rows}
