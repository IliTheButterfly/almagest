"""The field-discovery pass: read a datasheet already stored, propose missing fields.

    python -m app.scripts.propose_fields --sha256 <hash> --fixture f.json --once
    python -m app.scripts.propose_fields --sha256 <hash> --model-url http://... --model qwen3:27b

The fourth sibling of `extract_datasheets`, `research_datasheets` and
`dispatch_captures`: same `ApiClient` Protocol so the tests drive it through
FastAPI's `TestClient` against the real routes, same standard-library `urllib`, same
`--once` posture, same `--fixture` replay so the whole path runs with no GPU.

## It creates nothing, and there is no call in its path that could

The output goes to `POST /api/field-proposals`, which records a worklist entry with
a quote attached. `POST /api/parameter-fields` — the only writer of
`parameter_template` — is never called here and is not in this module's vocabulary.
A person presses the button, and `tests/integration/test_field_proposals.py` runs a
whole pass and asserts the template count is unchanged.

The reason is the **duplicate**, not the bad row. A `parameter_template` is shared
by every part in its category, so a second template for one concept fragments
parametric search silently — the filter is a join on `template_id`, and a part with
a value under `on_resistance` reads as having none under `Rds(on)`. Nothing about
that surfaces as an error. `app.services.field_proposals.nearest_existing_template`
is what catches it, and it runs on the API side on every submission.

## There is no queue, and that is deliberate rather than unfinished

The other three workers claim from a lease. This one takes `--sha256` and exits.
Adding a queue means deciding when a 27B model gets the card, and the card is one,
exclusive and co-tenanted (ADR 0016): a field-discovery pass competing with the
capture-dispatch drain (ADR 0021) is a contention question with real consequences
for the thing somebody is standing at a bench waiting for. That decision has not
been made, so this stays a pass somebody runs.

Consequences worth stating plainly rather than discovering:

* nothing schedules it, so a datasheet uploaded today produces no proposals until
  somebody asks;
* there is no lease, so two concurrent runs over one document would both submit —
  which is harmless, because submission is an upsert on `(proposal, source,
  document)` and the second lands the same rows;
* `--fixture` is the tested path and the model path is not, in the same sense
  ADR 0021's was not until it ran: see the report at the end of this chunk.

## Why `--sha256` and not "every document"

Because the honest work-selection query — every document with extracted text — is
also "spend a GPU on the whole library", and at the point where that is the right
thing to do it wants the queue this module does not have. A pass over a handful of
sheets somebody just added is what the feature is for on day one.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.services.enrichment.field_discovery import (
    DEFAULT_MAX_PROPOSALS,
    ExistingField,
    FakeFieldDiscoveryProvider,
    FieldDiscoveryFixtureMiss,
    FieldDiscoveryResponseError,
    FieldProposalProvider,
    request_for,
)
from app.services.enrichment.openai_compat import ModelUnavailable

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

#: Characters of datasheet text sent in one call.
#:
#: A 400-page datasheet is over a million characters and no context window takes it.
#: Truncation is the honest failure here rather than a chunking loop: the parametric
#: table is at the front of essentially every datasheet, and a chunked pass over the
#: application notes at the back produces proposals about the example circuit. A run
#: that truncated says so in its log.
MAX_DOCUMENT_CHARS = 60_000


@dataclass(frozen=True)
class Vocabulary:
    """What the install already has, fetched once per run.

    The three lists the schema is built from. Fetched over HTTP rather than read
    from a `Session`, for ADR 0005's reason: two SQLite writers is corruption and
    the single-replica deployment exists to protect exactly that.
    """

    existing: tuple[ExistingField, ...]
    categories: tuple[str, ...]
    base_units: tuple[str, ...]


class ApiClient(Protocol):
    """The API surface this worker uses. A Protocol so the tests can be real.

    Four calls, three of them reads. Note what is **not** here: nothing that creates
    a `parameter_template`. That absence is the design — see the module docstring —
    and it is asserted rather than described.
    """

    def vocabulary(self) -> Vocabulary: ...

    def document_text(self, sha256: str) -> str | None: ...

    def submit_proposal(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpApiClient:
    """`ApiClient` over HTTP, `urllib` only."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._timeout = timeout

    def vocabulary(self) -> Vocabulary:
        fields = self._get_json("api/parameter-fields")
        categories = self._get_json("api/part-categories")
        units = self._get_json("api/parameter-fields/base-units")
        return Vocabulary(
            existing=tuple(
                ExistingField(
                    name=str(row["name"]),
                    display_name=str(row["display_name"]),
                    value_type=str(row["value_type"]),
                    base_unit=row.get("base_unit"),
                    applies_to_category=row.get("applies_to_category"),
                )
                for row in _rows(fields)
            ),
            categories=tuple(str(row["slug"]) for row in _rows(categories)),
            base_units=tuple(str(row["name"]) for row in _rows(units)),
        )

    def document_text(self, sha256: str) -> str | None:
        body = self._get_json(f"api/documents/{urllib.parse.quote(sha256)}/text")
        if not isinstance(body, dict):
            raise RuntimeError(f"unexpected body for document {sha256}: {type(body).__name__}")
        text = body.get("text")
        return text if isinstance(text, str) else None

    def submit_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json("api/field-proposals", payload)

    def _get_json(self, path: str) -> Any:
        url = urllib.parse.urljoin(self._base, path)
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            decoded: Any = json.loads(response.read() or b"null")
        return decoded

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = urllib.parse.urljoin(self._base, path)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            decoded: Any = json.loads(response.read() or b"{}")
        if not isinstance(decoded, dict):
            raise RuntimeError(f"{url} returned {type(decoded).__name__}, not an object")
        return decoded


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RuntimeError(f"expected an array, got {type(value).__name__}")
    return [row for row in value if isinstance(row, dict)]


