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
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import PROVENANCE_PRIORITY, Provenance, ValueType
from app.models.parameter import (
    ParameterChoice,
    ParameterTemplate,
    ParameterValue,
    ParameterValueChoice,
)
from app.services.search.fts import refresh_param_digest
from app.services.search.value_parser import parse_for_template


class TooManyChoices(ValueError):
    """Several options were given for a field that holds one per part.

    Its own error rather than a silent truncation: a caller passing two options to a
    single-valued field has misunderstood the field, and keeping the first would
    look exactly like success.
    """


class ChoiceNotFound(ValueError):
    """No `parameter_choice` matches the given key or alias."""


class UnboundedValue(ValueError):
    """Refused: the parsed value has only one bound, so it cannot be stored.

    A one-sided limit — `>=50V`, `<100nF` — is a perfectly good *query* and the
    grammar parses it deliberately, but it is not a value a part *has*. Stored,
    it leaves `value_min` or `value_max` NULL, and because search is
    `value_min <= hi AND value_max >= lo` the row then matches **no** range
    query at all: a part accepted as `>=50V` disappears from a search for
    40–100 V while still looking, in every listing, like a part with a voltage
    rating.

    Refusing here rather than at each caller is the point of this module. The
    check is written against the interval, not against `ParsedValue.kind`, so a
    future grammar addition that yields a half-open interval is refused the day
    it lands instead of the day someone notices a part missing from a search.
    """


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
    """Parse and store a numeric parameter.

    Raises `ValueParseError` on bad input and `UnboundedValue` when the input
    parses to a one-sided limit, which is the invariant this module exists for:
    both bounds populated, or no row at all.
    """
    parsed = parse_for_template(raw_input, template)

    low, high = parsed.to_interval()
    if low is None or high is None:
        # Checked before `_existing_or_new`, so a refusal never leaves a blank
        # `ParameterValue` pending in the session for the caller's next flush to
        # write out.
        missing = "lower" if low is None else "upper"
        raise UnboundedValue(
            f"{raw_input!r} is a one-sided limit, so it has no {missing} bound to store for "
            f"{template.name}; parametric search is an interval-overlap test and a null-bounded "
            "row matches no range query at all. Give a value or a range."
        )

    row = _existing_or_new(session, part, template, provenance)

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


def set_text(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    text: str,
    *,
    provenance: Provenance = Provenance.MANUAL,
    confidence: float | None = None,
) -> ParameterValue:
    """Store a free-text parameter — a marking, a note, a package code.

    Written for the same reason `set_bool` below is: **nothing in this codebase
    ever wrote `value_text`**. The column and the `text` value type have existed
    since the first migration, the field form offers the type, and no writer could
    put anything in it — so a text field was declarable and permanently empty.

    Text matching is substring only, which the authoring form says out loud. The
    value is stored verbatim in both `raw_input` and `value_text`: what was typed
    *is* the value, and there is no parse to be lossless about.
    """
    row = _existing_or_new(session, part, template, provenance)

    row.raw_input = text
    row.value_text = text
    row.value_nominal = None
    row.value_min = None
    row.value_max = None
    row.value_typ = None
    row.tolerance_pct = None
    row.display_mantissa = None
    row.display_si_prefix = None
    row.display_unit_symbol = None
    row.choice_id = None
    row.value_bool = None
    row.provenance = provenance
    row.confidence = confidence

    session.flush()
    _replace_choice_set(session, row, ())
    session.flush()
    refresh_param_digest(session, part.id)
    return row


def set_bool(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    value: bool,
    *,
    provenance: Provenance = Provenance.MANUAL,
    confidence: float | None = None,
) -> ParameterValue:
    """Store a yes/no parameter — automotive grade, RoHS.

    `raw_input` is 'yes'/'no' rather than 'True'/'False': it is the lossless record
    of what a human said, and it is what a re-parse and every export read.
    """
    row = _existing_or_new(session, part, template, provenance)

    row.raw_input = "yes" if value else "no"
    row.value_bool = value
    row.value_nominal = None
    row.value_min = None
    row.value_max = None
    row.value_typ = None
    row.tolerance_pct = None
    row.display_mantissa = None
    row.display_si_prefix = None
    row.display_unit_symbol = None
    row.choice_id = None
    row.value_text = None
    row.provenance = provenance
    row.confidence = confidence

    session.flush()
    _replace_choice_set(session, row, ())
    session.flush()
    refresh_param_digest(session, part.id)
    return row


def clear_value(session: Session, part: Part, template: ParameterTemplate) -> bool:
    """Remove a part's value for one field. True if there was one.

    A delete rather than a null row: `parameter_value` exists to say "this part has
    this attribute", and a row with every value column null is a part that claims an
    attribute it has no answer for — invisible to a range query, present in a
    populated-count, and impossible to tell from a bug.

    The child choice rows go with it by `ON DELETE CASCADE`, which is safe in the
    one direction that matters: deleting *this part's* options is not deleting the
    options themselves.
    """
    row = session.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id, ParameterValue.template_id == template.id
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    session.delete(row)
    session.flush()
    refresh_param_digest(session, part.id)
    return True


def value_for(session: Session, part: Part, template: ParameterTemplate) -> ParameterValue | None:
    """This part's value for one field, or None."""
    return session.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id, ParameterValue.template_id == template.id
        )
    ).scalar_one_or_none()


