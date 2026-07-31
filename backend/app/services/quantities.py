"""Quantities this install defines itself, and how they reach the parser.

The shipped quantities live in `elec-value-parser` and cover the electrical ones
plus light, mass, length and a few ratios. This module is for the ones nobody
could have anticipated — bytes of flash, turns of wire, hours of runtime — so
that "I need to filter by something you did not think of" is a row rather than a
library edit.

**The table is the source of truth; the parser's registry is a per-process view
of it.** Every process that parses a value must call `load_into_parser` at
startup, and the API also calls it after each write so the process serving the
next request already knows. That arrangement is deliberate and the failure mode
is the reason: a stored quantity that some process never registered raises
`UnknownQuantityError` there, loudly, rather than being read under a different
definition. There is no fallback, because a value parsed under the wrong quantity
is stored, indexed and searched as a number that means something else.

Two rules the guards below exist to keep:

* **A custom quantity can never take a name the library answers to**, alias
  included. Every `parameter_value` already in the database was computed under the
  shipped definition of its quantity, so redefining `farad` locally would change
  what those numbers mean without touching a single row.
* **A definition that cannot parse its own unit is refused at authoring time.**
  That is the same rule `unknown_base_unit` enforces for the shipped list, and it
  is enforced the same way — by asking the parser, not by pattern-matching the
  symbol. A quantity whose symbol the grammar cannot read would take values
  forever and match nothing, silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from elec_value_parser import (
    Quantity,
    QuantityShadowsBuiltin,
    ValueParseError,
    is_builtin,
    parse,
    register_quantity,
    unregister_quantity,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.parameter import ParameterQuantity, ParameterTemplate
from app.services.search.value_parser import forget_quantity_cache


class QuantityError(ValueError):
    """A custom quantity could not be written, with a code the UI routes on."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class QuantitySpec:
    """One custom quantity as the API takes it, before it is a row."""

    name: str
    symbol: str
    display_name: str
    symbol_aliases: tuple[str, ...] = ()
    word_aliases: tuple[str, ...] = ()
    low: float | None = None
    high: float | None = None
    allow_zero: bool = False
    allow_negative: bool = False
    allow_prefix: bool = True


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def aliases_of(raw: str | None) -> tuple[str, ...]:
    """Decode an alias column. **The one decoder**, like `choice_aliases` is for
    a choice's — two readings of the same JSON is two answers to "is this a
    spelling of that unit"."""
    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in decoded if str(item).strip())


