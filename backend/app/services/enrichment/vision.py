"""Reading a photograph of a part: a request, a JSON schema, a parser, a fake.

The mirror of `extract.py`, one stage earlier in the pipeline and pointed at an
image instead of a page of text. Where extraction asks *what does this datasheet
say about this known part number*, this asks the question that comes before it:
**what part is this a photograph of?**

`extract.py`'s split is copied deliberately — the pure half here, the transport in
`vision_openai_compat.py` — and for the same reason ADR 0005 gives: none of the
model runs in this process. The API is a single replica pinned to an RWO volume
and must not be holding a GPU. What lives here is the interface, and every line of
it is verifiable with no network, no model and no new dependency.

## This is the first image-to-model code in the repository, and it is fenced

Every pixel operation in Almagest happens in the browser (ADR 0015): `zxing-wasm`
decodes the barcodes, `tesseract.js` reads the printed lines, and the API's entire
relationship with an image is five magic bytes and a sha256. That is not being
undone. This module is a *second reader of the same frame*, sitting behind a work
queue and a worker process, and it exists for the case ADR 0015 was written about
and could not solve: ink on a bag with no readable code, or a bare part with only
a top marking.

The browser's read is not replaced by it and not distrusted by it either. Both go
into the request: `barcode_texts` is an **anchor** the model reconciles against,
and `ocr_lines` is the thing most likely to be wrong and most useful to correct.

## The schema is where the rules become unrepresentable, not merely forbidden

`CLAUDE.md` says an OCR'd or model-read part number is never auto-accepted, and
ADR 0017 says the researcher proposes candidates and never asserts a URL. Both are
rules a future edit could quietly break. So they are not written as rules here:

* **There is no `url` field.** Under a schema-constrained decode the model cannot
  emit a datasheet URL at all. It is also nowhere in the provider cascade -- its
  only role there stays ranking things other providers fetched.
* **There is no `quantity`, `date_code` or `lot_code` field.** Those come off the
  barcode deterministically, through `extract.ts:extractSuggestions`. A vision
  model reading them would be a second and worse source for a solved problem.
* **`source_text` is required and non-empty.** If you cannot quote the characters
  you read, you did not read them. Same contract `ExtractedField.source_text`
  enforces, and for the same reason: an assertion nobody can trace back to the
  image cannot be reviewed.

`mpn` is necessarily a free string, because the space of part numbers is
unbounded. That is precisely why it can never be asserted: the only thing that
will ever confirm it is `datasheet_validation`'s check that the normalised part
number appears in the text of a PDF that was actually fetched.

## An empty answer is a normal answer

`VisionResult.candidates` may be empty and that is not an error. "I cannot tell
what this is" is the honest reading of a blurred label, and the queue has a
terminal state for it (`DispatchState.UNIDENTIFIED`) that is deliberately not
`FAILED` -- the same distinction `research.py` draws between `EXHAUSTED` and a run
that broke. A model pushed to answer anyway is a model inventing a part number,
which is the single failure this pipeline exists to prevent.

## What the confidence means, and what it must never be mixed with

`IdentityCandidate.confidence` is about **reading characters off a photograph**:
glare, focus, a crease through the third digit. It is not about whether a
datasheet states a value, which is what `ExtractedField.confidence` means and what
`candidates.AUTO_PROMOTE_CONFIDENCE` is calibrated against. They happen to share a
0..1 range and they are different quantities. Feeding this one into the promotion
rules would smuggle photo quality into a parameter's trust, so nothing downstream
of this module is allowed to do it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.services.enrichment.calls import CallStats

#: How many identities the model may propose when nothing anchors the read.
#:
#: Three, not one, because the whole point of proposing rather than asserting is
#: that the losers are kept and compete -- `datasheet_validation` eliminates the
#: wrong ones by failing to find their part number in a PDF it actually fetched,
#: which is arithmetic a reviewer can check rather than an opinion they cannot.
#: Beyond three the tail is noise: a label legible enough for a fourth guess to
#: be right is legible enough for the first three to contain it.
DEFAULT_MAX_CANDIDATES = 3

#: What the label is physically, for the reviewer's reading only.
#:
#: Never becomes a field value and never reaches `parameter_value_candidate`. It
#: is here because "this is a cut-tape strip" and "this is a reel" change what a
#: person expects the quantity to be, and that context is free to ask for while
#: the image is already in front of the model.
LABEL_KINDS = ("reel", "cut_tape", "bag", "tray", "bare_part", "unknown")


class VisionResponseError(ValueError):
    """The provider's response does not satisfy the schema it was given.

    Raised rather than repaired, for the reason `ExtractionResponseError` gives:
    a response that half-parses is the one case where guessing what the model
    meant would put an invented part number in front of a person with a real
    confidence attached to it.
    """


class VisionFixtureMiss(LookupError):
    """`FakeVisionProvider` has no recorded response for that image.

    A miss raises instead of returning an empty result. Empty is a *meaningful*
    answer here -- it settles an entry as `UNIDENTIFIED` -- so a fake that
    returned it on a missing fixture would let a test assert the unidentified
    path while actually testing a typo in a sha256.
    """


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisionRequest:
    """One photograph, plus everything already known about it.

    The image is passed as bytes rather than a path or a sha256 because this
    module never touches the blob store: the worker fetches the document over
    HTTP like every other worker does, and hands the bytes in. That keeps the one
    filesystem-reading path in `blobstore.py` and keeps this module importable by
    tests that have no data directory at all.
    """

    image: bytes
    #: From `documents.media_type`, never sniffed here. `blobstore.store` already
    #: checked the magic bytes on the way in, so re-deriving it would be a second
    #: opinion that is allowed to disagree with the row.
    media_type: str
    #: Provenance. What a reviewer opens, and what the result is keyed by.
    document_sha256: str
    #: What the browser decoded. **An anchor, not an instruction.** A checksummed
    #: symbology is stronger evidence than anything the model will produce, so
    #: when this is non-empty the caller narrows `max_candidates` to 1 and asks
    #: only for confirmation of manufacturer and package.
    barcode_texts: tuple[str, ...] = ()
    #: What tesseract read. The least reliable input and the most useful to
    #: correct -- ADR 0015 exists because these lines are wrong often enough to
    #: need a person, and this is the second reader of them.
    ocr_lines: tuple[str, ...] = ()
    max_candidates: int = DEFAULT_MAX_CANDIDATES

    def __post_init__(self) -> None:
        if not self.image:
            raise ValueError("VisionRequest.image is empty")
        if not self.document_sha256:
            raise ValueError("VisionRequest.document_sha256 is required")
        if self.max_candidates < 1:
            raise ValueError(f"max_candidates must be at least 1, got {self.max_candidates}")

    @property
    def anchored(self) -> bool:
        """Did a barcode already say what this is?

        The common case, and worth naming: a distributor bag with a readable
        DataMatrix needs the model only to confirm the manufacturer and package.
        The fan-out over several identities exists for the case that has no code
        at all, which is not the case most captures are.
        """
        return bool(self.barcode_texts)


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityCandidate:
    """One part this photograph might be of. A proposal, never an answer."""

    mpn: str
    manufacturer: str | None
    #: 0..1, about reading characters off an image. See the module docstring for
    #: why this must never reach `candidates.AUTO_PROMOTE_CONFIDENCE`.
    confidence: float
    #: The characters on the label this reading came from, verbatim. Required.
    source_text: str
    package: str | None = None
    #: Free text for a reviewer: why this reading is uncertain, what was
    #: illegible, which sibling variants it could equally be.
    note: str | None = None


@dataclass(frozen=True)
class VisionResult:
    provider: str
    model: str
    document_sha256: str
    #: Ranked, best first. **May be empty** -- see the module docstring.
    candidates: tuple[IdentityCandidate, ...] = ()
    label_kind: str | None = None
    #: What the call cost. `None` from a fake or a replayed fixture, which is
    #: honest -- a recording has no latency of its own. The prompt token count is
    #: the one that matters here: `grab.ts` does not downscale, so this is what
    #: will say whether sending a 4K frame whole is affordable.
    stats: CallStats | None = None

    @property
    def identified(self) -> bool:
        return bool(self.candidates)

    @property
    def best(self) -> IdentityCandidate | None:
        return self.candidates[0] if self.candidates else None


class VisionProvider(Protocol):
    """What a capture reader has to look like. One image in, ranked guesses out.

    Deliberately the whole abstraction, as `ExtractionProvider` is: an Ollama
    server, a vLLM server and the fake below all satisfy it with no change to the
    worker that calls it, so which model reads a photograph is configuration
    rather than code.
    """

    name: str
    model: str

    def read(self, request: VisionRequest) -> VisionResult: ...


# ---------------------------------------------------------------------------
# The schema, and the parser that enforces it
# ---------------------------------------------------------------------------


def schema_for(request: VisionRequest) -> dict[str, Any]:
    """The JSON schema the model's structured output is constrained to.

    Built from the request rather than written out as a constant, so
    `maxItems` tracks `request.max_candidates` -- an anchored read is held to one
    candidate by the decoder rather than by a hopeful sentence in the prompt.

    Everything the module docstring says is unrepresentable is unrepresentable
    *here*, in `additionalProperties: False` plus the absence of a `url`,
    `quantity`, `date_code` or `lot_code` property. `parse_response` still
    re-checks what this guarantees, because a server that silently ignored
    `response_format` is exactly the one nobody validated.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "maxItems": request.max_candidates,
                "description": (
                    "Parts this image might show, best first. Return an empty array if "
                    "you cannot read enough to name a part -- that is a useful answer and "
                    "a guess is not. Never invent a part number to fill this in."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["mpn", "confidence", "source_text"],
                    "properties": {
                        "mpn": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "The manufacturer part number exactly as printed, including "
                                "any suffix. Do not expand abbreviations or complete a "
                                "partially legible number from memory."
                            ),
                        },
                        "manufacturer": {
                            "type": ["string", "null"],
                            "description": "The manufacturer, if the image says so.",
                        },
                        "package": {
                            "type": ["string", "null"],
                            "description": (
                                "Package or case code if printed or unambiguous from the "
                                "photograph, such as 0603 or SOT-23. Null if unsure."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": (
                                # The range is spelled out in the description as well as
                                # in `minimum`/`maximum` because **servers enforce the
                                # type and not the bounds**: observed on Ollama's
                                # structured output, qwen3-vl:8b answered `100` for a
                                # field declared `maximum: 1.0`. `parse_response` refuses
                                # it either way, but a refused read is a wasted GPU
                                # second, and the description is what the model actually
                                # reads.
                                "A decimal fraction between 0.0 and 1.0 -- for example "
                                "0.95, never 95. How legible this reading was: focus, "
                                "glare, creases, obscured characters. Not how plausible "
                                "the part is."
                            ),
                        },
                        "source_text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": (
                                "The characters on the label you read this from, verbatim. "
                                "Required. If you cannot quote it, you did not read it."
                            ),
                        },
                        "note": {
                            "type": ["string", "null"],
                            "maxLength": 500,
                            "description": (
                                "Anything a person reviewing this should know: what was "
                                "illegible, which variants it could equally be."
                            ),
                        },
                    },
                },
            },
            "label_kind": {
                "type": ["string", "null"],
                "enum": [*LABEL_KINDS, None],
                "description": "What the thing in the photograph physically is.",
            },
        },
    }


