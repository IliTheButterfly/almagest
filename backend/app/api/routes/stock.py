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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import idempotency
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
