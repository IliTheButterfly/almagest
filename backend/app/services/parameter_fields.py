"""Authoring filterable fields — the write half of `parameter_template`.

Until now every `parameter_template` row came from a migration or the demo seed
script, so there was no way to say "capacitors also have an ESR" without editing
Python. This module is the one place that mints and edits them, and it exists
separately from the route because all four of its guards are **invariants of the
search engine**, not input validation:

* **`base_unit` is validated against the parser the search path uses.** A
  numeric template whose `base_unit` the parser does not recognise accepts no
  value at all, so the field would exist, appear in the filter panel, and never
  match anything. Checked with `supported_quantity`, which was written for
  exactly this moment, and stored canonicalised so `OHM` and `ohm` cannot become
  two spellings of one quantity.
* **`value_type` cannot change once values exist.** The columns a value lives in
  are chosen by the type (`value_min`/`value_max` for numeric, `choice_id` for
  enum), so flipping the type strands every stored row in columns the executor
  no longer reads. That is a data migration, not an edit.
* **`base_unit` cannot change once values exist**, for the sharper version of the
  same reason: the bounds were computed under the old quantity, so they stay in
  the table looking authoritative while answering every range query in the wrong
  unit. Nothing surfaces that — it is the silent failure the whole
  `services/parameters.py` funnel exists to prevent, arrived at from the other
  end.
* **A choice in use cannot be deleted.** `parameter_value.choice_id` is
  `ON DELETE RESTRICT`, so the database already refuses — but it refuses as a
  bare `IntegrityError`, which reaches the client as a 500. Counting first turns
  it into "6 parts use this".

Nothing here writes `parameter_value`. Values go through
`app.services.parameters` and only through it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import PartCategory
from app.models.enums import SubstitutionDirection, ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate, ParameterValue
from app.services.search.value_parser import (
    canonical_quantity,
    supported_quantities,
    supported_quantity,
)
from app.services.tree import category_tree


class AuthoringError(ValueError):
    """A field definition that cannot be written, with a reason code.

    Every refusal in this module carries one, so the UI can say what to do about
    it rather than "invalid input" — and so a test can assert the *reason* rather
    than a message it would then have to keep in step with the wording.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


#: What a seed's identity is made of. These three are what every decoder,
#: extractor and saved search names; the rest of a seed is freely editable.
SEED_FROZEN_FIELDS: tuple[str, ...] = ("name", "value_type", "base_unit")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def applicable_category_slugs(session: Session, category: PartCategory) -> set[str]:
    """The slugs a template may name and still apply to `category`.

    The category itself plus **every ancestor** — a field authored on "Capacitors"
    has to be offered under "Capacitors > Ceramic", because that is the node parts
    are actually filed under. This is the reverse direction from
    `TreeRepository.subtree`: there we want the descendants of a node, here we
    want the ancestors, and both come out of the same cached `id_path` with no
    recursion.
    """
    return {category.slug} | {
        ancestor.slug for ancestor in category_tree(session).ancestors(category)
    }


def templates_for_category(session: Session, category_slug: str | None) -> list[ParameterTemplate]:
    """Every field offered under a category — its own, its ancestors', and the global ones.

    **The fix for a real defect.** The previous test was
    `template.applies_to_category in (None, request.category)`: an exact string
    match, so a field authored on "Capacitors" silently vanished from
    "Capacitors > Ceramic". Silently is the operative word — the user sees a
    filter panel missing a field they just created, with nothing to explain it.

    A template naming **no** category still applies everywhere, which is
    deliberate rather than a leftover: `package` and `mounting_type` are things
    every part has, and filtering them out per category would empty most panels
    of most of what you actually want to filter on.
    """
    templates = list(
        session.execute(
            select(ParameterTemplate).order_by(ParameterTemplate.sort_order, ParameterTemplate.name)
        ).scalars()
    )
    if category_slug is None:
        return templates

    category = require_category(session, category_slug)
    applicable = applicable_category_slugs(session, category)
    return [
        template
        for template in templates
        if template.applies_to_category is None or template.applies_to_category in applicable
    ]