def parse_response(
    payload: object,
    request: VisionRequest,
    *,
    provider: str,
    model: str,
    stats: CallStats | None = None,
) -> VisionResult:
    """Validate a raw response into a `VisionResult`, or raise.

    Re-checks everything `schema_for` was supposed to guarantee. That duplication
    is the point: `openai_compat.py`'s docstring already argues it, and the case
    it protects against -- a server that accepted `response_format` and ignored
    it -- is a deployment fact rather than a model fact, so it will be discovered
    by whoever swaps the serving stack and by nobody else.

    Candidates in excess of `max_candidates` are **refused rather than truncated**.
    Silently dropping the tail would hide the fact that the decode was not
    constrained, which is the very thing worth knowing.
    """
    body = _object(payload, "response")
    candidates_raw = body.get("candidates")
    if not isinstance(candidates_raw, list):
        raise VisionResponseError("response.candidates must be an array")
    if len(candidates_raw) > request.max_candidates:
        raise VisionResponseError(
            f"response.candidates has {len(candidates_raw)} entries, "
            f"more than the {request.max_candidates} requested"
        )

    seen: set[str] = set()
    candidates: list[IdentityCandidate] = []
    for index, entry in enumerate(candidates_raw):
        where = f"candidates[{index}]"
        candidate_body = _object(entry, where)
        mpn = _text(candidate_body, "mpn", where)
        # Deduplicated on the printed string rather than a normalised form: two
        # readings that differ only in punctuation are two genuinely different
        # readings of the same characters, and which one is right is exactly what
        # the datasheet fetch will settle. Collapsing them here would throw away
        # the alternative before anything had a chance to test it.
        if mpn in seen:
            raise VisionResponseError(f"{where}.mpn {mpn!r} appears more than once")
        seen.add(mpn)
        candidates.append(
            IdentityCandidate(
                mpn=mpn,
                manufacturer=_optional_text(candidate_body, "manufacturer", where),
                confidence=_confidence(candidate_body, where),
                source_text=_text(candidate_body, "source_text", where),
                package=_optional_text(candidate_body, "package", where),
                note=_optional_text(candidate_body, "note", where),
            )
        )

    return VisionResult(
        provider=provider,
        model=model,
        document_sha256=request.document_sha256,
        candidates=tuple(candidates),
        label_kind=_label_kind(body),
        stats=stats,
    )


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VisionResponseError(f"{where} must be an object, got {type(value).__name__}")
    return value


