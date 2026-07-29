"""Prose in, a structured `Requirement` out. **The front door never decides.**

The input is a description, not a part number — the kind of line an agent writes
while designing something:

    3x 10k 1% 0603 resistor
    100nF 50V X7R 0603
    a dual op-amp, rail-to-rail, SOIC-8
    something to level-shift 3.3V to 5V
    LM358N

The architecture is fixed and it is the whole point of this module:

    prose  ->  a structured Requirement  ->  the existing filter executor

A model may only ever produce the **middle** term, and only the part of it the
grammar could not. Nothing here answers "which part should I use": that is the
filter executor's answer, computed from `substitution_direction`, which is
correct by construction. A plausible substitute with the wrong voltage rating is
a field failure, so the decision cannot be delegated — see `search.query_builder`
and the invariant in `CLAUDE.md`.

## Deterministic first, and it gets much further than it looks

Nothing in the first three examples above needs a model. `elec-value-parser`
reads `10k`, `100nF`, `50V`, `1%`; `parameter_choice` already holds `0603`,
`X7R`, `SOIC-8` and `through hole` as curated spellings; `part_categories` holds
`resistor`. So this pass is a tokeniser over vocabulary somebody already
maintains (`requirements.vocabulary`), and what it cannot account for it **lists**
(`Requirement.residue`). That list is the entire signal for whether a model is
worth calling — and it is empty for most real lines, which is a fact that would
have been hidden had the model path been built first.

### How a token finds its template, without a unit table of its own

One rule, applied twice. A token is trial-parsed against **every** numeric
template; the templates that read it are its candidates.

* `100nF` is read by `capacitance` and refused by everything else, so it is
  unambiguous — no category needed, no unit table here, and a template added
  tomorrow joins the sweep for free.
* `10k` is read by resistance, voltage, current *and* power. It is not ambiguous
  in a **line that says `resistor`**: the category narrows the candidates to the
  templates scoped specifically to it (`applies_to_category`), which leaves
  exactly `resistance`.
* `470` with nothing to scope it stays ambiguous, and is **refused with its
  candidate list**, never assigned to the first plausible template.

The same mechanism running backwards supplies the category when the line does not
name one: `100nF` resolved to `capacitance`, whose `applies_to_category` is
`capacitor`, so `100nF 50V X7R 0603` gets a category from the schema rather than
from a guess.

### Order matters, and one ordering is load-bearing

Curated spellings are matched **before values, and before anything that reshapes
a token into a value**, because `0603` parses cleanly as 603 Ω. In
`10k 0603 resistor` the wrong order silently produces a second resistance filter
and a contradiction, and in a line with no other value it produces a 603 Ω
requirement that looks entirely reasonable. `tests/unit/test_requirements.py` and
`tests/integration/test_bom_intake_findings.py` pin this.

The "before anything that reshapes a token" half is not pedantry: it is the fix
for a real defect. `_attach_tolerances` fuses a bare `1%` onto the value it
belongs to, and it used to run over the *raw* token stream, before the vocabulary
was consulted at all — so `10k 0603 1% resistor` produced the token `0603 ±1%`,
which is not a package spelling, reads as 603 Ω, contradicts `10k`, and left the
line searching on `category=resistor` alone. A 220 Ω 1206 resistor was then
offered as the rank-1 *exact* match for a 10 k 0603 line, at `confidence: 1.0`.
So the fusion now runs over the **leftovers**, after `_consume_vocabulary` has
taken every curated spelling out of the stream, and only onto a token the value
grammar can actually read.

## Refusing to guess, and the two lists that come out of it

Two tokens naming the same template with different values are a **contradiction**,
and both are dropped. `something to level-shift 3.3V to 5V` is the honest
example: `3.3V` and `5V` both read as `voltage_rating`, and a requirement
carrying either one of them would assert a rating the user never asked for.
Refusing costs a review item; guessing puts an invented predicate into a search
that will be trusted.

So a `Requirement` carries two different admissions of ignorance, deliberately
not merged:

* `residue` — words nothing could account for (`op-amp`, `level-shift`). The only
  thing a model is ever shown.
* `rejections` — text that *was* read and refused, each with a machine reason
  (`implausible`, `ambiguous_template`, `contradictory_choice`). A model is never
  shown these, because the only thing it could do with `1M` under farads is talk
  us out of a correct refusal.

## A requirement is not a part

An MPN in the text becomes `Requirement.mpn_norm`, a **lookup key for the next
stage**, and never a `part_id`. Resolving identity here would put "the catalogue
contains this part" and "the user asked for this part" in one object, and the
second is not evidence for the first. Unparseable input is likewise a normal
outcome, not an error: the text is preserved, the residue is listed, and the line
still becomes a BOM line with a note, because `bom_lines.part_id` is nullable and
losing the line is worse than not understanding it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum

from elec_value_parser import ValueParseError

from app.services.requirements.vocabulary import TemplateVocab, Vocabulary
from app.services.scanning.codes import normalize_mpn
from app.services.search.query_builder import Filter
from app.services.search.value_parser import reads_as_a_quantity


class FieldOrigin(StrEnum):
    """Where one field of a requirement came from. Per field, not per line."""

    #: The value grammar or a curated spelling. An exact table lookup.
    DETERMINISTIC = "deterministic"
    #: A model's reading of the residue. Never overrides the grammar.
    INTERPRETED = "interpreted"


class RequirementProvenance(StrEnum):
    """The line-level roll-up of `FieldOrigin`, for a UI that must show it."""

    NONE = "none"
    DETERMINISTIC = "deterministic"
    MIXED = "mixed"
    INTERPRETED = "interpreted"


#: Reasons this module attaches to a `Rejection`, on top of the value grammar's
#: own stable codes (`implausible`, `unit_mismatch`, `syntax`, ...). Listed so a
#: UI can group them and a test can assert one was chosen deliberately.
AMBIGUITY_REASONS: frozenset[str] = frozenset(
    {
        #: A number, but more than one template reads it and nothing narrows it.
        "ambiguous_template",
        #: One spelling resolving to choices of two different templates.
        "ambiguous_choice",
        #: Two spellings naming different choices of the *same* template.
        "contradictory_choice",
        #: Two values for one numeric template.
        "contradictory_value",
        #: Two category words, or two templates implying different categories.
        "contradictory_category",
        #: Two part-number-shaped tokens in one line.
        "ambiguous_mpn",
        #: A model answered where the grammar had already decided.
        "model_contradicted_grammar",
    }
)


@dataclass(frozen=True)
class RequirementFilter:
    """One predicate, in the vocabulary `query_builder.Filter` already accepts."""

    #: `parameter_template.name`.
    template: str
    #: For an enum, the `parameter_choice.key` — canonical, so the executor's
    #: `resolve_choice` cannot fail on it. For a numeric, the **value text**
    #: (`10k ±1%`), re-parsed downstream by the same grammar that validated it
    #: here. Storing the text rather than a number is what keeps one definition
    #: of an interval: nothing in this module computes bounds.
    value: str
    #: The words this came from, verbatim. Required, for the same reason
    #: `enrichment.extract.ExtractedField.source_text` is: a predicate nobody can
    #: trace back to something the user wrote cannot be reviewed.
    source_text: str
    origin: FieldOrigin = FieldOrigin.DETERMINISTIC
    confidence: float = 1.0

    def as_filter(self) -> Filter:
        return Filter(template=self.template, value=self.value)


@dataclass(frozen=True)
class RequirementCategory:
    """The category the line is about, and how that was established."""

    slug: str
    source_text: str
    origin: FieldOrigin = FieldOrigin.DETERMINISTIC
    confidence: float = 1.0


@dataclass(frozen=True)
class Rejection:
    """Text that was read and refused, with a reason a UI can route on."""

    source_text: str
    reason: str
    message: str
    #: The template the refusal is about, when there is exactly one.
    template: str | None = None
    #: Templates that *could* have read it, for an ambiguity. This is the list a
    #: human needs to answer the question, so it travels with the refusal.
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Requirement:
    """What the next stage matches against. Not a part, and not an answer."""

    #: Verbatim input, always preserved — a description nobody can parse still
    #: becomes a BOM line with a note.
    text: str
    #: Units wanted, or **None for unspecified**. Not defaulted to 1: `3x 10k`
    #: says three and `10k 0603` says nothing, and quietly turning the second
    #: into a quantity of one manufactures a BOM figure the user never gave.
    quantity: int | None = None
    category: RequirementCategory | None = None
    filters: tuple[RequirementFilter, ...] = ()
    #: A part-number-shaped token, verbatim.
    mpn: str | None = None
    #: `parts.mpn_norm`-normalised. **A lookup key for the next stage, never an
    #: identity** — that a description contains `LM358N` is not evidence the
    #: catalogue's `LM358N` is the part meant, and only the next stage, matching
    #: deterministically, may say so.
    mpn_norm: str | None = None
    #: Words nothing accounted for. The whole signal for "is a model needed", and
    #: the only thing a model is ever shown.
    residue: tuple[str, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    #: Human-readable remarks that are not refusals — an inference worth showing.
    notes: tuple[str, ...] = ()

    @property
    def category_slug(self) -> str | None:
        return self.category.slug if self.category else None

    @property
    def is_actionable(self) -> bool:
        """Whether there is anything here the filter executor could run.

        A line that is only a quantity is not actionable, and neither is an empty
        one. Both are still `Requirement`s — an un-actionable requirement is a
        worklist item, not an error.
        """
        return bool(self.filters or self.category or self.mpn_norm)

    @property
    def is_complete(self) -> bool:
        """Whether everything in the text was accounted for.

        The honest companion to `confidence`, and the reason that number is
        allowed to be about the fields present. A line with one confident
        `package` filter and three words nobody understood is 1.0 confident and
        **not complete**, and a UI that shows only the first is lying by omission.
        """
        return not self.residue and not self.rejections

    @property
    def confidence(self) -> float:
        """How much to trust the fields that *are* here: the weakest of them.

        Deterministic fields are 1.0 — not a claim that the user's intent was
        understood, only that the token maps to this filter by an exact lookup in
        the value grammar or a curated spelling. A model's field carries the
        model's own (capped) number, so one interpreted category drags the whole
        line down, which is exactly what the next stage has to show.

        Deliberately **not** multiplied by anything derived from `residue`.
        Incomplete and wrong are different failures: `residue` is a list, because
        "three words I could not read" is actionable and "0.62" is not.
        """
        scores = [item.confidence for item in self.filters]
        if self.category is not None:
            scores.append(self.category.confidence)
        if self.mpn_norm is not None:
            scores.append(1.0)
        return min(scores) if scores else 0.0

    @property
    def provenance(self) -> RequirementProvenance:
        origins = {item.origin for item in self.filters}
        if self.category is not None:
            origins.add(self.category.origin)
        if self.mpn_norm is not None:
            # A model may not produce a part number at all, so this is always
            # the grammar's. See `requirements.interpret`.
            origins.add(FieldOrigin.DETERMINISTIC)
        if not origins:
            return RequirementProvenance.NONE
        if origins == {FieldOrigin.DETERMINISTIC}:
            return RequirementProvenance.DETERMINISTIC
        if origins == {FieldOrigin.INTERPRETED}:
            return RequirementProvenance.INTERPRETED
        return RequirementProvenance.MIXED

    def to_filters(self) -> tuple[Filter, ...]:
        """The predicates, in the executor's own shape.

        The single contact point with `search.query_builder`, and it stops here:
        this module builds no `SearchQuery` and calls no executor, because
        deciding what matches is the next stage's job and mixing the two is how
        the fuzzy front door would start answering questions.
        """
        return tuple(item.as_filter() for item in self.filters)


# ---------------------------------------------------------------------------
# Tokenising
# ---------------------------------------------------------------------------

#: A leading count. Every form requires an explicit marker, so a bare leading
#: integer is **not** a quantity — `470 resistor` is a value, and reading it as a
#: count would be the same class of silent invention this module exists to avoid.
#: `3x10k` is likewise not a count: unspaced `NxM` is a dimension (`5x20mm`
#: fuses) far more often than a multiplier.
_QUANTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # `\u00d7` is the multiplication sign, spelled as an escape because a literal
    # one is (correctly) flagged as confusable with `x` — which is the whole
    # reason both are accepted here.
    re.compile(r"^(\d+)\s*[x\u00d7*](?:\s+|$)", re.IGNORECASE),
    re.compile(r"^[x\u00d7*]\s*(\d+)(?:\s+|$)", re.IGNORECASE),
    re.compile(r"^qty\.?\s*[:=]?\s*(\d+)(?:\s+|$)", re.IGNORECASE),
    re.compile(r"^(\d+)\s*(?:pcs?|pieces?|off)(?:\s+|$)", re.IGNORECASE),
)

#: Token separators. Commas and semicolons separate clauses in a description
#: (`a dual op-amp, rail-to-rail, SOIC-8`) and never appear inside a value the
#: grammar accepts, so splitting on them costs nothing.
_SEPARATORS = re.compile(r"[\s,;]+")

#: Punctuation that decorates a token rather than belonging to it. Hyphens, dots,
#: percent signs, ± and / stay: they are all inside values and part numbers
#: (`0R22`, `±1%`, `RC0603FR-0710KL`, `3.3V`).
_TRIM = "()[]{}\"'`:!?"

#: A bare tolerance, which the grammar only accepts attached to a value. Joining
#: it to the token before it is what makes `10k 1%` one predicate with an
#: interval of 9900-10100 ohm instead of a resistance plus an unreadable `1%`.
#:
#: **Applied only to leftovers, and only onto a token that reads as a value** —
#: see `_attach_tolerances` for both gates and the defect each one closes.
_BARE_TOLERANCE = re.compile(r"^[±+]?(\d+(?:\.\d+)?)\s*%$")

#: Articles and filler. A word here carries no catalogue meaning, so it is
#: dropped rather than listed as residue — residue is a signal, and padding it
#: with `a` and `the` makes a line look less understood than it is. Applied only
#: to leftovers, so a stop word that is also a curated spelling is matched first.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "for",
        "i",
        "in",
        "need",
        "of",
        "or",
        "please",
        "some",
        "something",
        "the",
        "to",
        "want",
        "we",
        "with",
    }
)

#: Value-grammar refusals that mean "this text is not a value at all", as opposed
#: to "this is a bad value". Same set and same reasoning as
#: `bom_import._MPN_SHAPED_FAILURES`: a refusal on syntax is evidence of a part
#: number, while `implausible` is evidence of a wrong one.
_NOT_A_VALUE_AT_ALL: frozenset[str] = frozenset({"syntax", "unknown_unit"})

#: Shortest normalised part number worth believing. Below this the letter+digit
#: test alone starts accepting things like `5V1`.
_MPN_MIN_LENGTH = 4

#: A value in this grammar always has a mantissa, so a token with no digit in it
#: cannot be one and is never trial-parsed. Not an optimisation: `rail-to-rail`
#: and `level-shift` reach the grammar's range splitter, which refuses them with
#: `ambiguous_range` — a *value* refusal. Read as one, an English hyphenated word
#: would be filed as a bad value instead of as residue, and the residue is the
#: only thing the model seam is given.
_HAS_DIGIT = re.compile(r"\d")


def looks_like_a_part_number(token: str) -> bool:
    """Whether a token has the shape of an MPN — and is not a value.

    The value grammar is the gate, exactly as in `bom_import._mpn_candidates`:
    `reads_as_a_quantity` returning None is the one definition of "this text is
    not an electrical value", and it is shared so that `10k` cannot be a part
    number in one code path and a resistance in another. The shape test on top of
    it is cheap insurance against a bare word: a part number mixes letters and
    digits.
    """
    if reads_as_a_quantity(token) is not None:
        return False
    key = normalize_mpn(token)
    if len(key) < _MPN_MIN_LENGTH:
        return False
    return any(character.isdigit() for character in key) and any(
        character.isalpha() for character in key
    )


def _split_quantity(text: str) -> tuple[int | None, str]:
    for pattern in _QUANTITY_PATTERNS:
        match = pattern.match(text.strip())
        if match:
            return int(match.group(1)), text.strip()[match.end() :]
    return None, text


@dataclass(frozen=True)
class _Token:
    """What is parsed, and what the user actually wrote.

    They differ for exactly one reason today — a re-attached tolerance, where
    `10k 1%` is parsed as `10k ±1%` — and that difference has to survive, because
    `RequirementFilter.source_text` is the words a reviewer is shown. Quoting a
    spelling the user never typed is a small lie that makes the review harder.

    `source` is always words the user wrote, in the order they wrote them, but it
    is **not guaranteed contiguous**: in `10k 0603 1% resistor` the package is
    taken out of the stream before the tolerance is re-attached, so the resistance
    filter's source is `10k 1%`. That is the honest rendering — both words are
    the user's, and the alternative is either quoting `±1%` (a spelling nobody
    typed) or dropping the tolerance the user clearly stated.
    """

    text: str
    source: str


def _split_tokens(text: str) -> tuple[_Token, ...]:
    """Separators and decoration only. **Nothing here reads or reshapes a value.**

    Deliberately dumber than it was. This pass used to fuse a bare `N%` onto
    whatever token preceded it, which put a value-grammar decision *ahead* of the
    curated-spelling pass and inverted the one ordering this module says is
    load-bearing. See `_attach_tolerances`.
    """
    tokens: list[_Token] = []
    for raw in _SEPARATORS.split(text):
        token = raw.strip(_TRIM)
        if not token:
            continue
        tokens.append(_Token(text=token, source=token))
    return tuple(tokens)


def _attach_tolerances(tokens: list[_Token]) -> list[_Token]:
    """Fuse a bare `1%` onto the value it qualifies. **Two gates, two defects.**

    Runs over the *leftovers* — what `_consume_vocabulary` did not claim — and
    only onto a token `reads_as_a_quantity` accepts. Each gate closes a way a
    reasonable-looking predicate got invented out of thin air, both of which
    presented as `provenance: deterministic, confidence: 1.0`:

    * **Leftovers, not the raw stream.** `10k 0603 1% resistor` used to produce
      the token `0603 ±1%`. It is not a package spelling, so `_take_choice` never
      saw it; it reads as 603 Ω, contradicts `10k`, and `without_contradictions`
      dropped *both* — leaving `category=resistor` as the entire requirement, and
      a 220 Ω 1206 part as the rank-1 exact match for a 10 k 0603 line.
    * **Only onto something that reads as a value.** `10k resistor 1%` used to
      produce `resistor ±1%`, which is not a category spelling either, has a digit
      in it, and passes `looks_like_a_part_number` (`normalize_mpn` → `resistor1`).
      That became `Requirement.mpn` and then `SearchQuery.text`, so the line was
      matched on a part number that appears nowhere in the input.

    The gate is `reads_as_a_quantity` rather than a shape test for the same reason
    `looks_like_a_part_number` uses it: there is one definition of "this text is an
    electrical value" in this codebase, and a second one here would eventually
    disagree with it.
    """
    attached: list[_Token] = []
    for token in tokens:
        tolerance = _BARE_TOLERANCE.match(token.text)
        if tolerance and attached and reads_as_a_quantity(attached[-1].text) is not None:
            previous = attached[-1]
            attached[-1] = _Token(
                # Re-attached in the spelling the grammar documents, so what
                # reaches the parser is a form it accepts.
                text=f"{previous.text} ±{tolerance.group(1)}%",
                source=f"{previous.source} {token.source}",
            )
            continue
        attached.append(token)
    return attached


# ---------------------------------------------------------------------------
# The deterministic parser
# ---------------------------------------------------------------------------


@dataclass
class _Draft:
    """Mutable working state for one line. Frozen into a `Requirement` at the end."""

    text: str
    quantity: int | None = None
    filters: list[RequirementFilter] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    residue: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    category: RequirementCategory | None = None
    #: Set once two category words have disagreed, so a third cannot quietly win.
    category_conflicted: bool = False
    #: Tokens that read as a value under more than one template, waiting for a
    #: category to narrow them.
    deferred: list[tuple[_Token, tuple[str, ...]]] = field(default_factory=list)
    #: Categories implied by matched templates' `applies_to_category`.
    implied: dict[str, str] = field(default_factory=dict)
    mpn_tokens: list[str] = field(default_factory=list)


class DeterministicRequirementParser:
    """Reads a description against a `Vocabulary`. No session, no model, no network.

    Constructed once per batch: the phrase index and the trial-parse sweep are
    both per-vocabulary, and a page of BOM descriptions shares them.
    """

    def __init__(self, vocabulary: Vocabulary) -> None:
        self._vocabulary = vocabulary

    def parse(self, text: str) -> Requirement:
        draft = _Draft(text=text)
        draft.quantity, remainder = _split_quantity(text)
        tokens = _split_tokens(remainder)

        # Curated spellings first, *then* the tolerance fusion, then values. The
        # fusion is a value-grammar operation, so running it before the vocabulary
        # pass would invert the ordering the module docstring calls load-bearing —
        # and it did.
        leftovers = self._consume_vocabulary(draft, tokens)
        self._read_values(draft, _attach_tolerances(leftovers))
        self._settle_category(draft)
        self._resolve_deferred(draft)
        return self._freeze(draft)

    # -- pass 1: curated spellings, longest phrase first --------------------

    def _consume_vocabulary(self, draft: _Draft, tokens: tuple[_Token, ...]) -> list[_Token]:
        """Match choices and categories over n-grams, and return what is left.

        Longest-first and left-to-right so `surface mount device` prefers the
        two-word spelling, and greedy because a curated phrase is a stronger
        signal than any reading of its parts.
        """
        leftovers: list[_Token] = []
        index = 0
        width_limit = self._vocabulary.max_phrase_words
        while index < len(tokens):
            widest = min(width_limit, len(tokens) - index)
            for width in range(widest, 0, -1):
                window = tokens[index : index + width]
                phrase = " ".join(token.text for token in window)
                source = " ".join(token.source for token in window)
                if self._take_choice(draft, phrase, source) or self._take_category(
                    draft, phrase, source
                ):
                    index += width
                    break
            else:
                leftovers.append(tokens[index])
                index += 1
        return leftovers

    def _take_choice(self, draft: _Draft, phrase: str, source: str) -> bool:
        matches = self._vocabulary.choices_for(phrase)
        if not matches:
            return False

        templates = {match.template for match in matches}
        keys = {match.key for match in matches}
        if len(templates) > 1 or len(keys) > 1:
            # One spelling curated onto two facets. Guessing which the user meant
            # would silently apply a predicate they never wrote; the fix is to
            # de-duplicate the alias, and saying so is more useful than choosing.
            draft.rejections.append(
                Rejection(
                    source_text=source,
                    reason="ambiguous_choice",
                    message=(
                        f"{phrase!r} is a spelling of more than one choice "
                        f"({', '.join(sorted(f'{m.template}={m.key}' for m in matches))}), "
                        "so nothing can be read from it"
                    ),
                    candidates=tuple(sorted(templates)),
                )
            )
            return True

        match = matches[0]
        draft.filters.append(
            RequirementFilter(template=match.template, value=match.key, source_text=source)
        )
        self._note_implication(draft, match.template, source)
        return True

    def _take_category(self, draft: _Draft, phrase: str, source: str) -> bool:
        slugs = self._vocabulary.categories_for(phrase)
        if not slugs:
            return False
        if len(slugs) > 1:
            draft.rejections.append(
                Rejection(
                    source_text=source,
                    reason="ambiguous_choice",
                    message=(
                        f"{phrase!r} names more than one category ({', '.join(sorted(slugs))})"
                    ),
                )
            )
            return True

        slug = slugs[0]
        if draft.category is not None and draft.category.slug != slug:
            draft.rejections.append(
                Rejection(
                    source_text=f"{draft.category.source_text}, {source}",
                    reason="contradictory_category",
                    message=(
                        f"the line names two categories ({draft.category.slug} and {slug}); "
                        "one part belongs to one of them, so neither is applied"
                    ),
                )
            )
            draft.category = None
            draft.category_conflicted = True
            return True
        if draft.category is None and not draft.category_conflicted:
            draft.category = RequirementCategory(slug=slug, source_text=source)
        return True

    def _note_implication(self, draft: _Draft, template_name: str, source_text: str) -> None:
        """Record the category a matched template implies, per its own column."""
        template = self._vocabulary.template(template_name)
        if template is None or not template.applies_to_category:
            return
        if self._vocabulary.category(template.applies_to_category) is None:
            # A template scoped to a category this install does not have. Not
            # worth a refusal — it is a configuration slip, not a bad line.
            return
        draft.implied.setdefault(template.applies_to_category, source_text)

    # -- pass 2: values and part numbers -----------------------------------

    def _read_values(self, draft: _Draft, leftovers: list[_Token]) -> None:
        for token in leftovers:
            if not _HAS_DIGIT.search(token.text):
                self._set_aside(draft, token)
                continue
            if looks_like_a_part_number(token.text):
                draft.mpn_tokens.append(token.text)
                continue

            readable, refusals = self._trial_parse(token.text)
            if len(readable) == 1:
                template = readable[0]
                draft.filters.append(
                    RequirementFilter(
                        template=template.name, value=token.text, source_text=token.source
                    )
                )
                self._note_implication(draft, template.name, token.source)
            elif readable:
                draft.deferred.append((token, tuple(one.name for one in readable)))
            elif refusals and any(
                reason not in _NOT_A_VALUE_AT_ALL for reason in refusals.values()
            ):
                draft.rejections.append(self._refusal(token, refusals))
            else:
                self._set_aside(draft, token)

    def _set_aside(self, draft: _Draft, token: _Token) -> None:
        """A word, not a value. Residue unless it is pure filler."""
        if token.text.casefold() not in _STOP_WORDS:
            draft.residue.append(token.source)

    def _trial_parse(self, token: str) -> tuple[list[TemplateVocab], dict[str, str]]:
        """Which numeric templates read this token, and why the others did not."""
        readable: list[TemplateVocab] = []
        refusals: dict[str, str] = {}
        for template in self._vocabulary.numeric_templates:
            try:
                template.parse(token)
            except ValueParseError as error:
                refusals[template.name] = error.reason
            else:
                readable.append(template)
        return readable, refusals

    def _refusal(self, token: _Token, refusals: dict[str, str]) -> Rejection:
        """Turn "every template refused it" into the most informative refusal.

        The interesting refusals come first: a value the grammar read and found
        physically absurd (`1M` under farads) or filed under the wrong quantity
        (`100nH` where nothing is measured in henries) is a real, correctable
        mistake, while `syntax` only means "not a value".
        """
        for reason in ("implausible", "unit_mismatch", "inverted_range", "ambiguous_range"):
            named = [name for name, refused in refusals.items() if refused == reason]
            if named:
                return Rejection(
                    source_text=token.source,
                    reason=reason,
                    message=(
                        f"{token.text!r} was read as a value and refused ({reason}) by "
                        f"{', '.join(sorted(named))}"
                    ),
                    template=named[0] if len(named) == 1 else None,
                    candidates=tuple(sorted(named)),
                )
        reason = sorted(set(refusals.values()))[0]
        return Rejection(
            source_text=token.source,
            reason=reason,
            message=f"{token.text!r} is not a value any parameter can hold ({reason})",
        )

    # -- pass 3: the category, then the numbers it explains -----------------

    def _settle_category(self, draft: _Draft) -> None:
        """An explicit category word wins; otherwise the schema supplies one."""
        if draft.category is not None or draft.category_conflicted or not draft.implied:
            return
        if len(draft.implied) > 1:
            draft.notes.append(
                "no category: the line's parameters belong to more than one "
                f"({', '.join(sorted(draft.implied))})"
            )
            return
        slug, source_text = next(iter(draft.implied.items()))
        draft.category = RequirementCategory(slug=slug, source_text=source_text)
        draft.notes.append(f"category {slug} inferred from {source_text!r}")

    def _resolve_deferred(self, draft: _Draft) -> None:
        """Read the bare numbers, now that the category may have narrowed them."""
        scoped = (
            self._vocabulary.numeric_templates_for_category(draft.category.slug)
            if draft.category
            else ()
        )
        for token, candidates in draft.deferred:
            if len(scoped) == 1:
                template = scoped[0]
                try:
                    template.parse(token.text)
                except ValueParseError as error:
                    # The `1M` under capacitance case. It must surface with its
                    # reason: swallowed, the line would look like a capacitor
                    # requirement with one value quietly missing.
                    draft.rejections.append(
                        Rejection(
                            source_text=token.source,
                            reason=error.reason,
                            message=f"{token.text!r} cannot be a {template.name}: {error}",
                            template=template.name,
                        )
                    )
                    continue
                draft.filters.append(
                    RequirementFilter(
                        template=template.name, value=token.text, source_text=token.source
                    )
                )
                continue

            # Two or more readings survive — either the category scoped nothing,
            # or it scoped several. Either way the line does not say which, and
            # `candidates` is what a human needs to answer that, so it travels
            # with the refusal instead of one of them being picked.
            narrowed = tuple(template.name for template in scoped) or candidates
            draft.rejections.append(
                Rejection(
                    source_text=token.source,
                    reason="ambiguous_template",
                    message=(
                        f"{token.text!r} carries no unit and reads as {', '.join(narrowed)}; "
                        "nothing in the line says which"
                    ),
                    candidates=narrowed,
                )
            )

    # -- and freeze --------------------------------------------------------

    def _freeze(self, draft: _Draft) -> Requirement:
        filters, rejections = without_contradictions(draft.filters, self._vocabulary)
        mpn, mpn_rejection = _one_part_number(draft.mpn_tokens)
        if mpn_rejection is not None:
            rejections = (*rejections, mpn_rejection)
        return Requirement(
            text=draft.text,
            quantity=draft.quantity,
            category=draft.category,
            filters=filters,
            mpn=mpn,
            mpn_norm=normalize_mpn(mpn) if mpn else None,
            residue=tuple(dict.fromkeys(draft.residue)),
            rejections=(*draft.rejections, *rejections),
            notes=tuple(draft.notes),
        )


def without_contradictions(
    filters: list[RequirementFilter], vocabulary: Vocabulary
) -> tuple[tuple[RequirementFilter, ...], tuple[Rejection, ...]]:
    """Drop every filter of a template two tokens disagreed about.

    Not "keep the first" and not "OR them together". The executor does support an
    OR (`0603,1206` is one facet with two acceptable answers), but reading a
    contradiction as an OR silently *widens* the requirement, and reading it as
    the first token silently narrows it to a guess — and `0603 1206` in prose is
    a mistake far more often than it is a choice. A dropped filter plus a stated
    contradiction is the one outcome that cannot mislead.
    """
    grouped: dict[str, list[RequirementFilter]] = {}
    for item in filters:
        grouped.setdefault(item.template, []).append(item)

    kept: list[RequirementFilter] = []
    rejections: list[Rejection] = []
    for template, group in grouped.items():
        values = {item.value for item in group}
        if len(values) == 1:
            kept.append(group[0])
            continue
        known = vocabulary.template(template)
        numeric = known is not None and known.is_numeric
        reason = "contradictory_value" if numeric else "contradictory_choice"
        rejections.append(
            Rejection(
                source_text=", ".join(item.source_text for item in group),
                reason=reason,
                message=(
                    f"{template} is given as {' and as '.join(sorted(values))}; a part has one "
                    f"{template}, so neither is applied"
                ),
                template=template,
            )
        )
    return tuple(kept), tuple(rejections)


def _one_part_number(tokens: list[str]) -> tuple[str | None, Rejection | None]:
    unique = list(dict.fromkeys(tokens))
    if not unique:
        return None, None
    if len(unique) == 1:
        return unique[0], None
    return None, Rejection(
        source_text=", ".join(unique),
        reason="ambiguous_mpn",
        message=(
            f"{len(unique)} part-number-shaped tokens in one description "
            f"({', '.join(unique)}); a requirement names one part, so none is taken"
        ),
    )


def with_filters(
    requirement: Requirement,
    *,
    filters: tuple[RequirementFilter, ...],
    category: RequirementCategory | None,
    residue: tuple[str, ...],
    rejections: tuple[Rejection, ...],
    notes: tuple[str, ...],
) -> Requirement:
    """Rebuild a requirement with fields added. Used by `interpret.apply`.

    A named helper rather than a bare `dataclasses.replace` at the call site so
    the set of things a model is allowed to change is written down in one place:
    not the text, not the quantity, and not the part number.
    """
    return replace(
        requirement,
        filters=filters,
        category=category,
        residue=residue,
        rejections=rejections,
        notes=notes,
    )
