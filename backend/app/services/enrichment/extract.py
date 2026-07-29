"""The structured-extraction seam: a request, a JSON schema, a parser, a fake.

`docs/PLAN.md`'s pipeline tail is `fetch -> Docling -> LLM structured extraction
against a JSON schema -> MPN-decoder cross-check -> confidence score -> review
queue below 0.8 or on disagreement`. **None of the model runs in this process.**
Docling and any local inference stack are hundreds of megabytes of wheels, and
the GPU is a race rather than a reservation — the API is a single replica pinned
to an RWO volume and must not be holding a device. The extraction pass is a Job
that reads a datasheet and hands the result to `cross_check.ingest`.

So what lives here is the *interface*, and every line of it is verifiable with no
network, no model and no new dependency:

* `ExtractionRequest` — a **document** plus the part numbers to find in it and the
  fields to look for.
* `schema_for()` — the JSON schema the model's structured output is held to.
* `parse_response()` — the response back to typed values, refusing anything
  malformed. Shared by the fake and by any real provider, which is the only
  reason the fake is worth having.
* `FakeExtractionProvider` — replays a recorded response, per `docs/PLAN.md`:
  "every provider has a `Fake*Provider` replaying a JSON fixture recorded once
  from a real response, plus one `@pytest.mark.live` contract test skipped by
  default".

## The batch is the unit, not the part

There is no `extract_one`. `ExtractionRequest.mpns` is a tuple and
`ExtractionResult.variants` is keyed by part number, because the thing a
datasheet actually contains is a **variant table**: one PDF covers GRM188 in
every capacitance, voltage and tolerance the family ships. Extracting it in one
call is what makes the cost trivial (`docs/PLAN.md`: ~$0.0005–0.001 per part
batched, under $2 for a 1000-part backfill) and is also the only way the model
sees the table's own structure — asked for one row at a time it has to re-read
the header every time, which is exactly when it picks the wrong row.

A single-part extraction is a batch of one. Had the interface been per-part with
a batch wrapper bolted on, the wrapper would have been the untested path.

## What `parse_response` refuses, and what it deliberately does not

Refused, because the response is malformed: a field naming a template the request
did not ask for (a model must never introduce a parameter), a confidence outside
`[0, 1]`, an empty `raw_value`, two rows for one part number and field, and —
importantly — **a value with no `source_text`**. A value nobody can trace back to
a line of the datasheet cannot be reviewed, and an unreviewable assertion is
precisely what this pipeline exists not to store.

*Not* refused: a variant whose part number was not in `request.mpns`. That is not
a malformed response, it is the model reading a part number off the page — a
normal and even useful event, and one that must never be accepted. It is refused
one layer up, in `cross_check.ingest`, where identity is matched deterministically
against the catalogue. See that module.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate
from app.services import parameters
from app.services.scanning.codes import normalize_mpn

#: Most part numbers to put in one call.
#:
#: Not load-bearing — `chunk()` makes a request of any size legal, and the
#: correctness of everything below is independent of it. It exists because a
#: 400-variant family does not fit in a modest context window, and because one
#: malformed response should spoil a couple of dozen parts rather than a whole
#: manufacturer series.
MAX_BATCH_MPNS = 24


class ExtractionResponseError(ValueError):
    """The provider's response does not satisfy the schema it was given.

    Raised rather than repaired. A response that half-parses is the one case
    where guessing what the model meant would put an invented value into the
    candidate table with a real confidence attached to it.
    """


class FixtureMiss(LookupError):
    """`FakeExtractionProvider` has no recorded response for that document.

    A miss raises instead of returning an empty result, because an empty result
    is indistinguishable from "the datasheet contained nothing" and would let a
    test pass while extracting nothing at all.
    """


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetField:
    """One parameter the model is asked to find, and the shape of a legal answer.

    Numerics are requested **as strings with their unit in them** — `"100 nF"`,
    `"50 V"`, `"100 nF ±10%"` — and never as a bare number plus a separate unit
    field. That is the same contract `mpn_decoders.DecodedPart` uses and for the
    same reason: the string is parsed by the one value grammar, which is what
    populates `value_min`/`value_max`. A response carrying `{"value": 100,
    "unit": "nF"}` would need this module to re-assemble it, and a re-assembly
    bug produces a plausible number that is silently invisible to range search.
    """

    #: `parameter_template.name`.
    name: str
    value_type: ValueType
    #: The template's base unit, for the prompt only. The model is not required
    #: to answer in it — `set_numeric` converts.
    unit: str | None = None
    #: Legal answers for an enum field: `parameter_choice.key`, plus every
    #: alias, so a datasheet saying `1608` is not forced to say `0603`.
    choices: tuple[str, ...] = ()

    def describe(self) -> str:
        """One line of prompt text. Also the schema's per-field description."""
        if self.value_type is ValueType.ENUM:
            return f"{self.name}: exactly one of {', '.join(self.choices)}"
        unit = f", canonically {self.unit}" if self.unit else ""
        return (
            f"{self.name}: a number with its unit included{unit}"
            f" — a tolerance may be appended, e.g. '100 nF ±10%'"
        )


