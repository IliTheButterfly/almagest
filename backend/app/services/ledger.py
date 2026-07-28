"""The one write path into `stock_ledger`, and the only place a balance moves.

`stock_ledger` is the system's spine and it is append-only, enforced by database
triggers rather than by convention. Every function here exists to keep two facts
true together, in one transaction:

* the ledger row is appended, and
* `stock_lots.qty_milli_cached` moves by exactly that row's `delta_milli`.

**Nothing outside this module writes either one.** That is the whole point: a
second write path is how a cache and its source of truth drift apart, and drift
here does not mean a slow query, it means the numbers the UI has been showing
were wrong. `app.db.maintenance.check_lot_balance_drift` detects that nightly and
`rebuild_lot_balances` repairs it — but only because there is exactly one place
to audit when it fires.

Balances are **read** from `qty_milli_cached`, never by summing the ledger:
`SUM(delta_milli)` in an API path is how this design dies at 200k rows.

Two shapes are deliberate deviations worth stating plainly, because the obvious
alternative is wrong in a way that only shows up months later:

* **A whole-lot move rewrites `stock_lots.location_id`** and appends *one* row
  (`kind=move`, `delta_milli=0`, from and to set). Minting a new lot for every
  shelf change would destroy lot identity and per-lot cost continuity — the
  reel you paid 4.2 c a part for would become an anonymous new lot the first
  time it changed drawer.
* **Undo is a compensating row**, never an `UPDATE` and never a `DELETE`. The
  history says "this happened, then it was undone", which is not the same
  statement as "this never happened", and only one of those two is true.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import LedgerKind, LedgerSource, LotStatus
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location


class LedgerError(ValueError):
    """A refusal, decided before anything is written.

    `reason` is machine-readable so the API hands the UI something better than a
    sentence. Deliberately few: **a scan is never rejected**, so this is reserved
    for requests that cannot be *interpreted* — a move to where the lot already
    is, an undo of an undo — and never for stock going negative, which is a
    dashboard anomaly rather than a reason to refuse the write that recorded what
    actually happened.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Attribution:
    """Who caused a movement and how, plus the keys that tie its rows together.

    Bundled rather than threaded through as eight loose keyword arguments, so
    every operation below carries identical provenance instead of eight
    signatures drifting apart.
    """

    source: LedgerSource = LedgerSource.MANUAL
    #: Plain integer, no FK: multi-user is deferred. NULL means the owner.
    actor_id: int | None = None
    ref_type: str | None = None
    ref_id: int | None = None
    note: str | None = None
    #: Written to the **first** row an operation appends and only that one.
    #: `stock_ledger.client_op_id` is UNIQUE, which is the database's own
    #: backstop against a retry becoming a second movement; a multi-row
    #: operation cannot put the same key on every row, so the rest are tied
    #: together by `group_uuid` instead.
    client_op_id: str | None = None
    group_uuid: str | None = None


def new_group_uuid() -> str:
    """A fresh id tying the rows of one multi-row operation into one undoable unit."""
    return str(uuid.uuid4())


def _secondary(attribution: Attribution) -> Attribution:
    """The attribution for every row of an operation after the first."""
    return replace(attribution, client_op_id=None)


def _require_positive(value: int, field: str) -> None:
    if value <= 0:
        raise LedgerError(f"{field} must be greater than zero", reason="non_positive_quantity")


# ---------------------------------------------------------------------------
# The single writer
# ---------------------------------------------------------------------------


