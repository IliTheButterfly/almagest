"""Asking a model which fields a datasheet has that this install does not.

The third module of the same shape as `extract.py` and `vision.py`: a request, a
JSON schema, a parser, a fake, and a transport in a sibling file. **None of the
model runs in this process** — ADR 0005, and the API is a single replica pinned to
an RWO volume that must not be holding a GPU. Everything here is verifiable with no
network, no model and no new dependency.

Where `extract.py` asks *what does this datasheet say about the fields I already
have*, this asks the question one level out: **which fields does it state that I
have no template for?** Both readings come off the same page of text and they are
deliberately different calls, because the answers are stored in different places
and only one of them is ever allowed to become data.

## It proposes definitions and can create nothing, structurally

There is no `parameter_template` writer anywhere in this module's import graph. Its
output is `ProposedField` dataclasses; the worker posts them to
`POST /api/field-proposals`; a person presses a button, which calls
`POST /api/parameter-fields`. That chain is asserted rather than described —
`tests/integration/test_field_proposals.py` runs a whole discovery pass through the
real parser and the real routes and checks the `parameter_template` count is
unchanged.

The reason is the **duplicate**, not the bad row. A `parameter_template` is shared
by every part in its category, so a second template for one concept fragments
parametric search silently: the filter is a join on `template_id`, and a part with
a value under `on_resistance` reads as having none under `Rds(on)`. A wrong
`base_unit` is the same harm from the other end — every `value_min`/`value_max`
ever written against it is wrong while still answering range queries. Neither
surfaces as an error, which is why neither is left to a model's judgement.

## The schema is where the rules become unrepresentable

Following `vision.schema_for`, and each absence below is load-bearing:

* **`category` is an `enum` of slugs that exist.** A model cannot invent a category
  id or a slug — `applies_to_category` is inherited down the taxonomy, so a
  fabricated slug would either match nothing (a field offered nowhere) or, worse,
  collide with a real one and offer the field on a subtree nobody chose.
* **`base_unit` is an `enum` of quantities the value parser actually answers to**,
  plus null. A numeric field whose unit the parser does not recognise accepts no
  value at all — `parameter_fields.validated_base_unit` is the guard, and this
  narrows the input to it rather than duplicating it.
* **`quote` is required, non-empty and bounded.** The same contract
  `ExtractedField.source_text` and `IdentityCandidate.source_text` carry: a
  proposal nobody can trace back to a line of a datasheet cannot be reviewed, and
  an unreviewable assertion is what this pipeline exists not to store.
* **There is no `substitution_direction` field.** It is what makes a 50 V part an
  acceptable stand-in for a 25 V one and not the reverse, and defaulting or
  guessing it produces a substitution search that is wrong by construction while
  looking like it works. `POST /api/parameter-fields` requires it with no default
  for exactly that reason, so the person filling the form supplies it.
* **`value_type` is `numeric` or `enum` and nothing else.**
  `app.services.parameters` has no writer for `text` or `bool`, so a field of
  either type could never hold a value — `target_fields` already drops them from
  extraction requests for the same reason.

## An empty answer is a normal answer

`FieldDiscoveryResult.proposals` may be empty, and on a well-covered category it
usually should be. "This datasheet states nothing you do not already have a field
for" is the correct reading of most sheets once the seed library is in place, and a
model pushed to answer anyway is a model inventing fields — which is the failure
this whole surface is arranged around.

## A name that already exists is *not* refused

The request lists the existing fields and asks for the ones that are missing, so a
proposal naming one back is the model ignoring an instruction. It is still recorded
rather than rejected, because the dedup check is what decides that question and it
decides it better: `field_proposals.nearest_existing_template` matches on a
normalised name, on display names and on a curated synonym list, so a proposal
comes back labelled `exact_name` and the surface says "this already exists" with
the template attached. Refusing it here would throw the quote away and lose the
one case that matters — the model proposing `Rds(on)` at an install that spells it
`on_resistance`, which is not an exact name match and is exactly the duplicate the
feature exists to catch.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.models.enums import ValueType
from app.services.enrichment.calls import CallStats

#: The value types a proposed field may have. Not every member of `ValueType`:
#: `app.services.parameters` has no writer for `text` or `bool`, so such a field
#: could be created and never filled in. `extract.target_fields` drops them from
#: extraction requests for the same reason.
PROPOSABLE_VALUE_TYPES: tuple[ValueType, ...] = (ValueType.NUMERIC, ValueType.ENUM)

#: Most fields one call may propose.
#:
#: Twelve, because a datasheet's parametric table has that order of rows and a
#: response longer than its own subject is a model padding. Not load-bearing — the
#: parser refuses more than this and nothing downstream depends on the number — but
#: it is sent as `maxItems`, so on a server that honours the schema a rambling
#: answer is truncated by the sampler rather than discarded after the GPU has
#: already been paid for.
DEFAULT_MAX_PROPOSALS = 12

#: Characters of the quote, matching `ParameterFieldProposalEvidence.quote` and
#: `extract.schema_for`'s bound on the same field, so a response the parser
#: accepted cannot fail to store.
MAX_QUOTE_CHARS = 500


class FieldDiscoveryResponseError(ValueError):
    """The provider's response does not satisfy the schema it was given.

    Raised rather than repaired, for the reason `ExtractionResponseError` gives: a
    response that half-parses is the one case where guessing what the model meant
    would put an invented field definition in front of a person with a quote
    attached that does not support it.
    """


class FieldDiscoveryFixtureMiss(LookupError):
    """`FakeFieldDiscoveryProvider` has no recorded response for that document.

    A miss raises instead of returning an empty result. Empty is a *meaningful*
    answer here — "this sheet needs no new fields" — so a fake that returned it on
    a missing fixture would let a test assert the nothing-to-propose path while
    actually testing a typo in a `document_ref`.
    """


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingField:
    """One field this install already has, as the prompt states it.

    Both spellings go in, not just `name`. The duplicate that actually happens is a
    datasheet's symbol against an inventory's word, and a model shown only
    `on_resistance` has less to recognise `Rds(on)` against than one shown
    "on_resistance (On resistance, ohm)".
    """

    name: str
    display_name: str
    value_type: str
    base_unit: str | None = None
    applies_to_category: str | None = None

    def describe(self) -> str:
        unit = f", {self.base_unit}" if self.base_unit else ""
        where = f" [{self.applies_to_category}]" if self.applies_to_category else ""
        return f"{self.name} ({self.display_name}, {self.value_type}{unit}){where}"


@dataclass(frozen=True)
class FieldDiscoveryRequest:
    """One call: one document's text, what already exists, and what may be named."""

    #: Content hash of the source PDF. Becomes the evidence row's `document_ref`,
    #: which is what makes a second pass over the same datasheet update its own
    #: sighting rather than inflate the seen-count.
    document_ref: str

    #: The extracted text, already through an `app.services.extractors.Extractor`.
    #: **Already discovered** — this pass reads what the extraction queue stored and
    #: fetches nothing.
    document_text: str

    #: Every field this install has. Sent so the model can be asked for what is
    #: *missing*, and never used to refuse a response — see the module docstring.
    existing: tuple[ExistingField, ...] = ()

    #: Category slugs the proposal may name, from `part_categories`. The schema
    #: constrains `category` to this list plus null, so a model cannot invent one.
    categories: tuple[str, ...] = ()

    #: Quantity names the value parser answers to, from
    #: `value_parser.supported_quantities()`. The schema constrains `base_unit` to
    #: this list plus null.
    base_units: tuple[str, ...] = ()

    max_proposals: int = DEFAULT_MAX_PROPOSALS

    #: Part numbers the document covers, for the prompt only. A datasheet read with
    #: no idea what it is about produces proposals about the wrong table.
    mpns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.document_ref:
            raise ValueError("document_ref is required: it is the evidence row's document_ref")
        if not self.document_text.strip():
            raise ValueError(
                "there is nothing to read. A document with no extracted text is a "
                "document for the extraction queue, not this one."
            )
        if self.max_proposals < 1:
            raise ValueError("max_proposals must be at least 1")


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedField:
    """One field a model claims the document states and this install lacks.

    No confidence. That is deliberate and it is not an oversight of the schema: a
    number here would be a self-report about whether a *definition* should exist,
    which is not a reading anything can check and not a quantity the promotion
    rules are calibrated against. What a reviewer judges on is the quote and how
    many documents produced one — both of which are facts.
    """

    name: str
    #: The line, cell or heading the field name was read from, verbatim.
    #: **Required.** See the module docstring.
    quote: str
    value_type: ValueType
    display_name: str | None = None
    base_unit: str | None = None
    #: A slug from `request.categories`, never a new one.
    category: str | None = None
    #: The value printed under the heading. The strongest hint about type and unit
    #: there is, and never written as a value — there is no template to write it
    #: against, and there will not be one until a person makes it.
    stated_value: str | None = None
    page: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class FieldDiscoveryResult:
    provider: str
    model: str
    document_ref: str
    proposals: tuple[ProposedField, ...] = field(default_factory=tuple)
    #: What the call cost. `None` from a fake or a replayed fixture, which is
    #: honest — a recorded response has no latency of its own.
    stats: CallStats | None = None