def target_fields(
    session: Session, templates: Sequence[ParameterTemplate]
) -> tuple[TargetField, ...]:
    """Turn the templates a caller cares about into the answer shape.

    Enum choices are read from the database rather than hard-coded, because
    `parameter_choice` is user-curated content: an install that added a
    dielectric gets it in the schema, and the model is never invited to answer
    with a facet this install cannot store.

    `text` and `bool` templates are **dropped**, not requested. `app.services
    .parameters` has no writer for them, so `candidates.record` refuses them —
    asking a model for a value that could never be recorded would spend tokens
    to manufacture review-queue items nobody can action.
    """
    fields: list[TargetField] = []
    for template in templates:
        value_type = parameters.value_type_of(template)
        if value_type not in (ValueType.NUMERIC, ValueType.ENUM):
            continue
        choices: tuple[str, ...] = ()
        if value_type is ValueType.ENUM:
            rows = session.execute(
                select(ParameterChoice)
                .where(ParameterChoice.template_id == template.id)
                .order_by(ParameterChoice.sort_order, ParameterChoice.id)
            ).scalars()
            keys: list[str] = []
            for row in rows:
                keys.append(row.key)
                keys.extend(sorted(_alias_keys(row)))
            choices = tuple(dict.fromkeys(keys))
        fields.append(
            TargetField(
                name=template.name,
                value_type=value_type,
                unit=template.base_unit,
                choices=choices,
            )
        )
    return tuple(fields)


def _alias_keys(choice: ParameterChoice) -> set[str]:
    if not choice.aliases_json:
        return set()
    loaded = json.loads(choice.aliases_json)
    return {str(alias) for alias in loaded} if isinstance(loaded, list) else set()


@dataclass(frozen=True)
class ExtractionRequest:
    """One call: one document, the part numbers wanted from it, the fields to find."""

    #: Content hash of the source PDF. Becomes `parameter_value_candidate
    #: .source_ref`, which is what makes a nightly re-run update its own rows
    #: instead of accumulating a new observation every night — and what makes a
    #: *revised* datasheet a second, comparable observation rather than a silent
    #: overwrite of the first.
    document_ref: str

    #: The extracted text, already through Docling (or pdfplumber + tesseract on
    #: a scanned sheet, which is itself the signal to distrust the result).
    #: Optionally sliced to the section covering `mpns`.
    document_text: str

    #: Every part number to look for. The caller's list, from the catalogue —
    #: never the model's.
    mpns: tuple[str, ...]

    fields: tuple[TargetField, ...]

    def __post_init__(self) -> None:
        if not self.document_ref:
            raise ValueError("document_ref is required: it is the candidate's source_ref")
        if not self.mpns:
            raise ValueError("an extraction request needs at least one part number")
        if not self.fields:
            raise ValueError("an extraction request needs at least one target field")

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)


def chunk(request: ExtractionRequest, size: int = MAX_BATCH_MPNS) -> Iterator[ExtractionRequest]:
    """Split a request whose part list is too long for one call.

    The document text is **not** split with it. Slicing the text per chunk is a
    heuristic that can cut a variant table's header away from its rows, and a
    table without its header is the exact input that makes a model pick the
    wrong column.
    """
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    for start in range(0, len(request.mpns), size):
        yield ExtractionRequest(
            document_ref=request.document_ref,
            document_text=request.document_text,
            mpns=request.mpns[start : start + size],
            fields=request.fields,
        )