@dataclass(frozen=True)
class PassResult:
    """What one document's pass did. Reported rather than logged only.

    `submitted` counts proposals that reached the API, which is **not** the same as
    fields created: nothing here creates one. `refused` counts responses
    `parse_response` rejected — an invented category, a missing quote, a unit the
    parser does not answer to — and it is worth a number of its own, because a
    provider that is quietly ignoring the schema shows up here and nowhere else.
    """

    document_ref: str
    proposed: int
    submitted: int
    refused: int
    skipped_reason: str | None = None


def process_one(
    client: ApiClient,
    provider: FieldProposalProvider,
    sha256: str,
    *,
    vocabulary: Vocabulary,
    mpns: Sequence[str] = (),
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
) -> PassResult:
    """One document: read the stored text, ask, submit every proposal.

    A document with no extracted text is **skipped rather than failed**. It is the
    ordinary state of a freshly uploaded PDF and the fix is the extraction queue,
    not this pass — exactly the `EXHAUSTED`-is-not-`FAILED` distinction the other
    queues draw.
    """
    text = client.document_text(sha256)
    if text is None:
        logger.info("%s has no extracted text yet; the extraction queue owns that", sha256)
        return PassResult(sha256, 0, 0, 0, skipped_reason="no_text")
    if not text.strip():
        # Extracted and genuinely empty: a page-image PDF. Also the extraction
        # queue's business (it will be flagged low-confidence), and asking a model
        # to find fields in nothing burns a GPU handover to be told nothing.
        logger.info("%s extracted to no text at all; needs OCR, not a model", sha256)
        return PassResult(sha256, 0, 0, 0, skipped_reason="empty_text")

    if len(text) > MAX_DOCUMENT_CHARS:
        logger.warning(
            "%s is %d characters; sending the first %d. Proposals come from the front "
            "of the sheet, which is where the parametric table is.",
            sha256,
            len(text),
            MAX_DOCUMENT_CHARS,
        )
        text = text[:MAX_DOCUMENT_CHARS]

    request = request_for(
        document_ref=sha256,
        document_text=text,
        existing=vocabulary.existing,
        categories=vocabulary.categories,
        base_units=vocabulary.base_units,
        mpns=mpns,
        max_proposals=max_proposals,
    )
    try:
        result = provider.propose(request)
    except FieldDiscoveryResponseError as error:
        # The model answered and the answer broke the schema. Reported as a refusal
        # rather than raised: it is a fact about this run, the pass over the next
        # document is unaffected, and a provider ignoring `response_format` shows up
        # as a count rather than as a stack trace.
        logger.error("%s: response refused: %s", sha256, error)
        return PassResult(sha256, 0, 0, 1, skipped_reason="malformed_response")
    except (FieldDiscoveryFixtureMiss, ModelUnavailable) as error:
        logger.error("%s: %s", sha256, error)
        return PassResult(sha256, 0, 0, 0, skipped_reason=type(error).__name__)

    submitted = 0
    for proposal in result.proposals:
        client.submit_proposal(
            {
                "name": proposal.name,
                "display_name": proposal.display_name,
                "suggested_value_type": str(proposal.value_type),
                "suggested_base_unit": proposal.base_unit,
                "suggested_category": proposal.category,
                # No `suggested_substitution_direction`. The schema has no such
                # field, so there is nothing to send — see `field_discovery`, and
                # note that `POST /api/parameter-fields` requires one with no
                # default precisely because guessing it makes substitution wrong.
                "evidence": [
                    {
                        "quote": proposal.quote,
                        "document_ref": result.document_ref,
                        "source": "llm_inferred",
                        "stated_value": proposal.stated_value,
                        "page": proposal.page,
                        "note": proposal.note,
                        "provider": result.provider,
                        "model": result.model,
                    }
                ],
            }
        )
        submitted += 1

    logger.info(
        "%s: %d proposal(s) submitted for review by %s/%s",
        sha256,
        submitted,
        result.provider,
        result.model,
    )
    return PassResult(sha256, len(result.proposals), submitted, 0)


