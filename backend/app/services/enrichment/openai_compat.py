"""The real `ExtractionProvider`: an OpenAI-compatible endpoint, schema-constrained.

The drop-in `extract.ExtractionProvider` was written for. `parse_response` and
`cross_check.ingest` are untouched — this module adds a transport and nothing else,
which is what that Protocol existed to make possible.

One class serves **every** backend that speaks `/v1/chat/completions`: the local
vLLM of ADR 0016, Ollama, and a frontier API. `PLAN.md`'s design is "local first
pass, frontier API as escalation for low-confidence items", and that is two
instances of this class with different base URLs plus a rule for choosing — not two
code paths.

## The schema is a constraint, not a request

`schema_for(request)` is sent as `response_format: {"type": "json_schema", ...,
"strict": true}`, and its `template_name` is an `enum` of exactly the fields this
call asked for. On a server that honours it, an invented parameter name is
**unrepresentable** rather than merely rejected — the sampler cannot emit the
tokens. `guided_json` is sent alongside for vLLM, which named the same feature
differently before it grew the OpenAI spelling; servers ignore fields they do not
know, so sending both costs a few bytes and covers both.

`parse_response` still checks everything afterwards. That is not redundancy: a
server may quietly ignore `response_format` entirely, and the one that does is
exactly the one whose output nobody has validated. The live contract test exists
to catch that drift.

## Why `temperature=0` and no retry on a bad parse

Extraction is a reading task with a right answer. Sampling diversity buys nothing
and costs reproducibility — the same datasheet must yield the same candidate rows,
or a nightly re-run becomes a source of churn rather than a check.

A response that fails `parse_response` is **not** retried here. It is a malformed
answer from a model that was handed a schema, which is a fact worth surfacing to
the queue rather than papering over: the extraction queue already counts attempts
and expires leases, so the retry exists one layer up where it is bounded and
visible. A silent in-provider retry loop would turn a broken model into a slow
worker instead of a reported failure.

## No SDK

`urllib`, like every other client in this repository. The API image must not grow
an HTTP client for a code path that runs in the worker, and the wire format here is
one POST with a JSON body — an SDK would be a dependency, a version floor and a
retry policy nobody asked for, in exchange for nothing.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.services.enrichment.calls import CallStats
from app.services.enrichment.extract import (
    ExtractionRequest,
    ExtractionResult,
    parse_response,
    schema_for,
)
from app.services.timing import elapsed_ms

log = logging.getLogger("almagest.extract.model")

#: Seconds one completion may take. Generous: a 24-part variant table against a
#: 4-bit 8B model on a busy GPU is not fast, and the caller is a batch Job with a
#: fifteen-minute lease rather than a person waiting.
DEFAULT_TIMEOUT = 300.0

#: What the model is told it is doing. Short on purpose — the schema carries the
#: field definitions and the per-field guidance, so repeating them in prose would
#: be a second description free to drift from the one that is enforced.
SYSTEM_PROMPT = (
    "You read electronic component datasheets and report only what the document "
    "states. You are given one datasheet and a list of part numbers from a "
    "catalogue. For each part number, report the requested fields as the document "
    "states them for that exact part.\n"
    "\n"
    "Rules:\n"
    "- A datasheet usually covers a family. Find the row for the exact part "
    "number given, not a neighbouring variant. Picking the wrong row of a variant "
    "table is the most common way to be confidently wrong.\n"
    "- Omit any field the document does not state for that part. Omitting is "
    "always better than guessing; a missing field costs a person one lookup, and "
    "a wrong field is stored as fact.\n"
    "- Every value needs the verbatim text you read it from. If you cannot quote "
    "it, you did not read it.\n"
    "- Report only the part numbers you were given."
)


class ModelUnavailable(RuntimeError):
    """The endpoint could not be reached or refused the call.

    Distinct from a malformed response: nothing was read, so no document is
    implicated and the run — not the document — is what failed.

    Carries how long it took to fail, because the two ways this is raised look
    identical in a log and mean opposite things: a connection refused in 3 ms is
    a server that is not running, and the same message after 300 000 ms is a
    server that is running and wedged. One is fixed by scaling a deployment up
    and the other by looking at what it is doing.
    """

    def __init__(self, message: str, *, elapsed_ms: int | None = None) -> None:
        super().__init__(message)
        self.elapsed_ms = elapsed_ms


@dataclass
class OpenAICompatExtractionProvider:
    """`ExtractionProvider` over any `/v1/chat/completions` endpoint.

    `name` is recorded on every candidate row as its provenance, so it should say
    *which* deployment answered (`local-vllm`, `openrouter`) rather than repeating
    the model name, which is stored separately.
    """

    base_url: str
    model: str
    name: str = "local-vllm"
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    #: Sent as `max_tokens`. A 24-part batch with source quotes is a few thousand
    #: tokens; the ceiling exists so a model that starts looping is cut off rather
    #: than billed or, locally, left holding the GPU.
    max_tokens: int = 8192

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        schema = schema_for(request)
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._user_message(request)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "datasheet_extraction", "strict": True, "schema": schema},
            },
            # vLLM's own spelling of the same constraint, from before it grew the
            # OpenAI one. Servers ignore fields they do not recognise.
            "guided_json": schema,
        }

        started = time.perf_counter()
        body = self._post(payload, started=started)
        stats = stats_from_openai_body(body, started=started)
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ModelUnavailable(
                f"no completion in the response: {error}", elapsed_ms=stats.latency_ms
            ) from error

        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as error:
            # Two different faults produce non-JSON here, and telling them apart
            # is the whole reason `finish_reason` is read.
            if stats.truncated:
                # The answer was cut off mid-object. Nothing is wrong with the
                # model or the server; the batch did not fit in `max_tokens`.
                raise ModelUnavailable(
                    f"{self.name} stopped at the {self.max_tokens}-token ceiling with the "
                    f"answer unfinished, so it is not valid JSON. Send fewer part numbers "
                    f"per call (see extract.chunk) or raise max_tokens. ({error})",
                    elapsed_ms=stats.latency_ms,
                ) from error
            # A schema-constrained decode that produced non-JSON means the server
            # ignored `response_format`. Worth saying plainly: it is a deployment
            # fact, not a fact about this datasheet.
            raise ModelUnavailable(
                f"{self.name} returned content that is not JSON; "
                f"does {self.model} support constrained decoding? ({error})",
                elapsed_ms=stats.latency_ms,
            ) from error

        # Everything the schema was supposed to guarantee is checked again here.
        # See the module docstring: the server that ignored the schema is exactly
        # the one whose output nobody validated.
        return parse_response(decoded, request, provider=self.name, model=self.model, stats=stats)

    def _user_message(self, request: ExtractionRequest) -> str:
        """The document and the part numbers, in that order.

        The document goes **last** in the prompt body but the instruction goes
        first, because a long datasheet between the instruction and the question is
        how the question gets lost. The part numbers are repeated here as well as
        being an `enum` in the schema: the schema constrains what may be *emitted*,
        and this is what the model is asked to *look for*.
        """
        wanted = "\n".join(f"- {mpn}" for mpn in request.mpns)
        fields = "\n".join(f"- {field.describe()}" for field in request.fields)
        return (
            f"Part numbers to find in this datasheet:\n{wanted}\n\n"
            f"Fields to report for each:\n{fields}\n\n"
            f"Datasheet text follows.\n\n---\n{request.document_text}"
        )

    def _post(self, payload: dict[str, Any], *, started: float) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                decoded: Any = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read()[:500].decode("utf-8", "replace")
            raise ModelUnavailable(
                f"HTTP {error.code} from {url}: {detail}", elapsed_ms=elapsed_ms(started)
            ) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ModelUnavailable(
                f"{type(error).__name__} calling {url}: {error}", elapsed_ms=elapsed_ms(started)
            ) from error
        if not isinstance(decoded, dict):
            raise ModelUnavailable(
                f"{url} returned {type(decoded).__name__}, not an object",
                elapsed_ms=elapsed_ms(started),
            )
        return decoded


def stats_from_openai_body(body: dict[str, Any], *, started: float) -> CallStats:
    """What the server said the call cost, defaulting to silence rather than zero.

    Everything here is best-effort by design: `usage` is optional in the response
    shape and a server that omits it is not misbehaving. A count this cannot read
    stays `None`, because a benchmark that silently recorded zero tokens would
    average them in and understate every model that reported honestly.
    """
    usage = body.get("usage")
    prompt = completion = None
    if isinstance(usage, dict):
        prompt = _count(usage.get("prompt_tokens"))
        completion = _count(usage.get("completion_tokens"))

    finish_reason = None
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        raw = choices[0].get("finish_reason")
        if isinstance(raw, str) and raw:
            finish_reason = raw

    return CallStats(
        latency_ms=elapsed_ms(started),
        prompt_tokens=prompt,
        completion_tokens=completion,
        finish_reason=finish_reason,
    )


def _count(value: object) -> int | None:
    # `bool` is an `int` and would report True as 1 token. Nothing sane sends
    # one, which is exactly why it would go unnoticed.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