def request_for(
    session: Session,
    *,
    document_ref: str,
    document_text: str,
    mpns: Sequence[str],
    templates: Sequence[ParameterTemplate],
) -> ExtractionRequest:
    """The ordinary constructor: everything sharing one datasheet, in one request."""
    return ExtractionRequest(
        document_ref=document_ref,
        document_text=document_text,
        mpns=tuple(mpns),
        fields=target_fields(session, templates),
    )


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedField:
    """One value the model claims the document states, and where it read it."""

    template_name: str
    #: Verbatim, in the document's own units and spelling. Normalisation happens
    #: once, in `candidates.record`.
    raw_value: str
    #: The model's own confidence, before the cross-check adjusts it.
    confidence: float
    #: The line, cell or sentence the value was read from. **Required.** This is
    #: what a reviewer reads instead of taking the model's word, and it is
    #: carried into `parameter_value_candidate.note` so it survives in the queue
    #: rather than only in a worker log.
    source_text: str
    page: int | None = None


@dataclass(frozen=True)
class ExtractedVariant:
    """One part number's row of the table."""

    mpn: str
    #: Where the part number itself was read. Carried into the refusal when the
    #: number matches nothing in the catalogue, so a human can judge whether the
    #: model found a real sibling variant or invented one.
    mpn_source_text: str
    fields: tuple[ExtractedField, ...]

    def field(self, template_name: str) -> ExtractedField | None:
        for row in self.fields:
            if row.template_name == template_name:
                return row
        return None


@dataclass(frozen=True)
class ExtractionResult:
    provider: str
    model: str
    document_ref: str
    variants: tuple[ExtractedVariant, ...]

    def variant_for(self, mpn: str) -> ExtractedVariant | None:
        key = normalize_mpn(mpn)
        for variant in self.variants:
            if normalize_mpn(variant.mpn) == key:
                return variant
        return None


class ExtractionProvider(Protocol):
    """What a datasheet extractor has to look like. Batch in, batch out.

    Deliberately the whole abstraction, exactly as `LabelBackend` is: a local
    vLLM worker, a frontier API and the fake below all satisfy this with no
    change to `cross_check.ingest`, which is the only caller any of them has.
    `docs/PLAN.md`'s design is "local first pass, frontier API as escalation for
    low-confidence items" — two objects of this shape and a rule for choosing,
    not two code paths.
    """

    name: str
    model: str

    def extract(self, request: ExtractionRequest) -> ExtractionResult: ...


# ---------------------------------------------------------------------------
# The schema, and the parser that enforces it
# ---------------------------------------------------------------------------


def schema_for(request: ExtractionRequest) -> dict[str, Any]:
    """The JSON schema the model's structured output is constrained to.

    Built from the request rather than written out as a constant, so
    `template_name` is an `enum` of exactly the fields this call asked for. A
    schema-constrained decode then makes an invented parameter name unrepresentable
    instead of merely rejected — and `parse_response` still rejects it, because a
    provider that ignores the schema is precisely the drift the live contract
    test exists to catch.
    """
    field_guide = "; ".join(field.describe() for field in request.fields)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["variants"],
        "properties": {
            "variants": {
                "type": "array",
                "description": (
                    "One entry per part number, for these and only these: "
                    + ", ".join(request.mpns)
                    + ". Include an entry only if the document actually states values for it."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["mpn", "mpn_source_text", "fields"],
                    "properties": {
                        "mpn": {
                            "type": "string",
                            "description": "The manufacturer part number, exactly as printed.",
                        },
                        "mpn_source_text": {
                            "type": "string",
                            "description": "Verbatim text this part number was read from.",
                        },
                        "fields": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "template_name",
                                    "raw_value",
                                    "confidence",
                                    "source_text",
                                ],
                                "properties": {
                                    "template_name": {
                                        "type": "string",
                                        "enum": list(request.field_names),
                                        "description": field_guide,
                                    },
                                    "raw_value": {"type": "string", "minLength": 1},
                                    "confidence": {
                                        "type": "number",
                                        "minimum": 0.0,
                                        "maximum": 1.0,
                                        "description": (
                                            "How sure you are this document states this value "
                                            "for this part number. Guessing is worse than "
                                            "omitting the field."
                                        ),
                                    },
                                    "source_text": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 500,
                                        "description": (
                                            "The cell, line or sentence the value was read "
                                            "from, verbatim. Required."
                                        ),
                                    },
                                    "page": {"type": ["integer", "null"], "minimum": 1},
                                },
                            },
                        },
                    },
                },
            }
        },
    }