class FieldProposalProvider(Protocol):
    """What a field-discovery reader has to look like.

    Deliberately the whole abstraction, exactly as `ExtractionProvider` and
    `VisionProvider` are: a local model, a frontier API and the fake below all
    satisfy this with no change to the worker.
    """

    name: str
    model: str

    def propose(self, request: FieldDiscoveryRequest) -> FieldDiscoveryResult: ...


# ---------------------------------------------------------------------------
# The schema, and the parser that enforces it
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You read component datasheets and report which *parameters* the datasheet "
    "specifies that the inventory has no field for. You are proposing that a field "
    "should exist; you are not creating one and you are not filling one in. "
    "Report a parameter only if the datasheet states it as a specification with a "
    "value or a limit, and quote the line you read it from. If every parameter the "
    "datasheet specifies is already in the list of existing fields, return an empty "
    "array — that is the correct answer and a guess is not."
)


def schema_for(request: FieldDiscoveryRequest) -> dict[str, Any]:
    """The JSON schema the model's structured output is constrained to.

    Built from the request rather than written out as a constant, so `category` is
    an `enum` of slugs that exist and `base_unit` an `enum` of quantities the parser
    answers to. On a server that honours it, an invented category is
    **unrepresentable** rather than merely rejected. `parse_response` re-checks
    everything anyway: a server that quietly ignores `response_format` is exactly
    the one whose output nobody validated.

    Note what is *absent* — `substitution_direction`, and any field that could
    carry an id. See the module docstring; both absences are the design.
    """
    existing = "; ".join(row.describe() for row in request.existing)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["proposals"],
        "properties": {
            "proposals": {
                "type": "array",
                "maxItems": request.max_proposals,
                "description": (
                    "Parameters this datasheet specifies that are missing from the "
                    "inventory's fields. Return an empty array if there are none. "
                    "Fields that already exist: " + (existing or "(none)")
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "value_type", "quote"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": (
                                "A short machine name for the parameter, "
                                "lower_snake_case, no units in it: 'on_resistance', "
                                "not 'Rds(on) in mOhm'."
                            ),
                        },
                        "display_name": {
                            "type": ["string", "null"],
                            "maxLength": 255,
                            "description": (
                                "What a person should see in a filter panel, including "
                                "the datasheet's own symbol if it has one: "
                                "'On resistance Rds(on)'."
                            ),
                        },
                        "value_type": {
                            "type": "string",
                            "enum": [str(value) for value in PROPOSABLE_VALUE_TYPES],
                            "description": (
                                "'numeric' for anything with a unit; 'enum' for a fixed "
                                "set of named options such as a dielectric or a package."
                            ),
                        },
                        "base_unit": {
                            "type": ["string", "null"],
                            "enum": [*request.base_units, None],
                            "description": (
                                "For a numeric parameter, the name of its physical "
                                "quantity from this list — not a unit symbol and not a "
                                "prefixed unit. Null if it is not numeric or you are "
                                "unsure; a wrong quantity is worse than none, because "
                                "every value stored against it would then be wrong."
                            ),
                        },
                        "category": {
                            "type": ["string", "null"],
                            "enum": [*request.categories, None],
                            "description": (
                                "The part category this parameter belongs to, from this "
                                "list only. Null for something every part has. Do not "
                                "invent a category."
                            ),
                        },
                        "quote": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_QUOTE_CHARS,
                            "description": (
                                "The heading, cell or sentence in the datasheet that "
                                "states this parameter, verbatim. Required. If you "
                                "cannot quote it, you did not read it."
                            ),
                        },
                        "stated_value": {
                            "type": ["string", "null"],
                            "maxLength": 255,
                            "description": (
                                "The value or limit printed for it, with its unit as "
                                "printed: '45 mOhm', '2.5 V max'."
                            ),
                        },
                        "page": {"type": ["integer", "null"], "minimum": 1},
                        "note": {
                            "type": ["string", "null"],
                            "maxLength": 500,
                            "description": (
                                "Anything a person deciding whether to create this "
                                "field should know."
                            ),
                        },
                    },
                },
            }
        },
    }