def require_category(session: Session, slug: str) -> PartCategory:
    category = session.execute(
        select(PartCategory).where(PartCategory.slug == slug)
    ).scalar_one_or_none()
    if category is None:
        raise AuthoringError(f"no part category with slug {slug!r}", reason="unknown_category")
    return category


def choices_of(session: Session, template: ParameterTemplate) -> list[ParameterChoice]:
    return list(
        session.execute(
            select(ParameterChoice)
            .where(ParameterChoice.template_id == template.id)
            .order_by(ParameterChoice.sort_order, ParameterChoice.key)
        ).scalars()
    )


def value_count(session: Session, template: ParameterTemplate) -> int:
    """How many parts hold a value for this field. The number every refusal names."""
    return int(
        session.execute(
            select(func.count())
            .select_from(ParameterValue)
            .where(ParameterValue.template_id == template.id)
        ).scalar_one()
    )


def choice_use_count(session: Session, choice: ParameterChoice) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(ParameterValue)
            .where(ParameterValue.choice_id == choice.id)
        ).scalar_one()
    )


def find_by_name(session: Session, name: str) -> ParameterTemplate | None:
    return session.execute(
        select(ParameterTemplate).where(ParameterTemplate.name == name)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validated_base_unit(value_type: ValueType, base_unit: str | None) -> str | None:
    """Refuse a `base_unit` the search path could not parse against, now.

    Returns the registry's canonical spelling. A numeric field with an
    unrecognised unit is the worst possible outcome of this feature: it is
    creatable, it appears in the filter panel, and every value entered against it
    is rejected — or, if the check were skipped at write time too, stored with
    null bounds and invisible to every range query.
    """
    if value_type != ValueType.NUMERIC:
        if base_unit:
            raise AuthoringError(
                f"a {value_type} field takes no base_unit; {base_unit!r} was given. "
                "A physical unit only means anything for a numeric field — a list "
                "field's options carry their own labels.",
                reason="unit_on_non_numeric",
            )
        return None

    if not base_unit:
        raise AuthoringError(
            "a numeric field needs a base_unit naming its physical quantity, so a "
            "bare '1M' can be read as 1 MΩ under resistance and refused under "
            f"capacitance. Choose one of: {', '.join(supported_quantities())}.",
            reason="missing_base_unit",
        )

    if not supported_quantity(base_unit):
        raise AuthoringError(
            f"{base_unit!r} is not a quantity the value parser recognises, so no value "
            "could ever be entered against this field and it would never match a "
            f"search. Give the quantity's name, not a unit symbol or a plural — "
            f"one of: {', '.join(supported_quantities())}.",
            reason="unknown_base_unit",
        )

    canonical = canonical_quantity(base_unit)
    assert canonical is not None  # supported_quantity just said so
    return canonical


def _validated_plausibility(low: float | None, high: float | None) -> None:
    if low is not None and high is not None and low > high:
        raise AuthoringError(
            f"plausible_min ({low}) is above plausible_max ({high}), which is a window "
            "no value can fall inside — every entry against this field would be "
            "refused as implausible.",
            reason="inverted_plausibility",
        )


def _validated_choices(specs: Sequence[ChoiceSpec], value_type: ValueType) -> None:
    if value_type != ValueType.ENUM:
        if specs:
            raise AuthoringError(
                f"choices only mean something for a list field; this one is {value_type}.",
                reason="choices_on_non_enum",
            )
        return
    if not specs:
        raise AuthoringError(
            "a list field needs at least one option. An enum template with no choices "
            "matches nothing and offers nothing to click.",
            reason="no_choices",
        )
    seen: set[str] = set()
    for spec in specs:
        folded = spec.key.strip().casefold()
        if folded in seen:
            raise AuthoringError(
                f"two options both have the key {spec.key!r}", reason="duplicate_choice_key"
            )
        seen.add(folded)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class ChoiceSpec:
    """One option of a list field, as authoring receives it.

    A plain class rather than the route's Pydantic model so the service does not
    import the wire types — the same direction of dependency `layout_authoring`
    keeps.
    """

    __slots__ = ("aliases", "key", "label", "sort_order")

    def __init__(
        self, key: str, label: str, aliases: Sequence[str] = (), sort_order: int = 0
    ) -> None:
        self.key = key
        self.label = label
        self.aliases = tuple(aliases)
        self.sort_order = sort_order


def dump_aliases(aliases: Iterable[str] | None) -> str | None:
    """Encode the alias list `app.services.parameters.choice_aliases` decodes.

    Written here rather than in the route because that function is documented as
    the **one** decoder of `aliases_json`; an encoder that disagreed with it would
    produce spellings search accepts and enrichment does not.
    """
    if aliases is None:
        return None
    cleaned = [alias.strip() for alias in aliases if alias.strip()]
    return json.dumps(cleaned) if cleaned else None


def create_template(
    session: Session,
    *,
    name: str,
    display_name: str,
    value_type: ValueType,
    substitution_direction: SubstitutionDirection,
    base_unit: str | None = None,
    applies_to_category: str | None = None,
    sort_order: int = 0,
    plausible_min: float | None = None,
    plausible_max: float | None = None,
    choices: Sequence[ChoiceSpec] = (),
) -> ParameterTemplate:
    """Mint a field and, for a list field, all of its options in one go.

    Authoring a list field is **one action**. Creating the field and then adding
    options one request at a time leaves a window in which an enum template exists
    with no choices — which is a field that matches nothing, offered in the filter
    panel as though it worked.

    The caller is responsible for the name-collision decision *before* calling
    this: `name` is globally UNIQUE and `idempotency.run` rolls back on
    `IntegrityError` to absorb a duplicate `client_op_id`, so letting a collision
    reach the insert conflates two unrelated conditions and returns a bare 500 —
    the same trap `create_container_type` documents.
    """
    unit = validated_base_unit(value_type, base_unit)
    _validated_plausibility(plausible_min, plausible_max)
    _validated_choices(choices, value_type)
    category = (
        require_category(session, applies_to_category).slug
        if applies_to_category is not None
        else None
    )

    template = ParameterTemplate(
        name=name,
        display_name=display_name,
        value_type=value_type,
        base_unit=unit,
        applies_to_category=category,
        substitution_direction=substitution_direction,
        sort_order=sort_order,
        plausible_min=plausible_min,
        plausible_max=plausible_max,
        is_seed=False,
    )
    session.add(template)
    session.flush()

    for index, spec in enumerate(choices):
        # Spaced by ten and starting at ten, so there is room to insert an option
        # before the first one later without renumbering the list.
        add_choice(
            session,
            template,
            key=spec.key,
            label=spec.label,
            aliases=spec.aliases,
            sort_order=spec.sort_order or (index + 1) * 10,
        )
    return template


def rename_template(session: Session, template: ParameterTemplate, name: str) -> None:
    _refuse_frozen(template, "name")
    clash = find_by_name(session, name)
    if clash is not None and clash.id != template.id:
        raise AuthoringError(
            f"a field named {name!r} already exists ({clash.display_name!r})",
            reason="duplicate_name",
        )
    template.name = name


def retype_template(session: Session, template: ParameterTemplate, value_type: ValueType) -> None:
    """Change what kind of field this is — only while nothing holds a value."""
    if value_type == ValueType(template.value_type):
        return
    _refuse_frozen(template, "value_type")
    held = value_count(session, template)
    if held:
        raise AuthoringError(
            f"{held} part{'s' if held != 1 else ''} already hold a {template.value_type} "
            f"value for {template.name!r}, and a {value_type} value lives in different "
            "columns — the stored rows would stay in the table and match nothing. "
            "Changing this is a data migration, not an edit: create a new field, or "
            "clear those values first.",
            reason="value_type_in_use",
        )
    template.value_type = value_type
    if value_type != ValueType.NUMERIC:
        # A quantity left behind on a field that is no longer numeric is a
        # contradiction the create path refuses (`unit_on_non_numeric`), so a
        # retype must not be able to produce one by omission.
        template.base_unit = None


def set_base_unit(session: Session, template: ParameterTemplate, base_unit: str | None) -> None:
    """Change the quantity — only while nothing holds a value.

    Stored bounds were computed under the old quantity: 1 µF re-read as 1 µH is
    still `1e-06` in the table, so every row keeps answering range queries with a
    number that means something else now. Nothing about that is visible.
    """
    unit = validated_base_unit(ValueType(template.value_type), base_unit)
    if unit == template.base_unit:
        return
    _refuse_frozen(template, "base_unit")
    held = value_count(session, template)
    if held:
        raise AuthoringError(
            f"{held} part{'s' if held != 1 else ''} already hold a value for "
            f"{template.name!r} in {template.base_unit}; re-reading those numbers as "
            f"{unit} would leave every one of them wrong but still searchable. "
            "Create a new field instead.",
            reason="base_unit_in_use",
        )
    template.base_unit = unit


def set_applies_to_category(
    session: Session, template: ParameterTemplate, slug: str | None
) -> None:
    template.applies_to_category = None if slug is None else require_category(session, slug).slug


def set_plausibility(template: ParameterTemplate, low: float | None, high: float | None) -> None:
    _validated_plausibility(low, high)
    template.plausible_min = low
    template.plausible_max = high


def delete_template(session: Session, template: ParameterTemplate) -> None:
    """Remove a field, if nothing depends on it.

    `parameter_value.template_id` is `ON DELETE CASCADE`, so an unguarded delete
    **silently destroys every value of this field** — no error, no count, nothing
    to undo. That is the one delete in this module that the database would happily
    perform.
    """
    if template.is_seed:
        raise AuthoringError(
            f"{template.name!r} is part of the shared field library and cannot be "
            "deleted; the decoders and extractors refer to it by name.",
            reason="seed_immutable",
        )
    held = value_count(session, template)
    if held:
        raise AuthoringError(
            f"{held} part{'s' if held != 1 else ''} hold a value for {template.name!r}. "
            "Deleting the field would delete those values with it, without asking. "
            "Clear them first if that is really the intent.",
            reason="field_in_use",
        )
    session.delete(template)


def add_choice(
    session: Session,
    template: ParameterTemplate,
    *,
    key: str,
    label: str,
    aliases: Sequence[str] = (),
    sort_order: int = 0,
) -> ParameterChoice:
    """Add one option. Additive, so permitted on a seed field too."""
    if ValueType(template.value_type) != ValueType.ENUM:
        raise AuthoringError(
            f"{template.name!r} is a {template.value_type} field, not a list, so it has "
            "no options.",
            reason="not_a_list_field",
        )
    key = key.strip()
    if not key:
        raise AuthoringError("an option needs a key", reason="empty_choice_key")

    existing = {choice.key.casefold() for choice in choices_of(session, template)}
    if key.casefold() in existing:
        raise AuthoringError(
            f"{template.name!r} already has an option keyed {key!r}",
            reason="duplicate_choice_key",
        )

    choice = ParameterChoice(
        template_id=template.id,
        key=key,
        label=label.strip() or key,
        aliases_json=dump_aliases(aliases),
        sort_order=sort_order,
    )
    session.add(choice)
    session.flush()
    return choice


def delete_choice(session: Session, choice: ParameterChoice) -> None:
    """Remove an option, naming how many parts use it if any do."""
    used = choice_use_count(session, choice)
    if used:
        raise AuthoringError(
            f"{used} part{'s' if used != 1 else ''} are filed under the option "
            f"{choice.key!r}. Deleting it would leave them with no value for this "
            "field — rename it instead, or move those parts first.",
            reason="choice_in_use",
        )
    session.delete(choice)


def _refuse_frozen(template: ParameterTemplate, field: str) -> None:
    if template.is_seed and field in SEED_FROZEN_FIELDS:
        raise AuthoringError(
            f"{template.name!r} is part of the shared field library, so its {field} is "
            "frozen: the MPN decoders, the datasheet extractors and every saved search "
            "name this field and expect it to mean what it means. Its display name, "
            "ordering, plausibility window and substitution direction are all still "
            "editable, and you can add a new field of your own.",
            reason="seed_immutable",
        )