def parse_response(
    payload: object, request: ExtractionRequest, *, provider: str, model: str
) -> ExtractionResult:
    """Validate a raw response into an `ExtractionResult`, or raise."""
    body = _object(payload, "response")
    variants_raw = body.get("variants")
    if not isinstance(variants_raw, list):
        raise ExtractionResponseError("response.variants must be an array")

    allowed = set(request.field_names)
    seen_mpns: set[str] = set()
    variants: list[ExtractedVariant] = []

    for index, entry in enumerate(variants_raw):
        where = f"variants[{index}]"
        variant_body = _object(entry, where)
        mpn = _text(variant_body, "mpn", where)
        key = normalize_mpn(mpn)
        if not key:
            raise ExtractionResponseError(f"{where}.mpn is not a usable part number: {mpn!r}")
        if key in seen_mpns:
            # Two rows for one part number would collide on the candidate
            # table's `(part, template, source, source_ref)` uniqueness and
            # quietly overwrite each other, so the disagreement the response
            # actually contains would vanish. It is a malformed response.
            raise ExtractionResponseError(f"{where}.mpn {mpn!r} appears more than once")
        seen_mpns.add(key)

        fields_raw = variant_body.get("fields")
        if not isinstance(fields_raw, list):
            raise ExtractionResponseError(f"{where}.fields must be an array")

        seen_fields: set[str] = set()
        fields: list[ExtractedField] = []
        for field_index, field_entry in enumerate(fields_raw):
            field_where = f"{where}.fields[{field_index}]"
            field_body = _object(field_entry, field_where)
            name = _text(field_body, "template_name", field_where)
            if name not in allowed:
                raise ExtractionResponseError(
                    f"{field_where}.template_name {name!r} was not requested; "
                    f"asked for {sorted(allowed)}"
                )
            if name in seen_fields:
                raise ExtractionResponseError(f"{field_where}.template_name {name!r} is repeated")
            seen_fields.add(name)
            fields.append(
                ExtractedField(
                    template_name=name,
                    raw_value=_text(field_body, "raw_value", field_where),
                    confidence=_confidence(field_body, field_where),
                    source_text=_text(field_body, "source_text", field_where),
                    page=_page(field_body, field_where),
                )
            )

        variants.append(
            ExtractedVariant(
                mpn=mpn,
                mpn_source_text=_text(variant_body, "mpn_source_text", where),
                fields=tuple(fields),
            )
        )

    return ExtractionResult(
        provider=provider,
        model=model,
        document_ref=request.document_ref,
        variants=tuple(variants),
    )


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtractionResponseError(f"{where} must be an object, got {type(value).__name__}")
    return value


def _text(body: dict[str, Any], key: str, where: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExtractionResponseError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _confidence(body: dict[str, Any], where: str) -> float:
    value = body.get("confidence")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ExtractionResponseError(f"{where}.confidence must be a number")
    if not 0.0 <= float(value) <= 1.0:
        raise ExtractionResponseError(f"{where}.confidence must be between 0 and 1, got {value}")
    return float(value)


def _page(body: dict[str, Any], where: str) -> int | None:
    value = body.get("page")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExtractionResponseError(f"{where}.page must be an integer or null")
    return value


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


class FakeExtractionProvider:
    """Replays a response recorded once from a real one. No network, no model.

    The fixture holds the provider's **raw JSON**, not this module's dataclasses,
    and the fake runs it through the same `parse_response` a real provider's
    output goes through. That is the whole point: a fake returning pre-built
    objects would exercise none of the parsing, so the refusals above — invented
    field names, missing `source_text`, a confidence of 1.4 — would be tested
    only against hand-written inputs and never against the shape a model
    actually emits.

    Fixture format::

        {"provider": "...", "model": "...",
         "responses": {"<document_ref>": {"variants": [...]}}}

    Keyed by `document_ref` so one file can hold several recorded documents, and
    so a test that builds the wrong request gets `FixtureMiss` rather than
    somebody else's datasheet.
    """

    def __init__(self, fixture_path: Path) -> None:
        body = _object(json.loads(fixture_path.read_text(encoding="utf-8")), "fixture")
        self.name = str(body.get("provider", "fake"))
        self.model = str(body.get("model", "fake"))
        self._responses = _object(body.get("responses"), "fixture.responses")
        self.calls: list[ExtractionRequest] = []

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        if request.document_ref not in self._responses:
            raise FixtureMiss(
                f"no recorded response for document_ref {request.document_ref!r}; "
                f"recorded: {sorted(self._responses)}"
            )
        return parse_response(
            self._responses[request.document_ref],
            request,
            provider=self.name,
            model=self.model,
        )
