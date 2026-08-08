"""The real `VisionProvider`: a photograph to an OpenAI-compatible endpoint.

The transport half of `vision.py`, and the counterpart to `openai_compat.py`.
Everything about the schema, the parsing and the refusals lives next door; this
module adds a way to send an image and nothing else.

## Two wire shapes, because the two servers genuinely disagree

Text extraction gets away with one payload for every backend. Multimodal does not,
and pretending otherwise is how this ships broken against whichever server was not
tried:

* **`openai_content_parts`** -- the OpenAI spelling. `content` becomes a list of
  parts and the image rides in `{"type": "image_url", "image_url": {"url":
  "data:image/jpeg;base64,..."}}`, with the schema constraint sent as
  `response_format` plus `guided_json`, exactly as extraction does. This is
  vLLM's documented path.

* **`ollama_native`** -- Ollama's own `/api/chat`, where images are a flat
  `images: ["<base64>"]` array on the message with **no `data:` prefix**, and the
  schema constraint is the top-level `format` field.

**`ollama_native` is the default for Ollama, and the reason is the constraint
rather than the image.** Ollama's OpenAI-compatibility documentation describes
`response_format` as supported without saying whether the `json_schema` variant
is, while `/api/chat`'s `format` field taking a whole JSON schema is documented
plainly. Constrained decoding is not a nicety here -- it is what makes a datasheet
URL unrepresentable rather than merely discouraged (see `vision.schema_for`), so
the path where the constraint is documented to work is the path to use. The image
encoding differs too, and that difference is real: the same docs show `image_url`
as a bare string rather than the nested object the OpenAI spec uses, which is
precisely the kind of divergence that fails at 2 a.m. rather than in review.

`for_base_url()` picks between them so no caller has to remember which server is
which, and `@pytest.mark.live` contract tests are what actually settle it. Until
those have run against both, **treat the choice as informed rather than verified**
-- it is written down here so the next person knows which claim to check.

## What is copied from `openai_compat.py` and why it is copied

`urllib`, no SDK. `temperature=0`. No retry on a bad parse. Re-validation through
`parse_response` regardless of what the schema was supposed to guarantee. Each of
those arguments is made in that module's docstring and transfers unchanged.

The duplication is deliberate and matches what the repository already does:
`chat_agent.OpenAICompatChatModel` and `OpenAICompatExtractionProvider` are
already two independent copies of a urllib POST with different temperatures,
timeouts and response handling, and neither imports the other. A shared base class
would have to be parameterised by every one of those differences, which is how a
helper becomes harder to read than the thing it replaced.

## The image is sent as stored

`grab.ts` writes the still at the camera's native resolution with no downscale, so
a 4K phone frame arrives here whole. Both servers resize internally against their
own pixel budget, and adding Pillow to the worker to pre-shrink it would be a real
dependency for a cost nobody has measured. `CallStats.prompt_tokens` is recorded
on every call precisely so that cost becomes visible; if it turns out to dominate,
the resize belongs here, in the worker, and not in the API.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from app.services.enrichment.calls import CallStats
from app.services.enrichment.openai_compat import ModelUnavailable, stats_from_openai_body
from app.services.enrichment.vision import (
    VisionRequest,
    VisionResult,
    parse_response,
    schema_for,
)
from app.services.timing import elapsed_ms

#: Seconds one read may take. Generous for the same reason extraction's is: the
#: caller is a worker with a thirty-minute lease, and the first call after a model
#: swap is competing with weights still arriving in VRAM.
DEFAULT_TIMEOUT = 300.0

#: The ceiling on one answer. Generous, and the reason is measured rather than
#: guessed.
#:
#: The answer itself is tiny -- three candidates is about forty tokens. But
#: **Qwen3-VL is a thinking model and its reasoning is spent from this same
#: budget before the answer begins.** Observed on qwen3-vl:8b against the XBee
#: case in `bench/corpus/`: roughly 1700 characters of reasoning, then 44 tokens
#: of content. At 1024 the reasoning consumed the ceiling and the JSON arrived
#: truncated, which reads as a malformed response from a healthy model.
#:
#: **Turning thinking off is not the fix, and it is worth saying why.** Ollama's
#: `think: false` on this model returns an *empty* completion -- the reasoning is
#: suppressed and nothing takes its place. That is the same failure this
#: repository already hit once and fixed in chat ("an answer that was all
#: reasoning read as no answer at all"), and it is strictly worse than a large
#: budget: a truncated answer is at least diagnosable, an empty one looks like
#: the model had nothing to say.
#:
#: 8192, and the last raise is the informative one. At 4096 the *easy* case
#: answered fine and the ambiguous one -- a bare module whose label carries an
#: FCC ID, an IC number and a MAC address alongside the part number -- spent
#: **12318 characters of reasoning and never answered**. Reasoning cost scales
#: with how ambiguous the picture is, so the ceiling has to be set by the hard
#: case rather than the common one. Matching `openai_compat`'s 8192 keeps one
#: number to remember.
DEFAULT_MAX_TOKENS = 8192

ImageTransport = Literal["openai_content_parts", "ollama_native"]

#: What the model is told it is doing. The schema carries the field rules, so this
#: says only the things a schema cannot: what the photograph is, what the other
#: readings are worth, and that refusing to answer is allowed.
SYSTEM_PROMPT = (
    "You identify electronic components from photographs of their packaging. "
    "You are shown one photograph, usually a distributor bag, a reel label or a "
    "loose part.\n"
    "\n"
    "Rules:\n"
    "- Report the manufacturer part number exactly as printed, including any "
    "suffix. Do not complete a partially legible number from memory, and do not "
    "expand or tidy what is printed.\n"
    "- If you cannot read enough to name a part, return no candidates. That is a "
    "useful answer. Inventing a plausible part number is the one outcome that "
    "cannot be recovered from downstream.\n"
    "- Every candidate needs the characters you read it from, verbatim. If you "
    "cannot quote it, you did not read it.\n"
    "- Your confidence is about legibility -- focus, glare, creases, obscured "
    "characters -- not about how plausible the part is.\n"
    "- Many strings on a label are not the part number. A distributor ordering "
    "code (DigiKey, Mouser), an FCC ID, a Canadian IC certification number, a "
    "MAC address or OUI prefix, a serial number, a date code and an internal "
    "master-part ID are all NOT the manufacturer part number. Report none of "
    "them as one.\n"
    "- Where a label prints headings, the heading tells you which is which. "
    "'Manufacturer Part Number' is the one wanted; 'Part Number' on a "
    "distributor label usually is not.\n"
    "- On a bare component with no headings, the part number is the marking that "
    "encodes the product, not the one that encodes a certification. If two "
    "candidates are plausible, return both and say which line each came from "
    "rather than choosing confidently."
)


def for_base_url(
    base_url: str, model: str, *, name: str | None = None, **kwargs: Any
) -> OpenAICompatVisionProvider:
    """Build a provider with the transport that server is known to speak.

    A convenience with a purpose: which wire shape a server wants is a property of
    the server, and making every caller remember it is how one of them gets it
    wrong. Ollama is recognised by its port, which is what
    `model_catalog.OLLAMA` pins.
    """
    ollama = ":11434" in base_url
    return OpenAICompatVisionProvider(
        base_url=base_url,
        model=model,
        name=name or ("local-ollama" if ollama else "local-vllm"),
        image_transport="ollama_native" if ollama else "openai_content_parts",
        **kwargs,
    )


@dataclass
class OpenAICompatVisionProvider:
    """`VisionProvider` over a chat endpoint that accepts images.

    `name` is recorded as provenance and should say *which deployment* answered
    rather than repeating the model name, which is stored separately -- the same
    convention `OpenAICompatExtractionProvider` uses.
    """

    base_url: str
    model: str
    name: str = "local-vision"
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int = DEFAULT_MAX_TOKENS
    image_transport: ImageTransport = "openai_content_parts"

    def read(self, request: VisionRequest) -> VisionResult:
        schema = schema_for(request)
        if self.image_transport == "ollama_native":
            path, payload = "/api/chat", self._ollama_payload(request, schema)
        else:
            path, payload = "/v1/chat/completions", self._openai_payload(request, schema)
        # Built here, before the call, so the transcript exists whether or not the
        # call comes back. It is handed to `parse_response` on success and attached
        # to `ModelUnavailable` on every failure below -- see `_sanitised`.
        sent = self._sanitised(payload, request)

        started = time.perf_counter()
        body = self._post(path, payload, started=started, sent=sent)
        content = self._content_of(body, started=started, sent=sent)
        stats = self._stats_of(body, started=started)

        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as error:
            if stats.truncated:
                raise ModelUnavailable(
                    f"{self.name} stopped at the {self.max_tokens}-token ceiling with the "
                    f"answer unfinished, so it is not valid JSON. Raise max_tokens. ({error})",
                    elapsed_ms=stats.latency_ms,
                    request_json=sent,
                    response_text=content,
                ) from error
            raise ModelUnavailable(
                f"{self.name} returned content that is not JSON; does {self.model} support "
                f"constrained decoding over the {self.image_transport} transport? ({error})",
                elapsed_ms=stats.latency_ms,
                request_json=sent,
                response_text=content,
            ) from error

        # Re-validated regardless of what the schema was supposed to guarantee.
        # The server that silently ignored the constraint is exactly the one
        # whose output nobody checked.
        return parse_response(
            decoded,
            request,
            provider=self.name,
            model=self.model,
            stats=stats,
            raw_response=content,
            request_json=sent,
        )

    # -- the transcript ----------------------------------------------------

    def _sanitised(self, payload: dict[str, Any], request: VisionRequest) -> str:
        """The payload as JSON, with the image swapped for its hash.

        **The one rule: no image bytes leave this module.** A base64'd 4K frame is
        several megabytes, `model_runs` has no pruning, and the blob already exists
        in the document store — so a copy would be pure duplication in the one
        place nothing sweeps. `{"image_sha256": ...}` is a strictly better handle
        anyway: it is what every other table calls a picture, and
        `GET /api/documents/{sha256}` serves the real thing.

        Surgery on the two known locations rather than a recursive scrub of
        anything long, deliberately. A "replace strings over N characters" pass
        would silently redact a prompt that grew — the OCR hints are prompt text
        and are exactly what a reviewer is here to read — and would keep passing
        while quietly hiding the interesting half. This is written per wire shape
        so a third one has to come back here and say what its image field is.
        """
        placeholder = {"image_sha256": request.document_sha256}
        messages = payload.get("messages")
        if isinstance(messages, list):
            scrubbed: list[Any] = []
            for message in messages:
                if not isinstance(message, dict):  # pragma: no cover - we build these
                    scrubbed.append(message)
                    continue
                copy = dict(message)
                if "images" in copy:
                    # `ollama_native`: a flat array of bare base64 on the message.
                    copy["images"] = [placeholder for _ in copy["images"]]
                content = copy.get("content")
                if isinstance(content, list):
                    # `openai_content_parts`: the image rides in a content part as
                    # a `data:` URI.
                    copy["content"] = [
                        {"type": "image_url", "image_url": placeholder}
                        if isinstance(part, dict) and part.get("type") == "image_url"
                        else part
                        for part in content
                    ]
                scrubbed.append(copy)
            payload = {**payload, "messages": scrubbed}
        return json.dumps(payload, sort_keys=True)

    # -- payloads ----------------------------------------------------------

    def _prompt(self, request: VisionRequest) -> str:
        """What is already known about this frame, said plainly.

        The browser's readings go in as **evidence, not instructions**. The
        barcode is the strong one: a checksummed symbology is better than
        anything the model will produce, so when it is present the request is
        narrowed to a single candidate and the job becomes confirmation. The OCR
        lines are the weak one, and are labelled as unreliable rather than
        omitted -- they are usually nearly right, and "nearly right" is exactly
        what a second reader can repair.
        """
        parts = []
        if request.barcode_texts:
            parts.append(
                "A barcode on this label decoded to the following. It is "
                "checksummed and is more reliable than anything you can read from "
                "the image, so confirm it rather than contradicting it; report the "
                "manufacturer part number it contains, not a distributor code:\n"
                + "\n".join(f"  {text}" for text in request.barcode_texts)
            )
        if request.ocr_lines:
            parts.append(
                "Optical character recognition read these lines from the same "
                "image. It is often wrong about single characters -- 1 and I, 0 "
                "and O, 5 and S -- so treat it as a hint and trust your own "
                "reading of the pixels where they differ:\n"
                + "\n".join(f"  {line}" for line in request.ocr_lines)
            )
        parts.append(
            "Identify the component in the photograph."
            if not request.anchored
            else "Confirm the part number above and report its manufacturer and package."
        )
        return "\n\n".join(parts)

    def _openai_payload(self, request: VisionRequest, schema: dict[str, Any]) -> dict[str, Any]:
        data_uri = f"data:{request.media_type};base64,{self._b64(request)}"
        return {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt(request)},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "capture_identity", "strict": True, "schema": schema},
            },
            # vLLM's older spelling of the same constraint. Servers ignore fields
            # they do not recognise, so sending both costs a few bytes.
            "guided_json": schema,
        }

    def _ollama_payload(self, request: VisionRequest, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "stream": False,
            # A whole JSON schema, which is Ollama's documented way of saying what
            # `response_format: json_schema` says elsewhere.
            "format": schema,
            "options": {"temperature": 0, "num_predict": self.max_tokens},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": self._prompt(request),
                    # Bare base64, no `data:` prefix. This is the difference that
                    # would otherwise be discovered in production.
                    "images": [self._b64(request)],
                },
            ],
        }

    def _b64(self, request: VisionRequest) -> str:
        return base64.b64encode(request.image).decode("ascii")

    # -- responses ---------------------------------------------------------

    def _content_of(self, body: dict[str, Any], *, started: float, sent: str | None = None) -> str:
        """The answer text, from whichever shape the server replies in."""
        try:
            if self.image_transport == "ollama_native":
                content = body["message"]["content"]
            else:
                content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ModelUnavailable(
                f"no completion in the response: {error}",
                elapsed_ms=elapsed_ms(started),
                request_json=sent,
            ) from error
        if not isinstance(content, str):
            raise ModelUnavailable(
                f"completion content was {type(content).__name__}, not a string",
                elapsed_ms=elapsed_ms(started),
                request_json=sent,
            )
        if not content.strip():
            # An answer that was all reasoning. Observed with Ollama's
            # `think: false` on a thinking model: the reasoning is suppressed and
            # nothing replaces it. Named plainly, because an empty string would
            # otherwise surface as "content that is not JSON" and send the reader
            # off to investigate constrained decoding, which is working fine.
            thinking = _reasoning_of(body)
            detail = (
                f" It emitted {len(thinking)} characters of reasoning and no answer."
                if thinking
                else ""
            )
            raise ModelUnavailable(
                f"{self.name} returned an empty completion.{detail} "
                "A thinking model spends this budget reasoning before it answers; "
                "raise max_tokens rather than disabling reasoning, which on some "
                "servers removes the answer along with it.",
                elapsed_ms=elapsed_ms(started),
                request_json=sent,
                # The reasoning *is* the response here. Recorded as one, because a
                # run that emitted 12318 characters of thinking and no answer is
                # ADR 0021's own measured case and the thing worth reading.
                response_text=thinking or None,
            )
        return content

    def _stats_of(self, body: dict[str, Any], *, started: float) -> CallStats:
        """What the call cost, in whichever vocabulary the server reports it.

        Ollama's native endpoint counts in `prompt_eval_count` / `eval_count` and
        has no `finish_reason` at all -- it says `done_reason` instead. Mapped
        here rather than left unreported, because the prompt token count is the
        one number that will say whether sending a 4K frame whole is affordable.
        """
        if self.image_transport != "ollama_native":
            return stats_from_openai_body(body, started=started)

        done_reason = body.get("done_reason")
        return CallStats(
            latency_ms=elapsed_ms(started),
            prompt_tokens=_count(body.get("prompt_eval_count")),
            completion_tokens=_count(body.get("eval_count")),
            # Ollama says "length" for the same condition, so no translation is
            # needed -- but anything else is passed through as-is rather than
            # coerced into the OpenAI vocabulary it does not belong to.
            finish_reason=done_reason if isinstance(done_reason, str) and done_reason else None,
        )

    def _post(
        self, path: str, payload: dict[str, Any], *, started: float, sent: str | None = None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url.rstrip("/") + path
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                decoded: Any = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read()[:500].decode("utf-8", "replace")
            raise ModelUnavailable(
                f"HTTP {error.code} from {url}: {detail}",
                elapsed_ms=elapsed_ms(started),
                request_json=sent,
            ) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ModelUnavailable(
                f"{type(error).__name__} calling {url}: {error}",
                elapsed_ms=elapsed_ms(started),
                request_json=sent,
            ) from error
        if not isinstance(decoded, dict):
            raise ModelUnavailable(
                f"{url} returned {type(decoded).__name__}, not an object",
                elapsed_ms=elapsed_ms(started),
                request_json=sent,
            )
        return decoded


def _reasoning_of(body: dict[str, Any]) -> str:
    """The model's reasoning, wherever this server happens to put it.

    Ollama's native shape uses `message.thinking`; the OpenAI-compatible servers
    that expose it at all use `reasoning_content` on the choice's message. Read
    only to explain an empty answer, never used as one.
    """
    message = body.get("message")
    if isinstance(message, dict):
        thinking = message.get("thinking")
        if isinstance(thinking, str):
            return thinking
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        inner = choices[0].get("message")
        if isinstance(inner, dict):
            reasoning = inner.get("reasoning_content")
            if isinstance(reasoning, str):
                return reasoning
    return ""


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
