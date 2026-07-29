"""Stock movement endpoints — workflows 1, 3 and 4.

Every handler here has the same three-part shape, and the order matters:

1. resolve and validate the request into objects, touching nothing;
2. hand a closure to :func:`app.api.idempotency.run`, which owns the single
   ``commit()``;
3. render the resulting lot from ``qty_milli_cached``.

Step 2 is why no handler calls ``session.commit()`` itself. The ledger row and
the cached balance have to land in one transaction or the cache silently
disagrees with the append-only record it is derived from, and the only way to
guarantee that for *every* route is for none of them to own the commit.

`ledger.py` is the sole writer, and it is deliberately not re-exported field by
field: a route that assembled a `StockLedger` itself would be a second write
path, which is exactly the thing the module docstring there forbids.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.batch import LineRefused
from app.api.limits import CountMilli, DeltaMilli, MassMg, MoneyMicro, QtyMilli, RowId
from app.api.schemas import LotRead, ReplayableResponse, lot_read
from app.db.session import get_db
from app.models.catalog import Part
from app.models.enums import LedgerSource, LotStatus
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location
from app.services import ledger
from app.services.ledger import Attribution, LedgerError

router = APIRouter(prefix="/api/stock", tags=["stock"])


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class MovementRequest(BaseModel):
    """Fields every movement carries.

    `client_op_id` is optional but strongly encouraged: without it the caller has
    accepted at-least-once semantics, which on an append-only ledger means a
    retried take can only be corrected by writing a third row.
    """

    client_op_id: str | None = Field(
        default=None,
        description=(
            "Client-generated idempotency key, attached at scan time. A retry or a "
            "double scan carrying the same key resolves to the same ledger row."
        ),
    )
    device_id: str | None = None
    source: LedgerSource = LedgerSource.MANUAL
    note: str | None = None


class MovementResponse(ReplayableResponse):
    #: The ledger `seq` values this request appended, in the order written. A
    #: partial move appends two; an undo of a multi-row operation appends one per
    #: reversed row.
    seqs: list[int]
    group_uuid: str | None = None
    lot: LotRead
    #: The other side of a split, so the UI can show both balances without a
    #: second round trip.
    counterpart_lot: LotRead | None = None


class ReceiveRequest(MovementRequest):
    part_id: RowId
    location_id: RowId
    qty_milli: QtyMilli
    packaging_id: RowId | None = None
    batch_code: str | None = None
    serial: str | None = None
    date_code: str | None = None
    unit_cost_micro: MoneyMicro | None = None
    currency: str | None = None
    #: "This is a separate package even though every field matches" — something
    #: the user can see and the schema cannot.
    force_new_lot: bool = False


class QuantityRequest(MovementRequest):
    qty_milli: QtyMilli


class AdjustRequest(MovementRequest):
    #: Signed. Zero is refused — an adjustment of nothing is not something a user
    #: meant, and it would put a movement in the history that never happened.
    delta_milli: DeltaMilli


class RecountRequest(MovementRequest):
    counted_qty_milli: CountMilli
    measured_mass_mg: MassMg | None = None


class MoveRequest(MovementRequest):
    to_location_id: RowId
    #: Omitted moves the whole lot in one row. Supplied splits it: two rows
    #: sharing a group_uuid, summing to zero.
    qty_milli: QtyMilli | None = None


class EmptyBinRequest(MovementRequest):
    to_location_id: RowId


class EmptyBinResponse(ReplayableResponse):
    moved_lot_ids: list[int]
    #: One entry per lot that could not be moved, with a machine-readable reason.
    #: Workflow 4: a bulk empty commits the rest and reports just the failures,
    #: because abandoning nineteen good moves over one bad lot is worse than
    #: reporting the one.
    failures: list[LotFailure]
    group_uuid: str


class LotFailure(BaseModel):
    lot_id: RowId
    reason: str
    message: str


class MovementDirection(StrEnum):
    """Which way one cart line moves stock.

    Two values, not four: a cart checkout to plain stock is ADR 0007's "pick a
    container, scan it and say how many parts you took or put back", and
    `adjust`/`recount` are corrections rather than things you did with your
    hands. Those stay one-at-a-time on their own routes, where the single lot in
    front of you is the whole subject of the request.
    """

    TAKE = "take"
    RETURN = "return"


class MovementLine(BaseModel):
    """One line of a batch movement.

    Names its stock **either** by `lot_id` — what a cart captured when the part
    was added — **or** by `part_id` inside `location_id`, which is what a client
    has when the user picked a container and then chose parts from search. The
    first is exact and can go stale; the second is resolved against the container
    now, so it cannot.
    """

    #: Echoed back verbatim on this line's result. The cart's own row id, so a
    #: client marks the row that failed rather than counting positions — see
    #: `MovementLineResult.index` for why both exist.
    client_line_id: str | None = Field(default=None, max_length=64)
    lot_id: RowId | None = None
    #: Resolved inside `location_id`, which must then be given. For a `return`
    #: with no matching lot in that container, one is created — putting parts
    #: back into a bin that has none of them yet is an ordinary thing to do. A
    #: `take` refuses instead: there is nothing there to take.
    part_id: RowId | None = None
    direction: MovementDirection
    qty_milli: QtyMilli
    #: This line's own idempotency key. Separate from the batch's, because a
    #: batch fails per line and so a retry of it is partial — see
    #: `app.api.idempotency.replay_line`.
    client_op_id: str | None = Field(default=None, max_length=36)
    note: str | None = None

    @model_validator(mode="after")
    def _names_exactly_one_thing(self) -> MovementLine:
        if (self.lot_id is None) == (self.part_id is None):
            raise ValueError("give exactly one of lot_id or part_id")
        return self


class BatchMovementRequest(MovementRequest):
    """A cart's worth of takes and returns, in one request (ADR 0007).

    The bound is the cart's: five hundred lines is already past what anybody
    gathers by hand, and it keeps the stored idempotency response a sane size.
    """

    #: The container the user scanned. Required to resolve a `part_id` line, and
    #: **asserted** against a `lot_id` line: a cart holding a lot that has since
    #: been moved to another bin fails that line rather than quietly taking stock
    #: from wherever it went. That is the staleness ADR 0007 says must fail the
    #: line and not the batch.
    location_id: RowId | None = None
    lines: list[MovementLine] = Field(min_length=1, max_length=500)


class MovementLineResult(ReplayableResponse):
    """What became of one line.

    Carries `index` **and** `client_line_id` because they answer different
    questions: the index is always present and unambiguous, and the client line
    id is what a cart row is keyed by locally. A client that only had the index
    would have to trust that it did not reorder the cart between building the
    request and rendering the answer.
    """

    index: int
    client_line_id: str | None
    applied: bool
    #: Machine-readable, and null exactly when `applied`. Same vocabulary as
    #: `LedgerError.reason` and `LotFailure.reason`, extended with the resolution
    #: failures a batch can have and a single-lot route cannot.
    reason: str | None = None
    message: str | None = None
    lot_id: int | None = None
    seq: int | None = None
    #: The lot's balance after this line, read from `qty_milli_cached` — the
    #: number every screen reads. A whole `LotRead` per line would make a
    #: five-hundred-line stored response enormous for data the client refetches
    #: anyway.
    qty_milli_after: int | None = None


class BatchMovementResponse(ReplayableResponse):
    applied_count: int
    failed_count: int
    #: Shared by every row this checkout wrote, so `POST /api/stock/undo` with
    #: `group_uuid_to_undo` reverses the whole cart in one tap. Present even when
    #: nothing applied, so a client can record it without branching.
    group_uuid: str
    results: list[MovementLineResult]


class UndoRequest(MovementRequest):
    """Undo by whichever handle the caller has.

    Exactly one must be given. `client_op_id_to_undo` is the one the UI actually
    uses — it already generated that key at scan time, so the eight-second undo
    button needs to remember nothing else.
    """

    seq: RowId | None = None
    group_uuid_to_undo: str | None = None
    client_op_id_to_undo: str | None = None


class UndoResponse(ReplayableResponse):
    #: The compensating rows. Never deletions — the history says "this happened,
    #: then it was undone", which is not the same statement as "this never
    #: happened".
    seqs: list[int]
    reversed_seqs: list[int]
    lots: list[LotRead]


EmptyBinResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _attribution(request: MovementRequest, **overrides: object) -> Attribution:
    return Attribution(
        source=request.source,
        note=request.note,
        client_op_id=request.client_op_id,
        **overrides,  # type: ignore[arg-type]
    )


def _require_lot(db: Session, lot_id: RowId) -> StockLot:
    lot = db.get(StockLot, lot_id)
    if lot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no stock lot with id {lot_id}")
    return lot


def _require_location(db: Session, location_id: RowId) -> Location:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no location with id {location_id}")
    return location


def _ledger_error(error: LedgerError) -> HTTPException:
    """Map a refusal onto 409, carrying the reason code.

    409 rather than 400: the request is well-formed and would have been valid
    against different state — a move to where the lot already is, an undo of an
    already-undone row. The UI needs to say *which*, not "bad request".
    """
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"reason": error.reason, "message": str(error)},
    )


# ---------------------------------------------------------------------------
# Movements
# ---------------------------------------------------------------------------


@router.post("/receive", response_model=MovementResponse)
def receive_stock(request: ReceiveRequest, db: Session = Depends(get_db)) -> MovementResponse:
    """Stock arriving — the commit step of intake.

    Nothing before this touches the ledger: workflow 1 runs scanning, enrichment,
    review and dimensions entirely against draft state, so an abandoned intake
    leaves no movement behind.
    """
    if db.get(Part, request.part_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no part with id {request.part_id}")
    location = _require_location(db, request.location_id)

    def work() -> MovementResponse:
        lot, _ = ledger.find_or_create_lot(
            db,
            part_id=request.part_id,
            location=location,
            packaging_id=request.packaging_id,
            batch_code=request.batch_code,
            serial=request.serial,
            date_code=request.date_code,
            unit_cost_micro=request.unit_cost_micro,
            currency=request.currency,
            force_new=request.force_new_lot,
        )
        try:
            row = ledger.receive(
                db,
                lot,
                request.qty_milli,
                attribution=_attribution(request),
                unit_cost_micro=request.unit_cost_micro,
                currency=request.currency,
            )
        except LedgerError as error:
            raise _ledger_error(error) from error
        return MovementResponse(seqs=[row.seq], lot=lot_read(db, lot))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="stock.receive",
        payload=request,
        response_model=MovementResponse,
        work=work,
    )


def _simple_movement(
    db: Session,
    lot_id: RowId,
    request: MovementRequest,
    endpoint: str,
    apply: object,
) -> MovementResponse:
    """Shared body for the one-row movements.

    Factored out because the only thing that differs between take, return, adjust
    and recount is which `ledger` function is called — and four near-identical
    handlers is four places for the idempotency wiring to be got subtly wrong.
    """
    lot = _require_lot(db, lot_id)

    def work() -> MovementResponse:
        try:
            row = apply(lot)  # type: ignore[operator]
        except LedgerError as error:
            raise _ledger_error(error) from error
        return MovementResponse(seqs=[row.seq], lot=lot_read(db, lot))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint=endpoint,
        payload=request,
        response_model=MovementResponse,
        work=work,
    )


@router.post("/lots/{lot_id}/consume", response_model=MovementResponse)
def consume_stock(
    lot_id: RowId, request: QuantityRequest, db: Session = Depends(get_db)
) -> MovementResponse:
    """Take stock. Accepted even when it drives the balance negative — that is a
    dashboard anomaly to investigate, not a reason to refuse the record of what
    physically happened."""
    return _simple_movement(
        db,
        lot_id,
        request,
        "stock.consume",
        lambda lot: ledger.consume(db, lot, request.qty_milli, attribution=_attribution(request)),
    )


@router.post("/lots/{lot_id}/return", response_model=MovementResponse)
def return_stock(
    lot_id: RowId, request: QuantityRequest, db: Session = Depends(get_db)
) -> MovementResponse:
    """Unused stock coming back. A distinct kind from a receipt: a return is not
    a purchase, and conflating them inflates every intake statistic."""
    return _simple_movement(
        db,
        lot_id,
        request,
        "stock.return",
        lambda lot: ledger.return_to_stock(
            db, lot, request.qty_milli, attribution=_attribution(request)
        ),
    )


@router.post("/lots/{lot_id}/adjust", response_model=MovementResponse)
def adjust_stock(
    lot_id: RowId, request: AdjustRequest, db: Session = Depends(get_db)
) -> MovementResponse:
    return _simple_movement(
        db,
        lot_id,
        request,
        "stock.adjust",
        lambda lot: ledger.adjust(db, lot, request.delta_milli, attribution=_attribution(request)),
    )


@router.post("/lots/{lot_id}/recount", response_model=MovementResponse)
def recount_stock(
    lot_id: RowId, request: RecountRequest, db: Session = Depends(get_db)
) -> MovementResponse:
    """Set the balance to what was physically counted.

    A confirming recount still writes a row: "I counted it and it was right" is
    evidence, and discarding it would make a verified bin indistinguishable from
    one nobody has opened in a year.
    """
    return _simple_movement(
        db,
        lot_id,
        request,
        "stock.recount",
        lambda lot: ledger.recount(
            db,
            lot,
            request.counted_qty_milli,
            attribution=_attribution(request),
            measured_mass_mg=request.measured_mass_mg,
        ),
    )


@router.post("/lots/{lot_id}/move", response_model=MovementResponse)
def move_stock(
    lot_id: RowId, request: MoveRequest, db: Session = Depends(get_db)
) -> MovementResponse:
    """Relocate a lot, whole or in part.

    Whole: one row, `delta_milli=0`, and `stock_lots.location_id` rewritten —
    the lot keeps its identity and its per-lot cost. Partial: two rows sharing a
    `group_uuid` that sum to zero, which is what makes the move provably
    conservative.
    """
    lot = _require_lot(db, lot_id)
    destination = _require_location(db, request.to_location_id)

    def work() -> MovementResponse:
        try:
            if request.qty_milli is None:
                row = ledger.move_whole_lot(
                    db, lot, destination.id, attribution=_attribution(request)
                )
                return MovementResponse(seqs=[row.seq], lot=lot_read(db, lot))

            if lot.location_id == destination.id:
                raise LedgerError(
                    "source and destination are the same location", reason="same_location"
                )
            group = ledger.new_group_uuid()
            target, _ = ledger.find_or_create_lot(
                db,
                part_id=lot.part_id,
                location=destination,
                packaging_id=lot.packaging_id,
                batch_code=lot.batch_code,
                serial=lot.serial,
                date_code=lot.date_code,
                unit_cost_micro=lot.unit_cost_micro,
                currency=lot.currency,
            )
            out_row, in_row = ledger.split_to_lot(
                db,
                lot,
                target,
                request.qty_milli,
                attribution=_attribution(request, group_uuid=group),
            )
        except LedgerError as error:
            raise _ledger_error(error) from error

        return MovementResponse(
            seqs=[out_row.seq, in_row.seq],
            group_uuid=group,
            lot=lot_read(db, lot),
            counterpart_lot=lot_read(db, target),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="stock.move",
        payload=request,
        response_model=MovementResponse,
        work=work,
    )


@router.post("/locations/{location_id}/empty", response_model=EmptyBinResponse)
def empty_bin(
    location_id: RowId, request: EmptyBinRequest, db: Session = Depends(get_db)
) -> EmptyBinResponse:
    """Move every lot out of one location into another.

    **One lot failing commits the rest and reports just that failure.** Workflow
    4 is explicit about this, and it is the right call for a physical task: the
    user has already tipped nineteen bags into the new bin, so refusing the whole
    batch over the twentieth would leave the database describing a world that no
    longer exists.
    """
    source = _require_location(db, location_id)
    destination = _require_location(db, request.to_location_id)
    if source.id == destination.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "same_location",
                "message": "source and destination are the same location",
            },
        )

    def work() -> EmptyBinResponse:
        group = ledger.new_group_uuid()
        lots = list(
            db.execute(
                select(StockLot).where(StockLot.location_id == source.id).order_by(StockLot.id)
            ).scalars()
        )

        moved: list[int] = []
        failures: list[LotFailure] = []
        for index, lot in enumerate(lots):
            # A quarantined lot is not swept along with the rest. Quarantine
            # exists precisely to stop a lot being used or relocated without
            # someone deciding about it, and a bulk operation is the easiest way
            # for that decision to get skipped by accident.
            if lot.status == LotStatus.QUARANTINED:
                failures.append(
                    LotFailure(
                        lot_id=lot.id,
                        reason="quarantined",
                        message="quarantined lots must be released or moved individually",
                    )
                )
                continue

            attribution = _attribution(
                request if index == 0 else request.model_copy(update={"client_op_id": None}),
                group_uuid=group,
            )
            try:
                # A SAVEPOINT per lot, so one refusal rolls back only its own
                # partial work and the rest of the batch still commits.
                with db.begin_nested():
                    ledger.move_whole_lot(db, lot, destination.id, attribution=attribution)
                moved.append(lot.id)
            except LedgerError as error:
                failures.append(LotFailure(lot_id=lot.id, reason=error.reason, message=str(error)))

        return EmptyBinResponse(moved_lot_ids=moved, failures=failures, group_uuid=group)

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="stock.empty",
        payload=request,
        response_model=EmptyBinResponse,
        work=work,
    )


# ---------------------------------------------------------------------------
# Batch movements — the cart's plain-stock checkout (ADR 0007)
# ---------------------------------------------------------------------------

#: The endpoint name a *line's* idempotency record is filed under. Distinct from
#: the batch's `stock.movements`, so reusing a line key as a batch key (or the
#: reverse) is caught as `request_mismatch` rather than replaying the wrong
#: shape.
_MOVEMENT_LINE_ENDPOINT = "stock.movement_line"


@router.post("/movements", response_model=BatchMovementResponse)
def batch_movements(
    request: BatchMovementRequest, db: Session = Depends(get_db)
) -> BatchMovementResponse:
    """Take or put back several parts in one request — a cart checkout to plain
    stock, with no project involved (ADR 0007).

    **One line failing applies the rest and reports just that failure**, the
    same rule `empty_bin` follows and for a sharper version of the same reason:
    a cart is gathered over minutes at a shelf, so by checkout time a line's lot
    may have been moved, emptied or deleted by somebody else. Rolling the batch
    back would discard nineteen correct statements about what the user physically
    did in order to protect the twentieth, and 4xx-ing it would leave the client
    unable to say which row to fix.

    Every row still goes through `app.services.ledger`, one `SAVEPOINT` per
    line, all under the single `commit()` `idempotency.run` owns. Both keys
    matter: the batch key makes a whole resubmission a no-op, and the per-line
    keys make a *partial* resubmission one — see `idempotency.replay_line`.
    """
    location = None if request.location_id is None else _require_location(db, request.location_id)

    def work() -> BatchMovementResponse:
        group = ledger.new_group_uuid()
        seen_keys: set[str] = set()
        results = [
            _apply_movement_line(db, request, line, index, location, group, seen_keys)
            for index, line in enumerate(request.lines)
        ]
        applied = sum(1 for result in results if result.applied)
        return BatchMovementResponse(
            applied_count=applied,
            failed_count=len(results) - applied,
            group_uuid=group,
            results=results,
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="stock.movements",
        payload=request,
        response_model=BatchMovementResponse,
        work=work,
    )


def _apply_movement_line(
    db: Session,
    request: BatchMovementRequest,
    line: MovementLine,
    index: int,
    location: Location | None,
    group: str,
    seen_keys: set[str],
) -> MovementLineResult:
    """One line, never raising.

    No SAVEPOINT, deliberately: every refusal is decided *before* anything is
    mutated — `_resolve_line_lot` only reads, and `ledger.consume` /
    `ledger.return_to_stock` validate before they post — so a refused line has
    nothing to roll back. It could not use one anyway: under pysqlite, releasing
    the outermost SAVEPOINT commits, which would split the enclosing `run`'s
    single transaction instead of protecting it.
    """

    def refused(reason: str, message: str) -> MovementLineResult:
        return MovementLineResult(
            index=index,
            client_line_id=line.client_line_id,
            applied=False,
            reason=reason,
            message=message,
        )

    if line.client_op_id is not None:
        if line.client_op_id in seen_keys:
            # Two lines of one cart cannot share a key: `stock_ledger.client_op_id`
            # is UNIQUE, so the second would fail on flush anyway — reported here
            # with a reason a client can act on instead.
            return refused(
                "duplicate_client_op_id",
                f"client_op_id {line.client_op_id} appears on more than one line",
            )
        seen_keys.add(line.client_op_id)

    try:
        stored = idempotency.replay_line(
            db,
            client_op_id=line.client_op_id,
            endpoint=_MOVEMENT_LINE_ENDPOINT,
            payload=line,
            response_model=MovementLineResult,
        )
    except idempotency.LineIdempotencyError as error:
        return refused(error.reason, str(error))
    if stored is not None:
        # The position may differ from the run that recorded it — the same cart
        # resubmitted with the failed rows removed is the normal retry.
        return stored.model_copy(update={"index": index})

    if line.client_op_id is not None and _ledger_holds_key(db, line.client_op_id):
        # `stock_ledger.client_op_id` is UNIQUE, and some *other* route may have
        # written this key — the station mints one per container, the intake queue
        # one per scan. Checked rather than left to the insert, because the
        # `IntegrityError` would poison the session and lose the lines that did
        # apply along with it.
        return refused(
            "duplicate_client_op_id",
            f"client_op_id {line.client_op_id} has already recorded a movement",
        )

    try:
        lot = _resolve_line_lot(db, line, location)
        attribution = Attribution(
            source=request.source,
            note=line.note if line.note is not None else request.note,
            client_op_id=line.client_op_id,
            group_uuid=group,
        )
        if line.direction is MovementDirection.TAKE:
            row = ledger.consume(db, lot, line.qty_milli, attribution=attribution)
        else:
            row = ledger.return_to_stock(db, lot, line.qty_milli, attribution=attribution)
    except LineRefused as error:
        return refused(error.reason, str(error))
    except LedgerError as error:
        return refused(error.reason, str(error))

    result = MovementLineResult(
        index=index,
        client_line_id=line.client_line_id,
        applied=True,
        lot_id=lot.id,
        seq=row.seq,
        qty_milli_after=lot.qty_milli_cached,
    )
    idempotency.record_line(
        db,
        client_op_id=line.client_op_id,
        device_id=request.device_id,
        endpoint=_MOVEMENT_LINE_ENDPOINT,
        payload=line,
        result=result,
    )
    return result


def _ledger_holds_key(db: Session, client_op_id: str) -> bool:
    """Whether any ledger row already carries this key."""
    row = db.execute(
        select(StockLedger.seq).where(StockLedger.client_op_id == client_op_id).limit(1)
    ).first()
    return row is not None


def _resolve_line_lot(db: Session, line: MovementLine, location: Location | None) -> StockLot:
    """The lot this line moves, or a refusal naming why it could not be found.

    Every path out of here is a *readable* refusal rather than an exception: a
    cart line whose part or lot has been deleted since it was added is the case
    ADR 0007 requires to degrade to a removable row, not to a 500.
    """
    if line.lot_id is not None:
        lot = db.get(StockLot, line.lot_id)
        if lot is None:
            raise LineRefused(f"no stock lot with id {line.lot_id}", reason="unknown_lot")
        if location is not None and lot.location_id != location.id:
            # The staleness the cart is built to survive: it captured this lot in
            # the container the user is holding, and it is somewhere else now.
            raise LineRefused(
                f"lot {lot.id} is no longer in location {location.id}", reason="lot_moved"
            )
        return lot

    assert line.part_id is not None  # `MovementLine` validates exactly one is set
    if location is None:
        raise LineRefused(
            "a line naming a part needs location_id — which container are these in?",
            reason="no_container",
        )
    if db.get(Part, line.part_id) is None:
        raise LineRefused(f"no part with id {line.part_id}", reason="unknown_part")

    if line.direction is MovementDirection.RETURN:
        # Putting parts back into a bin that holds none of them yet is ordinary,
        # so a return may create the lot. `find_or_create_lot` is packaging-aware
        # and returns the existing plain lot when there is one.
        lot, _ = ledger.find_or_create_lot(db, part_id=line.part_id, location=location)
        return lot

    candidates = list(
        db.execute(
            select(StockLot)
            .where(
                StockLot.part_id == line.part_id,
                StockLot.location_id == location.id,
                StockLot.status == LotStatus.ACTIVE,
            )
            .order_by(StockLot.id)
        ).scalars()
    )
    if not candidates:
        raise LineRefused(
            f"location {location.id} holds no active lot of part {line.part_id}",
            reason="no_lot_for_part",
        )
    if len(candidates) > 1:
        # Two packages of one MPN in one bin — a reel and a cut strip. Which one
        # was taken from is a fact only the user has, and guessing would file the
        # take against the wrong per-lot cost.
        raise LineRefused(
            f"location {location.id} holds {len(candidates)} lots of part {line.part_id};"
            " name one with lot_id",
            reason="ambiguous_lot",
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


@router.post("/undo", response_model=UndoResponse)
def undo_movement(request: UndoRequest, db: Session = Depends(get_db)) -> UndoResponse:
    """Undo by appending compensating rows. Never by deleting.

    The eight-second undo button posts here with the `client_op_id` it already
    generated at scan time, so it needs to remember nothing else about what it
    did.
    """
    handles = [request.seq, request.group_uuid_to_undo, request.client_op_id_to_undo]
    if sum(handle is not None for handle in handles) != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "ambiguous_handle",
                "message": "give exactly one of seq, group_uuid_to_undo, client_op_id_to_undo",
            },
        )

    rows = _rows_to_undo(db, request)
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "message": "no ledger rows match that handle"},
        )

    def work() -> UndoResponse:
        try:
            compensations = ledger.reverse(db, rows, attribution=_attribution(request))
        except LedgerError as error:
            raise _ledger_error(error) from error

        touched: dict[int, StockLot] = {}
        for row in compensations:
            if row.lot_id is not None and row.lot_id not in touched:
                lot = db.get(StockLot, row.lot_id)
                if lot is not None:
                    touched[row.lot_id] = lot

        return UndoResponse(
            seqs=[row.seq for row in compensations],
            reversed_seqs=[row.seq for row in rows],
            lots=[lot_read(db, lot) for lot in touched.values()],
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="stock.undo",
        payload=request,
        response_model=UndoResponse,
        work=work,
    )


def _rows_to_undo(db: Session, request: UndoRequest) -> list[StockLedger]:
    """Resolve an undo handle to the rows it names.

    A `client_op_id` identifies the *first* row an operation wrote; if that row
    belongs to a group, the whole group is undone together. That is what makes
    one tap undo both halves of a partial move rather than leaving stock
    duplicated across two bins.
    """
    if request.seq is not None:
        row = db.get(StockLedger, request.seq)
        return [row] if row is not None else []

    if request.group_uuid_to_undo is not None:
        return ledger.rows_of_group(db, request.group_uuid_to_undo)

    first = db.execute(
        select(StockLedger).where(StockLedger.client_op_id == request.client_op_id_to_undo)
    ).scalar_one_or_none()
    if first is None:
        return []
    if first.group_uuid is not None:
        return ledger.rows_of_group(db, first.group_uuid)
    return [first]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/lots/{lot_id}", response_model=LotRead)
def read_lot(lot_id: RowId, db: Session = Depends(get_db)) -> LotRead:
    return lot_read(db, _require_lot(db, lot_id))


class LedgerEntry(BaseModel):
    seq: RowId
    ts: str
    kind: str
    delta_milli: int
    qty_after_milli: int
    from_location_id: int | None
    to_location_id: int | None
    counted_qty_milli: int | None
    group_uuid: str | None
    reversal_of_seq: int | None
    source: str
    note: str | None


@router.get("/lots/{lot_id}/history", response_model=list[LedgerEntry])
def read_lot_history(
    lot_id: RowId, limit: int = 100, db: Session = Depends(get_db)
) -> list[LedgerEntry]:
    """The lot's movements, newest first.

    Reads the ledger directly, which is fine *here* — this endpoint is the
    history, so the rows are the answer rather than an expensive way to compute
    a balance.
    """
    _require_lot(db, lot_id)
    rows = db.execute(
        select(StockLedger)
        .where(StockLedger.lot_id == lot_id)
        .order_by(StockLedger.seq.desc())
        .limit(min(limit, 500))
    ).scalars()
    return [
        LedgerEntry(
            seq=row.seq,
            ts=row.ts.isoformat(),
            kind=row.kind,
            delta_milli=row.delta_milli,
            qty_after_milli=row.qty_after_milli,
            from_location_id=row.from_location_id,
            to_location_id=row.to_location_id,
            counted_qty_milli=row.counted_qty_milli,
            group_uuid=row.group_uuid,
            reversal_of_seq=row.reversal_of_seq,
            source=row.source,
            note=row.note,
        )
        for row in rows
    ]