def run_once(
    client: ApiClient,
    provider: FieldProposalProvider,
    shas: Sequence[str],
    *,
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
) -> list[PassResult]:
    """Every named document, one call each, vocabulary fetched once.

    Once, not per document: the existing fields do not change during a pass, and
    re-fetching them per sheet would be three HTTP round trips per document to learn
    the same thing. A field created *during* a long pass is therefore missed by it —
    the next submission's dedup check catches that, since `record_proposal` re-runs
    `nearest_existing_template` on the API side.
    """
    vocabulary = client.vocabulary()
    return [
        process_one(client, provider, sha, vocabulary=vocabulary, max_proposals=max_proposals)
        for sha in shas
    ]


def build_provider(args: argparse.Namespace) -> FieldProposalProvider:
    """The fixture replay or the real endpoint. Exactly one of the two."""
    if args.fixture is not None:
        return FakeFieldDiscoveryProvider(Path(args.fixture))
    # Imported here rather than at module scope so `--fixture` needs nothing that
    # speaks to a model, which is what makes the offline path genuinely offline.
    from app.services.enrichment.field_discovery_openai_compat import (
        OpenAICompatFieldDiscoveryProvider,
    )

    return OpenAICompatFieldDiscoveryProvider(
        base_url=args.model_url,
        model=args.model,
        name=args.provider_name,
        api_key=args.api_key,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="propose_fields",
        description=(
            "Read stored datasheet text and propose parameter fields this install has "
            "no template for. Proposes only: creating a field is a person pressing a "
            "button in the PWA, because a duplicate template fragments parametric "
            "search silently."
        ),
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="the Almagest API")
    parser.add_argument(
        "--sha256",
        action="append",
        default=[],
        metavar="HASH",
        help="a stored document to read. Repeatable. There is no queue — see the module docstring.",
    )
    parser.add_argument(
        "--mpn",
        action="append",
        default=[],
        metavar="MPN",
        help="part numbers the datasheet covers, for the prompt. Repeatable.",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help=(
            "replay a recorded response through the real parser instead of calling a "
            "model. No GPU, no network."
        ),
    )
    parser.add_argument("--model-url", default=None, help="an OpenAI-compatible base URL")
    parser.add_argument("--model", default=None, help="the model name to ask for")
    parser.add_argument("--provider-name", default="local-vllm", help="recorded on every sighting")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--max-proposals", type=int, default=DEFAULT_MAX_PROPOSALS)
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "the only mode. Accepted so the invocation reads like its three sibling "
            "workers; there is no loop to opt out of."
        ),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.sha256:
        logger.error("nothing to read: pass --sha256 at least once")
        return 2
    if args.fixture is None and not (args.model_url and args.model):
        logger.error(
            "give either --fixture (replay, no GPU) or both --model-url and --model. "
            "Neither is the default: a pass that silently did nothing would look like "
            "a datasheet with no missing fields."
        )
        return 2

    client = HttpApiClient(args.base_url, timeout=args.timeout)
    try:
        results = run_once(
            client,
            build_provider(args),
            args.sha256,
            max_proposals=args.max_proposals,
        )
    except (urllib.error.URLError, OSError) as error:
        logger.error("could not reach %s: %s", args.base_url, error)
        return 1

    submitted = sum(row.submitted for row in results)
    refused = sum(row.refused for row in results)
    logger.info(
        "%d document(s): %d proposal(s) awaiting a person, %d response(s) refused. "
        "Nothing was created.",
        len(results),
        submitted,
        refused,
    )
    # A refused response is a real failure of the run even though every other
    # document may have gone fine: it is how a provider ignoring the schema becomes
    # visible to whatever ran this.
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
