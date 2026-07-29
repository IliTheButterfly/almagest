"""The catalogue's own words, as a snapshot a prose description is read against.

A description like `3x 10k 1% 0603 resistor` is only readable because this
install already knows what `0603` and `resistor` mean: `parameter_choice` holds
`0603` as an alias of `0603_1608`, `part_categories` holds `resistor`, and
`parameter_template.base_unit` says what quantity `10k` could be. **The
deterministic parser invents no vocabulary of its own** — every token it
resolves, it resolves against a row somebody curated. That is what makes it
extensible without code: seed a `capacitor_technology` choice spelled `MLCC` and
the word starts being understood, in search and in prose, on the same day.

## Why a snapshot rather than the session

`load_vocabulary` reads the three tables once into frozen dataclasses, and
everything downstream is pure. Three reasons, in order of how much they matter:

1. The deterministic parser resolves a bare `10k` by **trial-parsing it against
   every numeric template** and seeing which quantities read it (`10 kΩ`? `10 kV`?
   `10 kA`?). That is N parses per token; doing it through ORM instances would
   put a session lifetime in the middle of a text-processing loop for no gain.
2. It makes the whole parser testable with no database, which is what keeps the
   table-driven corpus in `tests/unit/test_requirements.py` a unit test.
3. `Vocabulary` is also exactly what a model is allowed to answer in
   (`requirements.interpret`), and *that* has to be a value: it is serialised
   into a JSON schema.

The snapshot is read-only and short-lived — one per request or per batch of
lines. It is not a cache and nothing invalidates it.

## What is deliberately absent

`substitution_direction`. It is the entire substitution engine and it belongs to
the filter executor, which is the only thing allowed to decide what *satisfies*
a requirement. The front door translates words into a requirement and stops; if
it could see the direction it would eventually be tempted to apply it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from elec_value_parser import ParsedValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import PartCategory
from app.models.enums import ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate
from app.services.parameters import choice_aliases
from app.services.search.value_parser import parse_in_window

#: Runs of whitespace inside a phrase, so `through  hole` and `through hole` are
#: one key. Multi-word aliases are real — `surface mount`, `through hole` — and a
#: phrase index keyed on raw text would miss half of them.
_SPACES = re.compile(r"\s+")


def phrase_key(text: str) -> str:
    """The lookup key for a word or phrase: casefolded, spaces squashed."""
    return _SPACES.sub(" ", text.strip()).casefold()


@dataclass(frozen=True)
class ChoiceVocab:
    """One `parameter_choice`, with every spelling that resolves to it."""

    key: str
    label: str
    aliases: tuple[str, ...] = ()

    @property
    def spellings(self) -> tuple[str, ...]:
        """Key first, then aliases. All equal in authority — the key is not the
        "real" name, it is just the one `query_builder.Filter` carries."""
        return (self.key, *self.aliases)


@dataclass(frozen=True)
class TemplateVocab:
    """One `parameter_template`, reduced to what reading prose needs."""

    name: str
    display_name: str
    value_type: ValueType
    base_unit: str | None = None
    #: The `part_categories.slug` this parameter is specific to, or None for one
    #: that applies to everything (`voltage_rating`). Load-bearing in two
    #: directions: it is how a bare `10k` under "resistor" becomes a resistance,
    #: and how `100nF` on its own implies the category `capacitor`.
    applies_to_category: str | None = None
    plausible_min: float | None = None
    plausible_max: float | None = None
    choices: tuple[ChoiceVocab, ...] = ()

    @property
    def is_numeric(self) -> bool:
        return self.value_type is ValueType.NUMERIC and bool(self.base_unit)

    def parse(self, text: str) -> ParsedValue:
        """Read `text` as a value of this template's quantity, or raise.

        Goes through `search.value_parser` so the template's plausibility window
        is applied by the same code the search path uses — the guard that turns
        `1M` under farads into a refusal instead of a megafarad.
        """
        return parse_in_window(
            text,
            base_unit=self.base_unit,
            plausible_min=self.plausible_min,
            plausible_max=self.plausible_max,
            label=self.name,
        )


@dataclass(frozen=True)
class CategoryVocab:
    """One `part_categories` row. Slug and name are both spellings of it."""

    slug: str
    name: str


@dataclass(frozen=True)
class ChoiceMatch:
    """A spelling resolved to one choice of one template."""

    template: str
    key: str


@dataclass(frozen=True)
class Vocabulary:
    """Everything this install knows how to name, indexed for phrase lookup."""

    templates: tuple[TemplateVocab, ...] = ()
    categories: tuple[CategoryVocab, ...] = ()

    @cached_property
    def numeric_templates(self) -> tuple[TemplateVocab, ...]:
        return tuple(template for template in self.templates if template.is_numeric)

    def template(self, name: str) -> TemplateVocab | None:
        for template in self.templates:
            if template.name == name:
                return template
        return None

    def category(self, slug: str) -> CategoryVocab | None:
        for category in self.categories:
            if category.slug == slug:
                return category
        return None

    def numeric_templates_for_category(self, slug: str) -> tuple[TemplateVocab, ...]:
        """Numeric templates scoped *specifically* to this category.

        A template with no `applies_to_category` applies to every part and can
        therefore never be the unique reading of a bare number — including
        `voltage_rating` here would make `10k resistor` ambiguous between 10 kΩ
        and 10 kV, which is the opposite of the point.
        """
        return tuple(
            template for template in self.numeric_templates if template.applies_to_category == slug
        )

    @cached_property
    def _choices_by_phrase(self) -> dict[str, tuple[ChoiceMatch, ...]]:
        index: dict[str, list[ChoiceMatch]] = {}
        for template in self.templates:
            for choice in template.choices:
                for spelling in choice.spellings:
                    match = ChoiceMatch(template=template.name, key=choice.key)
                    index.setdefault(phrase_key(spelling), []).append(match)
        return {phrase: tuple(matches) for phrase, matches in index.items()}

    @cached_property
    def _categories_by_phrase(self) -> dict[str, tuple[str, ...]]:
        """Slug and display name, plus a naive singular.

        The plural is the only spelling generated rather than curated, because
        `part_categories.name` is written for a heading ("Resistors") while a
        description names one part ("resistor"), and asking a user to add
        "resistor" as an alias of "Resistors" would be a chore with one right
        answer. Nothing else is inflected: guessing that `ic` means
        `integrated circuits` is a job for the model seam, not for a rule here.
        """
        index: dict[str, list[str]] = {}
        for category in self.categories:
            spellings = {phrase_key(category.slug), phrase_key(category.name)}
            singulars = {spelling.removesuffix("s") for spelling in spellings if spelling != "s"}
            for candidate in sorted(spellings | singulars):
                if candidate:
                    index.setdefault(candidate, []).append(category.slug)
        return {phrase: tuple(dict.fromkeys(slugs)) for phrase, slugs in index.items()}

    @cached_property
    def max_phrase_words(self) -> int:
        """The widest n-gram worth trying, so `through hole` is reachable."""
        phrases = (*self._choices_by_phrase, *self._categories_by_phrase)
        return max((len(phrase.split(" ")) for phrase in phrases), default=1)

    def choices_for(self, phrase: str) -> tuple[ChoiceMatch, ...]:
        return self._choices_by_phrase.get(phrase_key(phrase), ())

    def categories_for(self, phrase: str) -> tuple[str, ...]:
        return self._categories_by_phrase.get(phrase_key(phrase), ())


def load_vocabulary(session: Session) -> Vocabulary:
    """Read the snapshot. One query per table, no lazy loading afterwards."""
    choices: dict[int, list[ChoiceVocab]] = {}
    choice_rows = session.execute(
        select(ParameterChoice).order_by(
            ParameterChoice.template_id, ParameterChoice.sort_order, ParameterChoice.id
        )
    ).scalars()
    for row in choice_rows:
        choices.setdefault(row.template_id, []).append(
            ChoiceVocab(key=row.key, label=row.label, aliases=choice_aliases(row))
        )

    template_rows = session.execute(
        select(ParameterTemplate).order_by(ParameterTemplate.sort_order, ParameterTemplate.name)
    ).scalars()
    templates = tuple(
        TemplateVocab(
            name=row.name,
            display_name=row.display_name,
            value_type=ValueType(row.value_type),
            base_unit=row.base_unit,
            applies_to_category=row.applies_to_category,
            plausible_min=row.plausible_min,
            plausible_max=row.plausible_max,
            choices=tuple(choices.get(row.id, ())),
        )
        for row in template_rows
    )

    category_rows = session.execute(select(PartCategory).order_by(PartCategory.slug)).scalars()
    categories = tuple(CategoryVocab(slug=row.slug, name=row.name) for row in category_rows)

    return Vocabulary(templates=templates, categories=categories)
