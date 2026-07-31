"""Quantities this install defines itself — `/api/parameter-quantities`.

The shipped list is what the value grammar can read, and it is the right default:
a field measured in `ohms` or `µF` is refused at authoring time precisely because
the parser would never read a value under it. But "the parser has to know it" is
not the same as "a developer has to add it", and this is the door for the second
half of that.

Separate from `/api/parameter-fields` because it answers a different question and
has a different lifetime: a *field* is a thing on a category, a *quantity* is a
thing the whole install can measure in, and a dozen fields may share one. The
authoring UI reaches both from one screen, which is a UI decision, not a reason to
merge the routes.

What is not here: an edit. A quantity's `name` is what every field measured in it
stores and what every value of those fields was parsed under, and its window and
symbol are the terms that parse were done under too. Changing any of it after
values exist is a data migration wearing an edit's clothes — the same argument
`parameter_template`'s frozen columns make. Delete-and-recreate is available while
nothing uses it, and refused once something does.
"""

from __future__ import annotations

from elec_value_parser import is_builtin
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.db.session import get_db
from app.models.parameter import ParameterQuantity
from app.services import quantities as quantities_service
from app.services.search.value_parser import quantity_symbol, supported_quantities

router = APIRouter(prefix="/api/parameter-quantities", tags=["parameter-quantities"])


class QuantityCreate(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "What a numeric field's `base_unit` will hold — so it is what every "
            "value of every field using it is parsed under. It cannot be a name "
            "Almagest already ships, or another name for one: `resistance` is "
            "`ohm`, and redefining it would change what stored numbers mean."
        ),
    )
    symbol: str = Field(
        min_length=1,
        max_length=16,
        description=(
            "What a value is written and printed with — 'B', 'turns', 'px'. Checked "
            "*before* any SI prefix, so a one-letter symbol that collides with a "
            "prefix letter still reads as the unit: under a quantity whose symbol is "
            "'m', '10m' is ten of them and '10mm' is ten milli of them."
        ),
    )
    display_name: str = Field(min_length=1, max_length=64)
    symbol_aliases: list[str] = Field(
        default_factory=list,
        description="Case-sensitive alternative symbols. SI case carries meaning.",
    )
    word_aliases: list[str] = Field(
        default_factory=list,
        description="Case-insensitive spelled-out names — 'bytes', 'turns'.",
    )
    low: float | None = Field(
        default=None,
        description=(
            "Inclusive plausibility bound, null for unbounded. This is the "
            "quantity's universal window; a field may narrow it further with its own."
        ),
    )
    high: float | None = None
    allow_zero: bool = False
    allow_negative: bool = Field(
        default=False,
        description=(
            "Also switches the window to the **signed** reading, because that is the "
            "only one that means anything once negatives are allowed: [-40, 125] "
            "compared against magnitudes would accept -200."
        ),
    )
    allow_prefix: bool = Field(
        default=True,
        description=(
            "Whether '10k' means ten thousand of these. Turn it off for anything "
            "counted or written out in full — kilo-turns is not a thing, and leaving "
            "it on makes a stray 'k' silently mean a thousandfold."
        ),
    )


class QuantityRead(BaseModel):
    """One quantity a numeric field may be measured in.

    Shipped and custom ones are reported through the same shape so a picker can
    list them together, with `custom` as the only difference: a shipped quantity
    cannot be deleted, and neither can a custom one that fields are using.
    """

    id: int | None = None
    name: str
    symbol: str
    display_name: str
    custom: bool
    #: How many numeric fields are measured in it. Always 0 for a shipped one,
    #: which is not counted because it cannot be deleted either way.
    field_count: int = 0


class QuantityCreated(BaseModel):
    quantity: QuantityRead


class QuantityDeleted(BaseModel):
    quantity_id: int
    name: str


def _read(db: Session, row: ParameterQuantity) -> QuantityRead:
    return QuantityRead(
        id=row.id,
        name=row.name,
        symbol=row.symbol,
        display_name=row.display_name,
        custom=True,
        field_count=quantities_service.field_use_count(db, row),
    )


def _error(error: quantities_service.QuantityError) -> HTTPException:
    """409 for "something already there is in the way", 422 for "this definition
    cannot be written" — the same split `parameter_fields` makes."""
    conflicts = {"duplicate_quantity", "builtin_quantity", "quantity_in_use"}
    code = (
        status.HTTP_409_CONFLICT
        if error.reason in conflicts
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(code, detail={"reason": error.reason, "message": str(error)})


@router.get("", response_model=list[QuantityRead])
def list_parameter_quantities(db: Session = Depends(get_db)) -> list[QuantityRead]:
    """Every quantity a numeric field may name, shipped and custom together.

    Read through `supported_quantities()` rather than by listing the library's
    table here, so this and the field-authoring guard can never disagree about
    what is namable — and a custom quantity that failed to register in this
    process is absent from both rather than offered by one and refused by the
    other.
    """
    custom = {row.name: row for row in quantities_service.all_custom(db)}
    reported: list[QuantityRead] = []
    for name in supported_quantities():
        row = custom.get(name)
        if row is not None:
            reported.append(_read(db, row))
        else:
            reported.append(
                QuantityRead(
                    name=name,
                    symbol=quantity_symbol(name) or name,
                    display_name=name.replace("_", " "),
                    custom=False,
                )
            )
    return reported


@router.post("", response_model=QuantityCreated, status_code=status.HTTP_201_CREATED)
def create_parameter_quantity(
    request: QuantityCreate, db: Session = Depends(get_db)
) -> QuantityCreated:
    """Define a quantity.

    Registered with this process's parser inside the write, so the very next
    request can author a field against it — the field guard asks the parser, and a
    row that were stored but unregistered would be refused as `unknown_base_unit`
    by the process that had just created it.

    No idempotency key: `name` is UNIQUE and the duplicate is a clean 409 naming
    the existing quantity, which is a better answer to a doubled tap than a replay
    of a create — there is nothing here a second press could half-do.
    """
    try:
        row = quantities_service.create(
            db,
            quantities_service.QuantitySpec(
                name=request.name,
                symbol=request.symbol,
                display_name=request.display_name,
                symbol_aliases=tuple(request.symbol_aliases),
                word_aliases=tuple(request.word_aliases),
                low=request.low,
                high=request.high,
                allow_zero=request.allow_zero,
                allow_negative=request.allow_negative,
                allow_prefix=request.allow_prefix,
            ),
        )
    except quantities_service.QuantityError as error:
        db.rollback()
        raise _error(error) from error
    created = _read(db, row)
    db.commit()
    return QuantityCreated(quantity=created)


@router.delete("/{quantity_id}", response_model=QuantityDeleted)
def delete_parameter_quantity(quantity_id: RowId, db: Session = Depends(get_db)) -> QuantityDeleted:
    """Remove a definition, refused while any field is measured in it."""
    row = db.get(ParameterQuantity, quantity_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_parameter_quantity",
                "message": f"no custom quantity with id {quantity_id}",
            },
        )
    if is_builtin(row.name):  # pragma: no cover - create refuses this outright
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "builtin_quantity",
                "message": f"{row.name!r} is a shipped quantity",
            },
        )
    name = row.name
    try:
        quantities_service.delete(db, row)
    except quantities_service.QuantityError as error:
        db.rollback()
        raise _error(error) from error
    db.commit()
    return QuantityDeleted(quantity_id=quantity_id, name=name)
