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

from elec_value_parser import ParsedValue, ValueParseError, get_quantity
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
    if not template.base_unit:
        raise TemplateHasNoUnit(f"numeric template {template.name!r} has no base_unit")

    parsed = parse_value(raw_input, template.base_unit)
    _enforce_template_window(parsed, template)
    return parsed


def _enforce_template_window(parsed: ParsedValue, template: ParameterTemplate) -> None:
    if template.plausible_min is None and template.plausible_max is None:
        return

    # Check both ends of whatever was parsed, so a range that only partly
    # overlaps the window is still caught.
    low, high = parsed.to_interval()
    for value in (low, high, parsed.value_nominal):
        if value is None:
            continue
        if template.plausible_min is not None and value < template.plausible_min:
            raise _out_of_range(parsed, template, value)
        if template.plausible_max is not None and value > template.plausible_max:
            raise _out_of_range(parsed, template, value)


def _out_of_range(
    parsed: ParsedValue, template: ParameterTemplate, value: float
) -> ValueParseError:
    error = ValueParseError(
        f"{value:g} is outside the configured range for {template.name} "
        f"[{template.plausible_min}, {template.plausible_max}]",
        text=parsed.raw_input,
        unit=template.base_unit or "",
    )
    error.reason = "implausible"
    return error


def supported_quantity(base_unit: str | None) -> bool:
    """Whether the parser recognises a template's `base_unit`.

    Used to validate a template at creation time rather than discovering the
    problem on the first value someone tries to enter.
    """
    return bool(base_unit) and get_quantity(base_unit or "") is not None