def parse_response(
    payload: object,
    request: FieldDiscoveryRequest,
    *,
    provider: str,
    model: str,
    stats: CallStats | None = None,
) -> FieldDiscoveryResult:
    """Validate a raw response into a `FieldDiscoveryResult`, or raise.

    Everything `schema_for` was supposed to guarantee is checked again, and the two
    checks that are not merely re-statements of it are the category and the
    duplicate name: a server that ignored `response_format` can emit a slug that
    does not exist, and a proposal for a category nobody has is a field offered
    nowhere.
    """
    body = _object(payload, "response")
    raw = body.get("proposals")
    if not isinstance(raw, list):
        raise FieldDiscoveryResponseError("response.proposals must be an array")
    if len(raw) > request.max_proposals:
        raise FieldDiscoveryResponseError(
            f"response.proposals holds {len(raw)} entries; at most "
            f"{request.max_proposals} were asked for"
        )

    allowed_types = {str(value) for value in PROPOSABLE_VALUE_TYPES}
    allowed_categories = set(request.categories)
    allowed_units = set(request.base_units)
    seen: set[str] = set()
    proposals: list[ProposedField] = []

    for index, entry in enumerate(raw):
        where = f"proposals[{index}]"
        item = _object(entry, where)
        name = _text(item, "name", where)
        folded = name.casefold()
        if folded in seen:
            # Two entries for one field would be two `record_proposal` calls that
            # upsert the same row, so the second would silently overwrite the
            # first's quote and the response's own disagreement would vanish. It is
            # a malformed response, the same call `parse_response` makes in
            # `extract.py` for a repeated part number.
            raise FieldDiscoveryResponseError(f"{where}.name {name!r} appears more than once")
        seen.add(folded)

        value_type = _text(item, "value_type", where)
        if value_type not in allowed_types:
            raise FieldDiscoveryResponseError(
                f"{where}.value_type {value_type!r} is not one of {sorted(allowed_types)}"
            )

        base_unit = _optional_text(item, "base_unit", where)
        if base_unit is not None and base_unit not in allowed_units:
            raise FieldDiscoveryResponseError(
                f"{where}.base_unit {base_unit!r} is not a quantity the value parser "
                f"recognises, so no value could ever be entered against this field"
            )

        category = _optional_text(item, "category", where)
        if category is not None and category not in allowed_categories:
            raise FieldDiscoveryResponseError(
                f"{where}.category {category!r} is not an existing part category; "
                f"asked for one of {sorted(allowed_categories)} or null"
            )

        quote = _text(item, "quote", where)
        if len(quote) > MAX_QUOTE_CHARS:
            raise FieldDiscoveryResponseError(
                f"{where}.quote is {len(quote)} characters; at most {MAX_QUOTE_CHARS}"
            )

        proposals.append(
            ProposedField(
                name=name,
                quote=quote,
                value_type=ValueType(value_type),
                display_name=_optional_text(item, "display_name", where),
                base_unit=base_unit,
                category=category,
                stated_value=_optional_text(item, "stated_value", where),
                page=_page(item, where),
                note=_optional_text(item, "note", where),
            )
        )

    return FieldDiscoveryResult(
        provider=provider,
        model=model,
        document_ref=request.document_ref,
        proposals=tuple(proposals),
        stats=stats,
    )


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FieldDiscoveryResponseError(f"{where} must be an object, got {type(value).__name__}")
    return value


