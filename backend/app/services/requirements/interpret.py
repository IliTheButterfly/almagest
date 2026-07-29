"""The model seam: the residue in, **filters** out. Never an answer, never a part.

`DeterministicRequirementParser` reads most real lines outright and lists what it
could not (`Requirement.residue`). This module is the interface for that
remainder, and its whole job is to produce the *middle* term:

    prose  ->  [ here ]  ->  a structured Requirement  ->  the filter executor

A model is asked "which of this install's parameters does `dual op-amp,
rail-to-rail` correspond to". It is never asked "which part should I use". If it
were, `substitution_direction` would have been replaced by a guess, and a
plausible substitute with the wrong voltage rating is a field failure — so only
the SQL filter decides, always.

## Three refusals that are the point of the module

1. **A part number is unrepresentable.** There is no `mpn` in the schema, unknown
   keys are refused, and the part-number-ish spellings are refused *by name* with
   a pointed message. `CLAUDE.md`: never auto-accept a model-read part number — a
   wrong-but-confident part ID is worse than "unknown". A model reading `LM358N`
   out of prose looks harmless right up to the point where the wrong `LM358` gets
   soldered in, so the answer shape simply cannot express it.
2. **A parameter the request did not ask for is refused**, exactly as
   `enrichment.extract.parse_response` refuses one. A model must never introduce a
   parameter, and a schema-constrained decode plus this check means it cannot.
3. **A value the vocabulary cannot hold is refused**, by parsing it here: an enum
   answer must resolve to a real `parameter_choice` spelling and is canonicalised
   to its key, and a numeric answer must survive the template's own plausibility
   window. Passing an unvalidated string down to the executor would turn a bad
   answer into a 400 two stages later, with nothing saying which stage invented it.

And one rule at the merge: **the grammar wins.** `apply_interpretation` never
overwrites a deterministically-established field. A model contradicting the value
grammar is recorded (`model_contradicted_grammar`) and discarded, because `100nF`
is not a matter of opinion.

## No provider is built here

Per `docs/adr/0005` and the deployment notes, the real implementation targets the
**local Qwen models and runs as a Job that releases the GPU** — never in the API.
The API process is a single replica pinned to an RWO SQLite volume, and on a
co-tenanted GPU host a free device is a race, not a reservation. So what ships is
this interface plus `FakeInterpreter`, which replays a recorded fixture through
the same `parse_interpretation` a real provider's output goes through, and one
`@pytest.mark.live` contract test skipped by default. That is the established
pattern here (`enrichment.extract`, `services.extractors`, `deviceagent`'s
`TagSource`), and it is what keeps CI offline and model-free.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Protocol

from elec_value_parser import ValueParseError

from app.models.enums import ValueType
from app.services.enrichment.extract import TargetField
from app.services.requirements.parser import (
    FieldOrigin,
    Rejection,
    Requirement,
    RequirementCategory,
    RequirementFilter,
    with_filters,
    without_contradictions,
)
from app.services.requirements.vocabulary import Vocabulary, phrase_key

#: A model's self-report is not evidence, so an interpreted field is not allowed
#: to present as grammar-certain. `Requirement.confidence` is the weakest field,
#: which means an uncapped 1.0 here would let a model make a whole mixed-origin
#: line look exact.
MAX_INTERPRETED_CONFIDENCE = 0.9

#: Keys refused by name rather than merely by "unknown key", so the error says
#: *why* instead of reading as a typo. Every one of these is an attempt to make
#: the model name a part, which is the thing it may never do.
_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "mpn",
        "part",
        "part_id",
        "part_number",
        "partnumber",
        "manufacturer_part_number",
        "sku",
    }
)


class InterpretationError(ValueError):
    """The model's answer does not satisfy the shape it was given.

    Raised rather than repaired, for the same reason
    `enrichment.extract.ExtractionResponseError` is: an answer that half-parses is
    exactly when guessing what was meant puts an invented predicate into a search
    with a real confidence attached to it.
    """


class InterpretationFixtureMiss(LookupError):
    """`FakeInterpreter` has no recorded answer for that description.

    Raises rather than returning an empty interpretation, which would be
    indistinguishable from "the model had nothing to add" and would let a test
    pass while interpreting nothing at all.
    """


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterpretationRequest:
    """One call: the description, what the grammar already settled, and the words left.

    Carries the `Vocabulary` because that *is* the legal answer space — the
    templates, their units and plausibility windows, and every curated spelling of
    every choice. Validation and prompt-building both read it, so passing it
    alongside would be two chances to disagree about what a legal answer is.
    """

    text: str
    vocabulary: Vocabulary
    #: The words nothing accounted for. **Not the rejections**: a value the
    #: grammar read and refused is a review item, and the only thing a model could
    #: do with `1M` under farads is talk us out of a correct refusal.
    residue: tuple[str, ...] = ()
    #: What the grammar already fixed, as (template, value). Sent so the model
    #: does not spend its answer re-deriving `100nF`, and so a contradiction is
    #: visible as one.
    established: tuple[tuple[str, str], ...] = ()
    established_category: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("an interpretation request needs the description text")
        if not self.residue:
            raise ValueError(
                "an interpretation request needs residue: with nothing unaccounted for "
                "there is nothing for a model to add, and asking anyway invites it to "
                "second-guess the grammar"
            )

    @cached_property
    def fields(self) -> tuple[TargetField, ...]:
        """The answer shape, reusing `enrichment.extract.TargetField`.

        Deliberately the same type the datasheet extractor asks in. Both are "one
        parameter a model is asked for, and what a legal answer looks like", and a
        near-copy would have drifted on the detail that matters — that numerics
        are requested as a string *with the unit in it*, so the one value grammar
        populates the interval rather than this module re-assembling one.
        """
        fields: list[TargetField] = []
        for template in self.vocabulary.templates:
            if template.value_type is ValueType.NUMERIC:
                fields.append(
                    TargetField(
                        name=template.name,
                        value_type=ValueType.NUMERIC,
                        unit=template.base_unit,
                    )
                )
            elif template.value_type is ValueType.ENUM:
                spellings: list[str] = []
                for choice in template.choices:
                    spellings.extend(choice.spellings)
                fields.append(
                    TargetField(
                        name=template.name,
                        value_type=ValueType.ENUM,
                        choices=tuple(dict.fromkeys(spellings)),
                    )
                )
        return tuple(fields)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @property
    def category_slugs(self) -> tuple[str, ...]:
        return tuple(category.slug for category in self.vocabulary.categories)


def request_for_residue(
    requirement: Requirement, vocabulary: Vocabulary
) -> InterpretationRequest | None:
    """The request for what the grammar could not read, or **None**.

    None means "the deterministic pass was enough" — the ordinary case, and the
    reason this is a function rather than a constructor call at each site. A
    caller that skips the check would send a model every line, which costs money,
    latency and honesty (an interpreted field downgrades the whole requirement)
    for lines that were already exact.
    """
    if not requirement.residue:
        return None
    return InterpretationRequest(
        text=requirement.text,
        vocabulary=vocabulary,
        residue=requirement.residue,
        established=tuple((item.template, item.value) for item in requirement.filters),
        established_category=requirement.category_slug,
    )


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterpretedFilter:
    """One predicate a model proposes, and the words it read it from."""

    template: str
    #: Already canonicalised by `parse_interpretation`: a choice key, or a value
    #: string the template's own window accepted.
    value: str
    confidence: float
    #: Which words of the description this came from. **Required**, exactly as in
    #: `enrichment.extract.ExtractedField`: an untraceable claim cannot be
    #: reviewed, and every claim from a model is going to be reviewed.
    source_text: str


@dataclass(frozen=True)
class Interpretation:
    provider: str
    model: str
    filters: tuple[InterpretedFilter, ...] = ()
    category_slug: str | None = None
    category_confidence: float | None = None
    category_source_text: str | None = None


class RequirementInterpreter(Protocol):
    """What an interpreter has to look like. Residue in, candidate filters out.

    Deliberately the whole abstraction. A local Qwen worker, a frontier API and
    the fake below all satisfy it, and nothing downstream changes for any of them
    — which is what makes "the real one is not built yet" a seam rather than a
    hole.
    """

    name: str
    model: str

    def interpret(self, request: InterpretationRequest) -> Interpretation: ...


def schema_for(request: InterpretationRequest) -> dict[str, Any]:
    """The JSON schema the model's structured output is constrained to.

    Built from the request, so `template` is an `enum` of exactly this install's
    parameters and `category` an `enum` of exactly its categories. A
    schema-constrained decode then makes an invented parameter — or a part number
    — *unrepresentable* rather than merely rejected, and `parse_interpretation`
    still rejects both, because a provider that ignores the schema is precisely
    the drift the live contract test exists to catch.
    """
    guide = "; ".join(field.describe() for field in request.fields)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["filters"],
        "description": (
            "Translate the description into this catalogue's own parameters. "
            "Answer only for what you are sure of; omitting a field is always "
            "better than guessing one. Do not name, guess or invent a "
            "manufacturer part number — there is no field for one, and a part is "
            "chosen by search over these parameters, never by you."
        ),
        "properties": {
            "category": {
                "type": ["string", "null"],
                "enum": [*request.category_slugs, None],
                "description": (
                    "The kind of part being described, if the description says. "
                    + (
                        f"Already established as {request.established_category!r}; "
                        "do not contradict it."
                        if request.established_category
                        else ""
                    )
                ),
            },
            "category_confidence": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "Required whenever category is given. A category is the field most "
                    "often guessed, so it carries its own number rather than the "
                    "answer's average."
                ),
            },
            "category_source_text": {
                "type": ["string", "null"],
                "description": "The words the category was read from, verbatim.",
            },
            "filters": {
                "type": "array",
                "description": (
                    "One entry per parameter you can read from the description. "
                    "These words are the ones nothing could account for: "
                    + ", ".join(request.residue)
                    + (
                        ". Already established, do not repeat or contradict: "
                        + ", ".join(f"{name}={value}" for name, value in request.established)
                        if request.established
                        else ""
                    )
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["template", "value", "confidence", "source_text"],
                    "properties": {
                        "template": {
                            "type": "string",
                            "enum": list(request.field_names),
                            "description": guide,
                        },
                        "value": {"type": "string", "minLength": 1},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": (
                                "How sure you are the description asks for this. "
                                "Guessing is worse than omitting the field."
                            ),
                        },
                        "source_text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": (
                                "The words of the description this was read from, verbatim. "
                                "Required."
                            ),
                        },
                    },
                },
            },
        },
    }


def parse_interpretation(
    payload: object, request: InterpretationRequest, *, provider: str, model: str
) -> Interpretation:
    """Validate a raw answer into an `Interpretation`, or raise."""
    body = _object(payload, "answer")
    _refuse_unknown_keys(
        body,
        "answer",
        allowed={"category", "category_confidence", "category_source_text", "filters"},
    )

    raw_filters = body.get("filters")
    if not isinstance(raw_filters, list):
        raise InterpretationError("answer.filters must be an array")

    allowed_templates = set(request.field_names)
    seen: set[str] = set()
    filters: list[InterpretedFilter] = []
    for index, entry in enumerate(raw_filters):
        where = f"answer.filters[{index}]"
        item = _object(entry, where)
        _refuse_unknown_keys(
            item, where, allowed={"template", "value", "confidence", "source_text"}
        )
        name = _text(item, "template", where)
        if name not in allowed_templates:
            raise InterpretationError(
                f"{where}.template {name!r} was not asked for; asked for "
                f"{sorted(allowed_templates)}"
            )
        if name in seen:
            # Two answers for one parameter is the model contradicting itself, and
            # there is no basis for preferring either.
            raise InterpretationError(f"{where}.template {name!r} is answered twice")
        seen.add(name)
        filters.append(
            InterpretedFilter(
                template=name,
                value=_canonical_value(name, _text(item, "value", where), request, where),
                confidence=min(_confidence(item, "confidence", where), MAX_INTERPRETED_CONFIDENCE),
                source_text=_text(item, "source_text", where),
            )
        )

    slug = _optional_text(body, "category", "answer")
    if slug is not None and slug not in request.category_slugs:
        raise InterpretationError(
            f"answer.category {slug!r} is not a category of this catalogue; known "
            f"{sorted(request.category_slugs)}"
        )

    confidence = (
        min(_confidence(body, "category_confidence", "answer"), MAX_INTERPRETED_CONFIDENCE)
        if slug is not None
        else None
    )
    return Interpretation(
        provider=provider,
        model=model,
        filters=tuple(filters),
        category_slug=slug,
        category_confidence=confidence,
        category_source_text=_optional_text(body, "category_source_text", "answer"),
    )


def _canonical_value(
    template_name: str, raw_value: str, request: InterpretationRequest, where: str
) -> str:
    """The answer, in the form `query_builder` accepts — or a refusal.

    An enum becomes its `parameter_choice.key`, so the executor's `resolve_choice`
    cannot fail on it downstream; a numeric is parsed against the template's own
    plausibility window and then kept **as text**, because the interval belongs to
    the one value grammar and nothing here recomputes it.
    """
    template = request.vocabulary.template(template_name)
    if template is None:  # pragma: no cover — field_names comes from the vocabulary
        raise InterpretationError(f"{where}.template {template_name!r} is not in the vocabulary")

    if template.value_type is ValueType.ENUM:
        wanted = phrase_key(raw_value)
        for match in request.vocabulary.choices_for(wanted):
            if match.template == template_name:
                return match.key
        raise InterpretationError(
            f"{where}.value {raw_value!r} is not a choice of {template_name}; known "
            f"{sorted(choice.key for choice in template.choices)}"
        )

    try:
        template.parse(raw_value)
    except ValueParseError as error:
        raise InterpretationError(
            f"{where}.value {raw_value!r} is not a legal {template_name} ({error.reason}): {error}"
        ) from error
    return raw_value


def _refuse_unknown_keys(body: dict[str, Any], where: str, *, allowed: set[str]) -> None:
    for key in body:
        if key in allowed:
            continue
        if key.casefold().replace(" ", "_") in _FORBIDDEN_KEYS:
            raise InterpretationError(
                f"{where}.{key} is refused outright: an interpreter may not name a part. "
                "A model-read part number is never auto-accepted here — it produces a "
                "requirement, and the catalogue is searched deterministically for what "
                "satisfies it."
            )
        raise InterpretationError(f"{where}.{key} is not part of the answer shape")


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InterpretationError(f"{where} must be an object, got {type(value).__name__}")
    return value


def _text(body: dict[str, Any], key: str, where: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InterpretationError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _optional_text(body: dict[str, Any], key: str, where: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InterpretationError(f"{where}.{key} must be a non-empty string or null")
    return value.strip()


def _confidence(body: dict[str, Any], key: str, where: str) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InterpretationError(f"{where}.{key} must be a number")
    if not 0.0 <= float(value) <= 1.0:
        raise InterpretationError(f"{where}.{key} must be between 0 and 1, got {value}")
    return float(value)


# ---------------------------------------------------------------------------
# Merging — and the rule that the grammar wins
# ---------------------------------------------------------------------------


def apply_interpretation(
    requirement: Requirement, interpretation: Interpretation, vocabulary: Vocabulary
) -> Requirement:
    """Fold a model's reading into a requirement, without letting it overrule one.

    Three rules, and each is a refusal:

    * a template the grammar already read is **not** overwritten. If the model
      disagrees, that is recorded as `model_contradicted_grammar` and dropped —
      `100nF` is not a matter of opinion, and the whole architecture rests on the
      deterministic reading being the one that survives.
    * a category the grammar already established is not overwritten either.
    * `text`, `quantity`, `mpn` and `mpn_norm` come through untouched, because
      nothing in an interpretation can address them.

    Residue shrinks to the words no accepted answer quoted, so a line the model
    fully explained ends up complete, and one it only partly explained still says
    which words are outstanding.
    """
    established = {item.template for item in requirement.filters}
    filters = list(requirement.filters)
    rejections = list(requirement.rejections)
    notes = list(requirement.notes)
    quoted: list[str] = []

    for item in interpretation.filters:
        if item.template in established:
            existing = next(one for one in filters if one.template == item.template)
            if existing.value != item.value:
                rejections.append(
                    Rejection(
                        source_text=item.source_text,
                        reason="model_contradicted_grammar",
                        message=(
                            f"{interpretation.model} read {item.template} as {item.value!r} from "
                            f"{item.source_text!r}, but the description states "
                            f"{existing.value!r} in {existing.source_text!r}; the grammar wins"
                        ),
                        template=item.template,
                    )
                )
            continue
        filters.append(
            RequirementFilter(
                template=item.template,
                value=item.value,
                source_text=item.source_text,
                origin=FieldOrigin.INTERPRETED,
                confidence=item.confidence,
            )
        )
        quoted.append(item.source_text)

    category = requirement.category
    if interpretation.category_slug is not None:
        if category is not None and category.slug != interpretation.category_slug:
            rejections.append(
                Rejection(
                    source_text=interpretation.category_source_text or requirement.text,
                    reason="model_contradicted_grammar",
                    message=(
                        f"{interpretation.model} read the category as "
                        f"{interpretation.category_slug}, but the description says "
                        f"{category.slug}; the grammar wins"
                    ),
                )
            )
        elif category is None:
            category = RequirementCategory(
                slug=interpretation.category_slug,
                source_text=interpretation.category_source_text or requirement.text,
                origin=FieldOrigin.INTERPRETED,
                confidence=interpretation.category_confidence or MAX_INTERPRETED_CONFIDENCE,
            )
            if interpretation.category_source_text:
                # Only a quoted source clears residue. Falling back to the whole
                # description would account for every word in it and make an
                # unexplained line look complete.
                quoted.append(interpretation.category_source_text)

    notes.append(f"{len(interpretation.filters)} field(s) proposed by {interpretation.model}")
    kept, contradictions = without_contradictions(filters, vocabulary)
    return with_filters(
        requirement,
        filters=kept,
        category=category,
        residue=_remaining(requirement.residue, quoted),
        rejections=(*rejections, *contradictions),
        notes=tuple(notes),
    )


def _remaining(residue: Sequence[str], quoted: Sequence[str]) -> tuple[str, ...]:
    """Residue words no accepted answer quoted.

    Word-level rather than trusting the model to report what it could not do: a
    self-reported "unresolved" list is one more thing that can be wrong, and the
    `source_text` every accepted answer already had to supply says the same thing
    without being asked.
    """
    accounted = {word for text in quoted for word in phrase_key(text).split(" ")}
    return tuple(token for token in residue if phrase_key(token) not in accounted)


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


class FakeInterpreter:
    """Replays a recorded answer. No model, no network, no GPU.

    The fixture holds a provider's **raw JSON**, not this module's dataclasses,
    and the fake runs it through the same `parse_interpretation` a real provider's
    output goes through. A fake returning pre-built objects would exercise none of
    the validation, so the refusals above — an invented parameter, a smuggled part
    number, a value no `parameter_choice` spells — would be tested only against
    hand-written inputs and never against the shape an answer actually arrives in.

    Fixture format::

        {"provider": "...", "model": "...",
         "answers": {"<description, verbatim>": {"filters": [...]}}}

    Keyed by the description so one file holds a corpus, and so a test that builds
    the wrong request gets `InterpretationFixtureMiss` rather than another line's
    answer.
    """

    def __init__(self, fixture_path: Path) -> None:
        body = _object(json.loads(fixture_path.read_text(encoding="utf-8")), "fixture")
        self.name = str(body.get("provider", "fake"))
        self.model = str(body.get("model", "fake"))
        self._answers = _object(body.get("answers"), "fixture.answers")
        self.calls: list[InterpretationRequest] = []

    def interpret(self, request: InterpretationRequest) -> Interpretation:
        self.calls.append(request)
        if request.text not in self._answers:
            raise InterpretationFixtureMiss(
                f"no recorded answer for {request.text!r}; recorded: {sorted(self._answers)}"
            )
        return parse_interpretation(
            self._answers[request.text], request, provider=self.name, model=self.model
        )
