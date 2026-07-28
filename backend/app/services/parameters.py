"""Writing parameter values.

Every numeric write goes through here, for one reason: **`value_min` and
`value_max` must always be populated**, including for a plain scalar, where
they are equal. Parametric search is an interval-overlap test
(`value_min <= hi AND value_max >= lo`), so a row that stores only
`value_nominal` is invisible to every range query — a 22 µF capacitor would
simply not appear in a search for 20–30 µF. That failure is silent, which is
what makes it worth funnelling all writes through one function.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import PROVENANCE_PRIORITY, Provenance, ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate, ParameterValue
from app.services.search.fts import refresh_param_digest
from app.services.search.value_parser import parse_for_template


class ChoiceNotFound(ValueError):
    """No `parameter_choice` matches the given key or alias."""


class LowerPrecedence(ValueError):
    """Refused: an existing value came from a more trusted source.

    Ordering is `manual > datasheet_table > mpn_decoder > distributor_freetext
    > llm_inferred`. A manufacturer's own printed table beats an API's
    marketing copy, and nothing beats a human.
    """


def set_numeric(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    raw_input: str,
    *,
    provenance: Provenance = Provenance.MANUAL,
    confidence: float | None = None,
) -> ParameterValue:
    """Parse and store a numeric parameter. Raises `ValueParseError` on bad input."""
    parsed = parse_for_template(raw_input, template)
    row = _existing_or_new(session, part, template, provenance)

    low, high = parsed.to_interval()

    row.raw_input = raw_input
    row.value_nominal = parsed.value_nominal
    row.value_min = low
    row.value_max = high
    row.tolerance_pct = parsed.tolerance_pct
    row.display_mantissa = parsed.display_mantissa
    row.display_si_prefix = parsed.display_si_prefix or None
    row.display_unit_symbol = parsed.display_unit_symbol
    row.choice_id = None
    row.value_text = None
    row.value_bool = None
    row.provenance = provenance
    row.confidence = confidence

    session.flush()
    # The FTS digest is derived from parameter_value, so it cannot be kept
    # current by the triggers on `parts`. Refreshing it here means the one
    # write path owns it too.
    refresh_param_digest(session, part.id)
    return row


def set_choice(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    key_or_alias: str,
    *,
    provenance: Provenance = Provenance.MANUAL,
    confidence: float | None = None,
) -> ParameterValue:
    """Store an enum facet.

    Enum facets live in the same table as numerics, via `choice_id`, so search,
    provenance and review all have one code path rather than three.
    """
    choice = resolve_choice(session, template, key_or_alias)
    row = _existing_or_new(session, part, template, provenance)

    row.raw_input = key_or_alias
    row.choice_id = choice.id
    row.value_nominal = None
    row.value_min = None
    row.value_max = None
    row.tolerance_pct = None
    row.display_mantissa = None
    row.display_si_prefix = None
    row.display_unit_symbol = None
    row.value_text = None
    row.value_bool = None
    row.provenance = provenance
    row.confidence = confidence

    session.flush()
    # The FTS digest is derived from parameter_value, so it cannot be kept
    # current by the triggers on `parts`. Refreshing it here means the one
    # write path owns it too.
    refresh_param_digest(session, part.id)
    return row


def resolve_choice(
    session: Session, template: ParameterTemplate, key_or_alias: str
) -> ParameterChoice:
    """Find a choice by its key or any of its aliases, case-insensitively.

    Aliases are what make dual notation work: `0603` and `1608` are the same
    package under two conventions, so both resolve to one row and **the user is
    never asked which convention a source used**.
    """
    needle = key_or_alias.strip().casefold()
    choices = list(
        session.execute(
            select(ParameterChoice).where(ParameterChoice.template_id == template.id)
        ).scalars()
    )

    for choice in choices:
        if choice.key.casefold() == needle:
            return choice
    for choice in choices:
        if needle in _aliases(choice):
            return choice

    known = ", ".join(sorted(choice.key for choice in choices))
    raise ChoiceNotFound(f"{key_or_alias!r} is not a choice of {template.name}; known: {known}")


def _aliases(choice: ParameterChoice) -> set[str]:
    if not choice.aliases_json:
        return set()
    loaded = json.loads(choice.aliases_json)
    return {str(alias).casefold() for alias in loaded}


def _existing_or_new(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    provenance: Provenance,
) -> ParameterValue:
    """Upsert on `(part_id, template_id)`, honouring source precedence.

    The unique constraint means there is at most one row to find, which is the
    same property that lets multi-predicate search use plain JOINs.
    """
    row = session.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id,
            ParameterValue.template_id == template.id,
        )
    ).scalar_one_or_none()

    if row is None:
        row = ParameterValue(part_id=part.id, template_id=template.id, raw_input="")
        session.add(row)
        return row

    incoming = PROVENANCE_PRIORITY.get(provenance, 0)
    established = PROVENANCE_PRIORITY.get(row.provenance, 0)
    if incoming < established:
        raise LowerPrecedence(
            f"{template.name} on part {part.id} already has a {row.provenance} value; "
            f"{provenance} does not override it"
        )
    return row


def value_type_of(template: ParameterTemplate) -> ValueType:
    return ValueType(template.value_type)