def _text(body: dict[str, Any], key: str, where: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FieldDiscoveryResponseError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _optional_text(body: dict[str, Any], key: str, where: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise FieldDiscoveryResponseError(f"{where}.{key} must be a string or null")
    stripped = value.strip()
    # An empty string is treated as absent rather than refused: a model filling an
    # optional field with "" is saying nothing, and there is no information to lose.
    # `quote` goes through `_text` instead, precisely because there it is a refusal.
    return stripped or None


def _page(body: dict[str, Any], where: str) -> int | None:
    value = body.get("page")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FieldDiscoveryResponseError(f"{where}.page must be an integer or null")
    return value


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


class FakeFieldDiscoveryProvider:
    """Replays a response recorded once from a real one. No network, no model.

    The fixture holds the provider's **raw JSON**, not this module's dataclasses,
    and the fake runs it through the same `parse_response` a real provider's output
    goes through. That is the whole point: a fake returning pre-built objects would
    exercise none of the parsing, so the refusals above — an invented category, a
    missing quote, a unit the parser does not know — would be tested only against
    hand-written inputs and never against the shape a model actually emits.

    Fixture format::

        {"provider": "...", "model": "...",
         "responses": {"<document_ref>": {"proposals": [...]}}}
    """

    def __init__(self, fixture_path: Path) -> None:
        body = _object(json.loads(fixture_path.read_text(encoding="utf-8")), "fixture")
        self.name = str(body.get("provider", "fake"))
        self.model = str(body.get("model", "fake"))
        self._responses = _object(body.get("responses"), "fixture.responses")
        self.calls: list[FieldDiscoveryRequest] = []

    def propose(self, request: FieldDiscoveryRequest) -> FieldDiscoveryResult:
        self.calls.append(request)
        if request.document_ref not in self._responses:
            raise FieldDiscoveryFixtureMiss(
                f"no recorded response for document_ref {request.document_ref!r}; "
                f"recorded: {sorted(self._responses)}"
            )
        return parse_response(
            self._responses[request.document_ref],
            request,
            provider=self.name,
            model=self.model,
        )


def request_for(
    *,
    document_ref: str,
    document_text: str,
    existing: Sequence[ExistingField],
    categories: Sequence[str],
    base_units: Sequence[str],
    mpns: Sequence[str] = (),
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
) -> FieldDiscoveryRequest:
    """The ordinary constructor. Takes plain sequences and needs no `Session`.

    Deliberately no database access: the worker holds no session (ADR 0005 — two
    SQLite writers is corruption), so the vocabulary arrives over HTTP from
    `GET /api/parameter-fields`, `GET /api/part-categories` and
    `GET /api/parameter-fields/base-units`.
    """
    return FieldDiscoveryRequest(
        document_ref=document_ref,
        document_text=document_text,
        existing=tuple(existing),
        categories=tuple(categories),
        base_units=tuple(base_units),
        mpns=tuple(mpns),
        max_proposals=max_proposals,
    )