def to_library_quantity(row: ParameterQuantity) -> Quantity:
    """The row as the parser's own dataclass.

    `bounds_on_magnitude` is **derived rather than stored**: it is only ever
    interesting when negatives are allowed, and then the signed reading is the
    only one that makes sense. A window of [-40, 125] compared against
    magnitudes would accept -200 (since |−200| ≥ −40 is vacuous), which is not
    what anybody drawing that window meant.
    """
    return Quantity(
        name=row.name,
        symbol=row.symbol,
        # The symbol is always an accepted spelling of itself, and it is checked
        # before any prefix split — that is what lets a one-letter symbol survive
        # colliding with a prefix letter.
        symbol_aliases=frozenset({row.symbol, *aliases_of(row.symbol_aliases_json)}),
        word_aliases=frozenset(aliases_of(row.word_aliases_json)),
        low=0.0 if row.low is None else row.low,
        high=float("inf") if row.high is None else row.high,
        allow_zero=row.allow_zero,
        allow_negative=row.allow_negative,
        allow_prefix=row.allow_prefix,
        bounds_on_magnitude=not row.allow_negative,
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def load_into_parser(session: Session) -> tuple[str, ...]:
    """Register every stored custom quantity with this process's parser.

    Called at application startup and after every write. Returns the names
    registered, so a caller can log what a process actually knows — the one
    question worth being able to answer when a value fails to parse in one place
    and succeeds in another.
    """
    rows = session.execute(select(ParameterQuantity).order_by(ParameterQuantity.name)).scalars()
    names: list[str] = []
    for row in rows:
        register_quantity(to_library_quantity(row))
        names.append(row.name)
    forget_quantity_cache()
    return tuple(names)


def forget(name: str) -> None:
    """Drop one from this process's parser registry, after deleting its row."""
    unregister_quantity(name)
    forget_quantity_cache()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def all_custom(session: Session) -> list[ParameterQuantity]:
    return list(
        session.execute(select(ParameterQuantity).order_by(ParameterQuantity.name)).scalars()
    )


def find_by_name(session: Session, name: str) -> ParameterQuantity | None:
    return session.execute(
        select(ParameterQuantity).where(func.lower(ParameterQuantity.name) == name.strip().lower())
    ).scalar_one_or_none()


def field_use_count(session: Session, row: ParameterQuantity) -> int:
    """How many numeric fields name this quantity. What a delete is refused with."""
    return int(
        session.execute(
            select(func.count())
            .select_from(ParameterTemplate)
            .where(ParameterTemplate.base_unit == row.name)
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create(session: Session, spec: QuantitySpec) -> ParameterQuantity:
    """Define a quantity, guards first, and register it before returning.

    Registering inside the write is what makes the very next request able to
    author a field against it: the field-authoring guard asks the parser whether
    the quantity exists, and a definition that were stored but unregistered would
    be refused as `unknown_base_unit` by the process that had just created it.
    """
    name = spec.name.strip()
    symbol = spec.symbol.strip()
    display_name = spec.display_name.strip() or name

    if not name:
        raise QuantityError("a quantity needs a name", reason="missing_name")
    if not symbol:
        raise QuantityError(
            "a quantity needs a unit symbol — it is what a value is written and printed with",
            reason="missing_symbol",
        )
    if is_builtin(name):
        raise QuantityError(
            f"{name!r} is one of the quantities Almagest ships with, or another name for one, "
            "so it cannot be redefined here. Every value already stored was parsed under the "
            "shipped definition; redefining it would change what those numbers mean without "
            "touching them. Use it as it is, or pick a different name.",
            reason="builtin_quantity",
        )
    if find_by_name(session, name) is not None:
        raise QuantityError(
            f"a quantity named {name!r} already exists", reason="duplicate_quantity"
        )

    low, high = spec.low, spec.high
    if low is not None and high is not None and low > high:
        raise QuantityError(
            f"the plausible window [{low}, {high}] is inverted, and no value can fall in it",
            reason="inverted_plausibility",
        )

    row = ParameterQuantity(
        name=name,
        symbol=symbol,
        display_name=display_name,
        symbol_aliases_json=_encode(spec.symbol_aliases),
        word_aliases_json=_encode(spec.word_aliases),
        low=low,
        high=high,
        allow_zero=spec.allow_zero,
        allow_negative=spec.allow_negative,
        allow_prefix=spec.allow_prefix,
    )
    _refuse_unparseable(row)

    session.add(row)
    session.flush()
    register_quantity(to_library_quantity(row))
    forget_quantity_cache()
    return row


def delete(session: Session, row: ParameterQuantity) -> None:
    """Remove a definition, refused while any field is measured in it.

    Not a cascade and not a soft delete: a field whose `base_unit` named a
    quantity that had gone would refuse every value from then on, and its stored
    values would have no defined meaning. The refusal names the count so the user
    knows what to change first.
    """
    used = field_use_count(session, row)
    if used:
        raise QuantityError(
            f"{used} field{'s' if used != 1 else ''} measure{'' if used != 1 else 's'} something "
            f"in {row.name}. Deleting it would leave those fields unable to read any value, and "
            "their stored numbers with no unit. Change or remove those fields first.",
            reason="quantity_in_use",
        )
    name = row.name
    session.delete(row)
    session.flush()
    forget(name)


def _encode(values: tuple[str, ...]) -> str | None:
    cleaned = [value.strip() for value in values if value.strip()]
    return json.dumps(cleaned) if cleaned else None


def _refuse_unparseable(row: ParameterQuantity) -> None:
    """Refuse a definition the grammar cannot read a value under.

    Asked by *parsing*, not by inspecting the symbol, for the same reason
    `base_unit` is validated through the parser rather than a regex: the
    authoritative answer is the one the search path will give. `1` plus the symbol
    is the minimal probe — if that does not read, nothing a user types ever will.

    Note the probe deliberately avoids the plausibility window by using the low
    bound where one is set: a quantity whose values start at 1000 would otherwise
    refuse its own probe and look unparseable.
    """
    quantity = to_library_quantity(row)
    probe_value = 1.0
    if row.low is not None and row.low > probe_value:
        probe_value = row.low
    if row.high is not None and row.high < probe_value:
        probe_value = row.high
    probe = f"{probe_value:g}{row.symbol}"
    try:
        parse(probe, quantity)
    except ValueParseError as error:
        raise QuantityError(
            f"a value cannot be written in this unit: {probe!r} does not read as "
            f"{row.name} ({error}). The symbol is what a value is typed with, so a symbol the "
            "grammar cannot read would make every value of every field using it unfilterable.",
            reason="unparseable_symbol",
        ) from error
    except QuantityShadowsBuiltin as error:  # pragma: no cover - guarded above
        raise QuantityError(str(error), reason="builtin_quantity") from error
