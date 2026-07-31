"""Adapter between `parameter_template` and the `elec-value-parser` library.

The library is deliberately standalone and knows nothing about this schema. All
this module does is supply it the missing context — **which physical quantity
is being parsed** — and then apply the template's own plausibility window on
top of the library's.

Two independent guards on the same class of mistake is not redundancy. A unit
misread is the most expensive error the system can make, and the two guards
fail differently: the library's window is per-quantity and universal, while the
template's is per-field and can be tightened for a specific use (a decoupling
capacitor template can refuse anything above 100 µF without claiming that
larger capacitors do not exist).
"""

from __future__ import annotations

import functools

from elec_value_parser import ParsedValue, ValueParseError, get_quantity, known_quantities
from elec_value_parser import parse as parse_value

from app.models.enums import ValueType
from app.models.parameter import ParameterTemplate


class TemplateNotNumeric(ValueError):
    """Asked to parse a value against a template that is not numeric."""


class TemplateHasNoUnit(ValueError):
    """A numeric template with no `base_unit` cannot be parsed against.

    Not a defensive check — it is a configuration error that would otherwise
    surface as a confusing parse failure on perfectly good input.
    """


def parse_for_template(raw_input: str, template: ParameterTemplate) -> ParsedValue:
    """Parse `raw_input` as a value of `template`'s quantity.

    Raises a `ValueParseError` subclass on failure. Callers route on
    `error.reason` — in this system a failure is a **review-queue item**, not an
    exception to swallow.
    """
    if template.value_type != ValueType.NUMERIC:
        raise TemplateNotNumeric(
            f"template {template.name!r} is {template.value_type}, not numeric"
        )
    return parse_in_window(
        raw_input,
        base_unit=template.base_unit,
        plausible_min=template.plausible_min,
        plausible_max=template.plausible_max,
        label=template.name,
    )


def parse_in_window(
    raw_input: str,
    *,
    base_unit: str | None,
    plausible_min: float | None = None,
    plausible_max: float | None = None,
    label: str,
) -> ParsedValue:
    """`parse_for_template` without the ORM row — the same two guards, by value.

    Exists because `app.services.requirements` reads prose against a **snapshot**
    of the template table rather than against live rows: the deterministic
    requirement parser trial-parses one token against every numeric template
    looking for the one quantity that reads it, and doing that through detached
    ORM instances would be a session-lifetime hazard for no gain.

    It is a parameter split, deliberately **not** a second implementation. The
    per-field plausibility window is the second of the two independent guards
    against a unit misread, and a copy of it that drifted from this one would
    disarm exactly the guard whose whole value is being redundant.
    """
    if not base_unit:
        raise TemplateHasNoUnit(f"numeric template {label!r} has no base_unit")

    parsed = parse_value(raw_input, base_unit)
    _enforce_window(parsed, label=label, base_unit=base_unit, low=plausible_min, high=plausible_max)
    return parsed


def _enforce_window(
    parsed: ParsedValue, *, label: str, base_unit: str, low: float | None, high: float | None
) -> None:
    if low is None and high is None:
        return

    # Check both ends of whatever was parsed, so a range that only partly
    # overlaps the window is still caught.
    interval_low, interval_high = parsed.to_interval()
    for value in (interval_low, interval_high, parsed.value_nominal):
        if value is None:
            continue
        if low is not None and value < low:
            raise _out_of_range(parsed, label, base_unit, value, low, high)
        if high is not None and value > high:
            raise _out_of_range(parsed, label, base_unit, value, low, high)


def _out_of_range(
    parsed: ParsedValue,
    label: str,
    base_unit: str,
    value: float,
    low: float | None,
    high: float | None,
) -> ValueParseError:
    error = ValueParseError(
        f"{value:g} is outside the configured range for {label} [{low}, {high}]",
        text=parsed.raw_input,
        unit=base_unit,
    )
    error.reason = "implausible"
    return error