def post(
    session: Session,
    lot: StockLot,
    *,
    kind: LedgerKind,
    delta_milli: int,
    attribution: Attribution,
    from_location_id: int | None = None,
    to_location_id: int | None = None,
    counted_qty_milli: int | None = None,
    measured_mass_mg: int | None = None,
    unit_cost_micro: int | None = None,
    currency: str | None = None,
    reversal_of_seq: int | None = None,
) -> StockLedger:
    """Append one ledger row and move the cached balance with it.

    `qty_after_milli` is read off the balance *after* the mutation, so it equals
    the lot's balance immediately after this row by construction rather than by
    a caller remembering to compute it. That redundancy with the running sum is
    deliberate: it makes drift traceable to the row that broke it, instead of
    only visible in aggregate once the totals already disagree.
    """
    lot.qty_milli_cached += delta_milli
    row = StockLedger(
        lot_id=lot.id,
        part_id=lot.part_id,
        kind=kind,
        delta_milli=delta_milli,
        qty_after_milli=lot.qty_milli_cached,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
        counted_qty_milli=counted_qty_milli,
        measured_mass_mg=measured_mass_mg,
        unit_cost_micro=unit_cost_micro,
        currency=currency,
        ref_type=attribution.ref_type,
        ref_id=attribution.ref_id,
        group_uuid=attribution.group_uuid,
        actor_id=attribution.actor_id,
        source=attribution.source,
        reversal_of_seq=reversal_of_seq,
        client_op_id=attribution.client_op_id,
        note=attribution.note,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------


def find_or_create_lot(
    session: Session,
    *,
    part_id: int,
    location: Location,
    packaging_id: int | None = None,
    batch_code: str | None = None,
    serial: str | None = None,
    date_code: str | None = None,
    unit_cost_micro: int | None = None,
    currency: str | None = None,
    force_new: bool = False,
) -> tuple[StockLot, bool]:
    """The lot a receipt belongs to, or a new one. Returns `(lot, created)`.

    **Packaging-aware.** A 5000-piece reel and a cut-tape strip of the same MPN
    in the same bin are two lots, independently costed, so the match keys on
    packaging, batch, serial and date code as well as part and place. Merging on
    part-and-place alone would fold two physically distinct packages into one
    balance and lose per-batch cost — and there would then be no way to split
    them back apart, because the ledger cannot be edited.

    `force_new` is the escape hatch for "this is a separate package even though
    every field matches", which the user can see and the schema cannot.
    """
    if not force_new:
        # SQLAlchemy renders `column == None` as `IS NULL`, which is what makes
        # "no packaging recorded" match another lot with no packaging recorded
        # rather than matching nothing at all.
        existing = (
            session.execute(
                select(StockLot)
                .where(
                    StockLot.part_id == part_id,
                    StockLot.location_id == location.id,
                    StockLot.status == LotStatus.ACTIVE,
                    StockLot.packaging_id == packaging_id,
                    StockLot.batch_code == batch_code,
                    StockLot.serial == serial,
                    StockLot.date_code == date_code,
                )
                .order_by(StockLot.id)
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return existing, False

    lot = StockLot(
        part_id=part_id,
        location_id=location.id,
        packaging_id=packaging_id,
        batch_code=batch_code,
        serial=serial,
        date_code=date_code,
        unit_cost_micro=unit_cost_micro,
        currency=currency,
        status=LotStatus.ACTIVE,
        qty_milli_cached=0,
        # `locations.tare_mg` is authoritative; this is its cache, so it is
        # copied at the moment the lot lands somewhere rather than joined at
        # read time by the bench station.
        container_tare_mg=location.tare_mg,
    )
    session.add(lot)
    session.flush()
    return lot, True


# ---------------------------------------------------------------------------
# Movements
# ---------------------------------------------------------------------------


def receive(
    session: Session,
    lot: StockLot,
    qty_milli: int,
    *,
    attribution: Attribution,
    unit_cost_micro: int | None = None,
    currency: str | None = None,
) -> StockLedger:
    """Stock arriving. Sets `to_location_id`, which is not bookkeeping: it is the
    only signal `assignment.homing_score` has that this part has ever lived
    here, and a location that used to hold a part is still plausibly its home
    after the last one was used up."""
    _require_positive(qty_milli, "qty_milli")
    return post(
        session,
        lot,
        kind=LedgerKind.RECEIVE,
        delta_milli=qty_milli,
        to_location_id=lot.location_id,
        attribution=attribution,
        unit_cost_micro=unit_cost_micro,
        currency=currency,
    )


def consume(
    session: Session, lot: StockLot, qty_milli: int, *, attribution: Attribution
) -> StockLedger:
    """Stock used. Accepted even when it takes the balance negative — see
    `LedgerError`: refusing here would block the record of what physically
    happened in order to protect a number that is meant to raise an alarm."""
    _require_positive(qty_milli, "qty_milli")
    return post(
        session,
        lot,
        kind=LedgerKind.CONSUME,
        delta_milli=-qty_milli,
        from_location_id=lot.location_id,
        attribution=attribution,
    )


def return_to_stock(
    session: Session, lot: StockLot, qty_milli: int, *, attribution: Attribution
) -> StockLedger:
    """Stock coming back unused. A distinct kind from `receive` on purpose: a
    return is not a purchase, and conflating them would inflate every
    consumption and intake statistic derived from the ledger."""
    _require_positive(qty_milli, "qty_milli")
    return post(
        session,
        lot,
        kind=LedgerKind.RETURN,
        delta_milli=qty_milli,
        to_location_id=lot.location_id,
        attribution=attribution,
    )


def adjust(
    session: Session, lot: StockLot, delta_milli: int, *, attribution: Attribution
) -> StockLedger:
    """A signed correction with no physical movement behind it.

    Zero is refused: an adjustment of nothing is not something a user meant.
    (A *recount* of the same balance is different, and is allowed — see
    `recount`.)
    """
    if delta_milli == 0:
        raise LedgerError("an adjustment of zero records nothing", reason="zero_delta")
    return post(
        session,
        lot,
        kind=LedgerKind.ADJUST,
        delta_milli=delta_milli,
        attribution=attribution,
    )


def recount(
    session: Session,
    lot: StockLot,
    counted_qty_milli: int,
    *,
    attribution: Attribution,
    measured_mass_mg: int | None = None,
) -> StockLedger:
    """Set the balance to what was physically counted.

    The delta is derived, and `counted_qty_milli` is stored alongside it so a
    disputed count stays reconstructible: "the ledger said 500, I counted 480"
    is a different fact from "someone adjusted by −20", and only the first can
    be argued with later.

    A confirming recount — delta zero — still writes a row. "I counted it and it
    was right" is evidence, and discarding it would make a verified bin
    indistinguishable from one nobody has opened in a year.
    """
    if counted_qty_milli < 0:
        raise LedgerError("a physical count cannot be negative", reason="negative_count")
    return post(
        session,
        lot,
        kind=LedgerKind.COUNT,
        delta_milli=counted_qty_milli - lot.qty_milli_cached,
        counted_qty_milli=counted_qty_milli,
        measured_mass_mg=measured_mass_mg,
        attribution=attribution,
    )


def move_whole_lot(
    session: Session,
    lot: StockLot,
    to_location_id: int,
    *,
    attribution: Attribution,
    reversal_of_seq: int | None = None,
) -> StockLedger:
    """Relocate an entire lot: **one** row, `delta_milli = 0`, from and to set,
    and `stock_lots.location_id` rewritten.

    See the module docstring for why this is not a new lot. The same-location
    refusal is workflow 4's "same source and destination blocks commit": it is
    almost always a double scan of one label, and writing a no-op row would put
    a movement in the history that never happened.
    """
    if lot.location_id == to_location_id:
        raise LedgerError("source and destination are the same location", reason="same_location")
    destination = session.get(Location, to_location_id)
    if destination is None:
        raise LedgerError(f"no location with id {to_location_id}", reason="unknown_location")

    from_location_id = lot.location_id
    lot.location_id = to_location_id
    # `container_tare_mg` caches the *container's* tare, and the container is
    # the destination now — a lot in a different bin has a different tare, so a
    # stale copy here would corrupt the bench station's differential weighing.
    lot.container_tare_mg = destination.tare_mg

    return post(
        session,
        lot,
        kind=LedgerKind.MOVE,
        delta_milli=0,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
        attribution=attribution,
        reversal_of_seq=reversal_of_seq,
    )


def split_to_lot(
    session: Session,
    lot: StockLot,
    destination_lot: StockLot,
    qty_milli: int,
    *,
    attribution: Attribution,
) -> tuple[StockLedger, StockLedger]:
    """Move part of a lot: **two** rows sharing a `group_uuid`.

    `split_out` (−N) off the source and `split_in` (+N) onto the destination.
    The pair sums to zero, which is the property that makes a partial move
    provably conservative — it moves stock without creating or destroying any —
    and the shared `group_uuid` is what lets one undo reverse both halves.

    Both rows carry both locations, so either row alone still says where the
    stock went.
    """
    _require_positive(qty_milli, "qty_milli")
    if destination_lot.id == lot.id:
        raise LedgerError("a lot cannot be split into itself", reason="same_lot")

    group = attribution.group_uuid or new_group_uuid()
    primary = replace(attribution, group_uuid=group)
    out_row = post(
        session,
        lot,
        kind=LedgerKind.SPLIT_OUT,
        delta_milli=-qty_milli,
        from_location_id=lot.location_id,
        to_location_id=destination_lot.location_id,
        attribution=primary,
    )
    in_row = post(
        session,
        destination_lot,
        kind=LedgerKind.SPLIT_IN,
        delta_milli=qty_milli,
        from_location_id=lot.location_id,
        to_location_id=destination_lot.location_id,
        attribution=_secondary(primary),
    )
    return out_row, in_row


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def rows_of_group(session: Session, group_uuid: str) -> list[StockLedger]:
    return list(
        session.execute(
            select(StockLedger)
            .where(StockLedger.group_uuid == group_uuid)
            .order_by(StockLedger.seq)
        ).scalars()
    )


def reverse(
    session: Session, rows: Sequence[StockLedger], *, attribution: Attribution
) -> list[StockLedger]:
    """Undo one or more rows by appending compensating rows.

    Newest first, so a multi-row operation unwinds in the order it was applied
    rather than fighting itself halfway through.

    **A compensation keeps the original's `kind`.** Reversing a `consume` of 5
    writes `consume +5`, not `adjust +5`. That reads oddly in isolation and is
    the right answer anyway: per-kind aggregation is the whole reason `kind`
    exists, and an `adjust` compensation would leave "how much did I consume
    this month" overstated by the takes that were undone eight seconds later —
    permanently, since the ledger cannot be edited.

    Two refusals, both about not compounding a mistake:

    * a row that already has a compensation cannot be reversed twice, or a
      double-tapped undo button doubles the correction;
    * a compensation cannot itself be reversed. A redo is a fresh movement with
      its own reason, not the negation of a negation.
    """
    if not rows:
        raise LedgerError("nothing to reverse", reason="not_found")

    ordered = sorted(rows, key=lambda row: row.seq, reverse=True)
    for row in ordered:
        if row.reversal_of_seq is not None:
            raise LedgerError(
                f"seq {row.seq} is itself a compensating row; a redo is a fresh movement",
                reason="is_a_reversal",
            )

    seqs = [row.seq for row in ordered]
    already = (
        session.execute(select(StockLedger.seq).where(StockLedger.reversal_of_seq.in_(seqs)))
        .scalars()
        .all()
    )
    if already:
        raise LedgerError(f"already reversed by seq {sorted(already)}", reason="already_reversed")

    # One group for the compensations of a multi-row operation, so the undo is
    # itself one undoable unit in the history.
    group = attribution.group_uuid or (new_group_uuid() if len(ordered) > 1 else None)
    primary = replace(attribution, group_uuid=group)

    compensations: list[StockLedger] = []
    for index, row in enumerate(ordered):
        lot = session.get(StockLot, row.lot_id) if row.lot_id is not None else None
        if lot is None:
            raise LedgerError(f"seq {row.seq} has no lot to compensate", reason="no_lot")
        compensations.append(
            _compensate(session, row, lot, primary if index == 0 else _secondary(primary))
        )
    return compensations


def _compensate(
    session: Session, row: StockLedger, lot: StockLot, attribution: Attribution
) -> StockLedger:
    if LedgerKind(row.kind) == LedgerKind.MOVE:
        if row.from_location_id is None:
            raise LedgerError(
                f"seq {row.seq} records no source location to move back to", reason="no_source"
            )
        if lot.location_id != row.to_location_id:
            # The lot has moved on since. Sending it back to where this row
            # started would be a fresh, unrequested relocation of a lot that is
            # somewhere else now, dressed up as an undo.
            raise LedgerError(
                f"lot {lot.id} is no longer at the destination of seq {row.seq}",
                reason="moved_since",
            )
        return move_whole_lot(
            session,
            lot,
            row.from_location_id,
            attribution=attribution,
            reversal_of_seq=row.seq,
        )

    # From and to are swapped as well as the delta negated: undoing a receipt
    # *into* a bin is stock leaving that bin, and stating it the other way round
    # would teach `assignment.homing_score` that this is the part's home.
    return post(
        session,
        lot,
        kind=LedgerKind(row.kind),
        delta_milli=-row.delta_milli,
        from_location_id=row.to_location_id,
        to_location_id=row.from_location_id,
        counted_qty_milli=None,
        attribution=attribution,
        reversal_of_seq=row.seq,
    )
