"""The real `FieldProposalProvider`: an OpenAI-compatible endpoint, schema-constrained.

The transport half of `field_discovery.py`, and a deliberate near-copy of
`openai_compat.py` — whose module docstring argues every decision here and whose
arguments transfer unchanged: one class serves vLLM, Ollama and a frontier API;
the schema goes on the wire as a *constraint* and `parse_response` re-checks it
anyway; `temperature=0` because reading a datasheet is a task with a right answer;
no in-provider retry, because a malformed answer from a model that was handed a
schema is a fact worth surfacing rather than papering over; and `urllib` rather
than an SDK.

What is **not** shared is anything about the answer's meaning, which is why this is
a sibling file rather than a parameter on the existing class. `ModelUnavailable`
and `stats_from_openai_body` are imported, and `elapsed_ms` comes from
`app.services.timing`, because all three are about the wire and the clock; the
prompt, the schema and the parser are this pass's own.

## The 27B is the intended model and nothing here runs it automatically

`app.scripts.propose_fields` is `--once`/`--fixture` only, with no queue, no lease
and no Deployment. A larger model on the one card is a second GPU consumer that
would compete with the capture-dispatch drain (ADR 0021), and that contention needs
its own decision rather than being introduced as a side effect of this transport
existing. See the worker's docstring.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.services.enrichment.field_discovery import (
    SYSTEM_PROMPT,
    FieldDiscoveryRequest,
    FieldDiscoveryResult,
    parse_response,
    schema_for,
)
from app.services.enrichment.openai_compat import ModelUnavailable, stats_from_openai_body
from app.services.timing import elapsed_ms

logger = logging.getLogger(__name__)

#: Seconds. Generous: a whole datasheet in the prompt and a reasoning model on a
#: shared card is minutes, not seconds, and a timeout that fires mid-read wastes
#: the GPU handover the run cost.
DEFAULT_TIMEOUT = 600.0


@dataclass
class OpenAICompatFieldDiscoveryProvider:
    """`FieldProposalProvider` over any `/v1/chat/completions` endpoint.

    `name` says *which deployment* answered (`local-vllm`, `ollama`) rather than
    repeating the model name, which is stored separately — both land on every
    evidence row, so a second pass under a different model leaves the comparison
    visible in the rows.
    """

    base_url: str
    model: str
    name: str = "local-vllm"
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    #: Sent as `max_tokens`. ADR 0021's measurement is that the **reasoning budget**,
    #: not the input, is what sets this: one observed run spent 12 318 characters
    #: thinking and answered nothing. A dozen field proposals with quotes is a few
    #: hundred tokens of answer, so the headroom here is entirely for the thinking.
    max_tokens: int = 8192

    def propose(self, request: FieldDiscoveryRequest) -> FieldDiscoveryResult:
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
                "json_schema": {"name": "field_discovery", "strict": True, "schema": schema},
            },
            # vLLM's own spelling of the same constraint. Servers ignore fields they
            # do not recognise, so sending both costs a few bytes and covers both.
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
            if stats.truncated:
                raise ModelUnavailable(
                    f"{self.name} stopped at the {self.max_tokens}-token ceiling with the "
                    f"answer unfinished, so it is not valid JSON. Send less document text "
                    f"or raise max_tokens. ({error})",
                    elapsed_ms=stats.latency_ms,
                ) from error
            raise ModelUnavailable(
                f"{self.name} returned content that is not JSON; "
                f"does {self.model} support constrained decoding? ({error})",
                elapsed_ms=stats.latency_ms,
            ) from error

        return parse_response(decoded, request, provider=self.name, model=self.model, stats=stats)

    def _user_message(self, request: FieldDiscoveryRequest) -> str:
        """The vocabulary first, the document last.

        The existing fields and the pickable categories go **before** the datasheet
        for `openai_compat._user_message`'s reason: a long document between the
        instruction and the question is how the question gets lost. The existing
        field list is repeated here as well as in the schema's description because
        the schema constrains what may be *emitted* and this is what the model is
        asked to compare against.
        """
        existing = "\n".join(f"- {row.describe()}" for row in request.existing) or "- (none)"
        categories = "\n".join(f"- {slug}" for slug in request.categories) or "- (none)"
        units = ", ".join(request.base_units) or "(none)"
        covers = ("\n".join(f"- {mpn}" for mpn in request.mpns)) or "- (not stated)"
        return (
            f"Fields the inventory already has:\n{existing}\n\n"
            f"Part categories you may name (and only these):\n{categories}\n\n"
            f"Physical quantities a numeric field may be measured in: {units}\n\n"
            f"Part numbers this datasheet covers:\n{covers}\n\n"
            "Report parameters this datasheet specifies that are missing from the list "
            "above. Quote the line for each. Datasheet text follows.\n\n---\n"
            f"{request.document_text}"
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