def _text(body: dict[str, Any], key: str, where: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VisionResponseError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _optional_text(body: dict[str, Any], key: str, where: str) -> str | None:
    """A field the model may legitimately not know.

    An empty or whitespace string becomes None rather than raising. A model
    declining to name the manufacturer is answering correctly, and `""` is how
    several of them spell it under a schema that permits a string.
    """
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise VisionResponseError(f"{where}.{key} must be a string or null")
    return value.strip() or None


#: Above this, a confidence is being reported in percent and is divided by 100.
#:
#: **Measured, not anticipated.** qwen3-vl:8b answers `95` for a field declared
#: `{"minimum": 0.0, "maximum": 1.0}`, reproducibly, across both wire shapes and
#: across two revisions of the prompt and the schema description that each said
#: "a decimal fraction between 0.0 and 1.0 -- for example 0.95, never 95". The
#: servers enforce the *type* of a constrained field and not its bounds, so the
#: schema cannot make this unrepresentable the way it can with a missing property.
PERCENT_THRESHOLD = 1.0


def _confidence(body: dict[str, Any], where: str) -> float:
    """The model's confidence, in whichever scale it chose to answer in.

    A percentage is normalised rather than refused, and that is a narrower
    concession than it looks. What this module refuses -- an invented part
    number, a missing quote, a candidate beyond the requested cap -- are all
    *claims about the part*. A confidence expressed as 95 instead of 0.95 is a
    **unit convention**, and this codebase already normalises units everywhere
    rather than rejecting a datasheet that says `100 nF` where the template
    says farads.

    Refusing it was tried first and is the wrong trade: it discards an otherwise
    correct reading, and it fails the whole capture rather than the field. The
    blast radius of accepting it is small by construction -- a vision confidence
    never reaches `candidates.AUTO_PROMOTE_CONFIDENCE` (see the module
    docstring), so this number ranks candidates for a reviewer and decides
    nothing on its own.

    Anything above 100 is still refused. At that point the scale is not a
    convention this can recognise, and guessing would be inventing.
    """
    value = body.get("confidence")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise VisionResponseError(f"{where}.confidence must be a number")
    number = float(value)
    if PERCENT_THRESHOLD < number <= 100.0:
        number /= 100.0
    if not 0.0 <= number <= 1.0:
        raise VisionResponseError(
            f"{where}.confidence must be between 0 and 1, or a percentage of it, got {value}"
        )
    return number


def _label_kind(body: dict[str, Any]) -> str | None:
    value = body.get("label_kind")
    if value is None:
        return None
    if not isinstance(value, str):
        raise VisionResponseError("response.label_kind must be a string or null")
    kind = value.strip().lower()
    if not kind:
        return None
    if kind not in LABEL_KINDS:
        raise VisionResponseError(
            f"response.label_kind {value!r} is not one of {list(LABEL_KINDS)}"
        )
    return kind


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


class FakeVisionProvider:
    """Replays a response recorded once from a real one. No network, no model.

    The fixture holds the provider's **raw JSON**, not this module's dataclasses,
    and the fake runs it through the same `parse_response` a real provider's
    output goes through -- the argument `FakeExtractionProvider` makes, unchanged.
    A fake returning pre-built objects would exercise none of the parsing, so
    every refusal above would be tested only against hand-written inputs and
    never against the shape a model actually emits.

    Fixture format::

        {"provider": "...", "model": "...",
         "responses": {"<document_sha256>": {"candidates": [...]}}}

    Keyed by `document_sha256` because that is what identifies an image
    everywhere else in the system, and because a test that builds a request for
    the wrong photograph gets `VisionFixtureMiss` rather than somebody else's
    part number.
    """

    def __init__(self, fixture_path: Path) -> None:
        body = _object(json.loads(fixture_path.read_text(encoding="utf-8")), "fixture")
        self.name = str(body.get("provider", "fake"))
        self.model = str(body.get("model", "fake"))
        self._responses = _object(body.get("responses"), "fixture.responses")
        self.calls: list[VisionRequest] = []

    def read(self, request: VisionRequest) -> VisionResult:
        self.calls.append(request)
        if request.document_sha256 not in self._responses:
            raise VisionFixtureMiss(
                f"no recorded response for document {request.document_sha256!r}; "
                f"recorded: {sorted(self._responses)}"
            )
        return parse_response(
            self._responses[request.document_sha256],
            request,
            provider=self.name,
            model=self.model,
        )