def supported_quantity(base_unit: str | None) -> bool:
    """Whether the parser recognises a template's `base_unit`.

    Used to validate a template at creation time rather than discovering the
    problem on the first value someone tries to enter.
    """
    return bool(base_unit) and get_quantity(base_unit or "") is not None


def canonical_quantity(base_unit: str) -> str | None:
    """The registry's own spelling of a `base_unit`, or None if it knows none.

    Authoring stores this rather than what the user typed, so `OHM`, `Ohm` and
    the quantity alias `resistance` all end up as the one string `ohm` that
    `parse_value` is given. Two rows spelling the same quantity differently would
    parse identically and *look* different in every facet panel and every export.
    """
    quantity = get_quantity(base_unit)
    return None if quantity is None else quantity.name


def quantity_symbol(base_unit: str) -> str | None:
    """The unit symbol a quantity prints with — 'Ω' for ohm, 'F' for farad.

    So the authoring UI can label a unit picker the way the part detail screen
    labels a value, rather than making the user match 'farad' to 'F' themselves.
    """
    quantity = get_quantity(base_unit)
    return None if quantity is None else quantity.symbol


def supported_quantities() -> tuple[str, ...]:
    """Every quantity a numeric template's `base_unit` may name.

    Read off the library rather than listed here, so a quantity added there
    becomes pickable in the field authoring UI without a second edit — the whole
    reason authoring offers a **select** instead of a free-text box. `µF` and
    `ohms` are not in here, and that is the point: both are refused at authoring
    time rather than on the first value somebody tries to enter.

    Asked of the library on **every call**, not captured at import: an install may
    define quantities of its own (`app.services.quantities`), and those are
    registered while this module is already loaded. A snapshot taken at import
    would leave every custom quantity out of the picker and out of the sweep
    below — which would not fail, it would just quietly never match.
    """
    return tuple(sorted(known_quantities()))


def forget_quantity_cache() -> None:
    """Drop the memoised sweep, because the set of quantities just changed.

    `reads_as_a_quantity` is memoised on the text alone — which is right, since
    for a fixed registry the answer is a property of the grammar. Registering a
    custom quantity changes the registry, so every cached "nothing reads this"
    becomes a maybe. Not clearing it would make a newly defined unit work for
    strings nobody had asked about yet and fail for the ones they had.
    """
    reads_as_a_quantity.cache_clear()


@functools.lru_cache(maxsize=4096)
def reads_as_a_quantity(text: str) -> str | None:
    """The first known quantity that reads this whole string as a value, or `None`.

    **The one definition of "this text is not an electrical value at all"**, and
    therefore of "this text might be a part number". Lives here, with the rest of
    the value-grammar adapter, because it is a question about the grammar alone —
    no template, no schema, no session — and because two callers now depend on
    exactly the same answer:

    * `app.services.bom_import._mpn_candidates`, deciding whether a BOM's `Value`
      cell may be used as an MPN lookup key;
    * `app.services.requirements`, deciding whether a token in a prose
      description is a part number rather than a value.

    Both are the same gate against the same mistake, and a second, subtly
    different copy of it would let one of them match `10k` against a catalogue
    part genuinely named `10K` while the other refused.

    Note it deliberately asks about **success under any quantity**, not about a
    refusal. `implausible` (`1M` under farads) and `unit_mismatch` (`100nH` under
    farads) mean the text *was* read as a quantity and rejected — a bad value,
    not a part number — and the sweep catches those through whichever quantity
    accepts them instead. It separates the two populations cleanly: `10k`, `4k7`,
    `100nF`, `0R22`, `1M`, `22p`, `16MHz` and a bare `0603` all read as a value
    under something, while `LM358N`, `74HC595`, `1N4148`, `STM32F103C8T6` and
    `RC0603FR-0710KL` read as a value under nothing.

    Cached because both callers ask repeatedly about the same handful of strings —
    a BOM repeats its values (a hundred `100nF` lines is one decoupling net) and
    `_mpn_candidates` runs twice per line.
    """
    for quantity in supported_quantities():
        try:
            parse_value(text, quantity)
        except ValueParseError:
            continue
        return quantity
    return None