def set_choice(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    key_or_alias: str,
    *,
    provenance: Provenance = Provenance.MANUAL,
    confidence: float | None = None,
) -> ParameterValue:
    """Store an enum facet: exactly this one option, replacing whatever was there.

    Enum facets live in the same table as numerics, via `choice_id`, so search,
    provenance and review all have one code path rather than three.

    Valid for a multi-valued field too — "this field now holds exactly this one
    option" is a legitimate thing to say about a set — which is why every existing
    caller, the MPN decoders and the enrichment promoter among them, needs no
    change. `set_choices` is for saying more than one.
    """
    return set_choices(
        session,
        part,
        template,
        [key_or_alias],
        provenance=provenance,
        confidence=confidence,
    )


def set_choices(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    keys_or_aliases: Sequence[str],
    *,
    provenance: Provenance = Provenance.MANUAL,
    confidence: float | None = None,
) -> ParameterValue:
    """Store the **complete set** of options a part holds for an enum field.

    The whole set, never a delta: an attribute that has become "SMD only" has to be
    sayable, and a caller that could only add would have no way to say it.

    Two invariants this function exists to hold, and both are why every enum write
    funnels through here:

    * **`parameter_value_choice` is written for every enum value**, single- or
      multi-valued, so `EXISTS` is the one predicate search needs and facet counts
      have one source. A row whose options lived only in `choice_id` would be
      invisible to both.
    * **`choice_id` is the single-valued answer, or null.** It mirrors the set only
      when the set has one member; with several it is null, so a consumer reading it
      gets "no single answer" rather than one option out of three presented as the
      whole truth.

    Refuses more than one option on a field that has not been declared
    `allow_multiple`, rather than quietly keeping the first: a caller passing two
    options to a single-valued field has misunderstood the field, and storing one of
    them would look like it worked.
    """
    if not keys_or_aliases:
        raise ChoiceNotFound(f"no options given for {template.name}")
    choices = [resolve_choice(session, template, token) for token in keys_or_aliases]
    # Dedup, keeping the order given: the same option named twice — once by key and
    # once by an alias — is one option, not a conflict.
    unique: list[ParameterChoice] = []
    for choice in choices:
        if all(choice.id != seen.id for seen in unique):
            unique.append(choice)

    if len(unique) > 1 and not template.allow_multiple:
        raise TooManyChoices(
            f"{template.name} holds one option per part, but {len(unique)} were given "
            f"({', '.join(choice.key for choice in unique)}). Turn on 'more than one at once' "
            "for the field, or pass a single option."
        )

    row = _existing_or_new(session, part, template, provenance)

    row.raw_input = ", ".join(keys_or_aliases)
    # Null for a set of several — see the docstring: half an answer read as a whole
    # one is worse than no answer.
    row.choice_id = unique[0].id if len(unique) == 1 else None
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
    _replace_choice_set(session, row, unique)

    session.flush()
    # The FTS digest is derived from parameter_value, so it cannot be kept
    # current by the triggers on `parts`. Refreshing it here means the one
    # write path owns it too.
    refresh_param_digest(session, part.id)
    return row


def _replace_choice_set(
    session: Session, row: ParameterValue, choices: Sequence[ParameterChoice]
) -> None:
    """Make the child rows exactly `choices`.

    Deletes what is no longer held and inserts what is newly held, rather than
    clearing and re-inserting the lot: the FK is `RESTRICT`, so deleting a row that
    is about to be re-inserted is a needless brush with the guard that protects
    options parts are filed under.
    """
    wanted = {choice.id for choice in choices}
    existing = set(
        session.execute(
            select(ParameterValueChoice.choice_id).where(ParameterValueChoice.value_id == row.id)
        )
        .scalars()
        .all()
    )
    for choice_id in existing - wanted:
        session.execute(
            delete(ParameterValueChoice).where(
                ParameterValueChoice.value_id == row.id,
                ParameterValueChoice.choice_id == choice_id,
            )
        )
    for choice_id in wanted - existing:
        session.add(ParameterValueChoice(value_id=row.id, choice_id=choice_id))


def choices_held(session: Session, row: ParameterValue) -> list[ParameterChoice]:
    """Every option a value holds, in the field's own display order.

    Read from the child table rather than from `choice_id`, because that is where
    the whole set lives; for a single-valued field the two agree by construction.
    """
    return list(
        session.execute(
            select(ParameterChoice)
            .join(ParameterValueChoice, ParameterValueChoice.choice_id == ParameterChoice.id)
            .where(ParameterValueChoice.value_id == row.id)
            .order_by(ParameterChoice.sort_order, ParameterChoice.key)
        ).scalars()
    )


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


def choice_aliases(choice: ParameterChoice) -> tuple[str, ...]:
    """A choice's alternative spellings, verbatim and in the stored order.

    **The one decoder of `aliases_json`.** Three callers need it — this module's
    `resolve_choice`, `enrichment.extract.target_fields` (which puts every
    spelling in the model's schema so a datasheet saying `1608` is not forced to
    say `0603`), and `requirements.vocabulary` (which matches them against words
    in a prose description). All three must agree about what an alias *is*, since
    a spelling one of them accepts and another does not is a token that resolves
    when typed into search and fails when read out of a description.

    Order-preserving and un-normalised: the callers that want a lookup key
    casefold it themselves, and the one that shows spellings to a model wants
    them as the curator wrote them.
    """
    if not choice.aliases_json:
        return ()
    loaded = json.loads(choice.aliases_json)
    if not isinstance(loaded, list):
        return ()
    return tuple(str(alias) for alias in loaded)


def _aliases(choice: ParameterChoice) -> set[str]:
    return {alias.casefold() for alias in choice_aliases(choice)}


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
