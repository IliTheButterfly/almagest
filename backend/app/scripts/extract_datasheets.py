"""The extraction worker: claim, fetch, extract, submit, exit.

    python -m app.scripts.extract_datasheets --base-url https://almagest.aether.lan --once

ADR 0005's other half. This process is the only thing in the repository that opens
a PDF, and it reaches the system **over HTTP** — never through the database and
never through the volume. That is not a layering preference: the datastore is SQLite
on a ReadWriteOnce volume with exactly one writer, and a second process holding the
same file is corruption. The API is the writer; this is a client of it.

It lives under `app/scripts/` because it shares the repo (and therefore the OpenAPI
contract, the enums and `app.services.extractors`) with the API, and runs from a
**different image**: the worker's requirements install the `datasheets` extra, and
Docling's if that path is wanted. Nothing the API serves imports this module.

## Shape: a Job that releases the device, not a daemon

`--once` claims a batch, works it, and exits. That is the default posture, because
`CLAUDE.local.md`'s GPU rule for the co-tenanted host is that **a free unit is a
race, not a reservation** — the extraction stage runs as a Job/CronJob that releases
the device between runs. `--poll-seconds` exists for a laptop run and a drain, and
even then `--max-batches` bounds it so a forgotten worker is not an immortal one.

## Failures

Three kinds, deliberately handled differently:

* **A bad document** (a truncated PDF, an encrypted one, a parser exception) is
  reported through the submit door as a failure. The queue counts the attempt and
  offers it again until its attempts run out; the worker moves to the next document
  rather than stopping, because one unreadable datasheet must not park a backlog.
* **A missing extractor** (`ExtractorUnavailable`) is *not* reported per document.
  Nothing is wrong with the document, and reporting it would burn the attempts of
  everything in the batch against a deployment mistake. It aborts the run loudly.
* **A dead worker** is not handled here at all, and cannot be: a `SIGKILL` or a
  segfault inside a C extension runs no code. The API's lease is what recovers it,
  which is why the attempt is counted when the claim is granted rather than when a
  failure is reported.

## Why `urllib` and no HTTP client library

Three requests, no auth, no retries worth the name (the queue's lease is the retry).
`urllib.request` is in the standard library, so the worker image needs no client
dependency on top of its parser — and the API, which shares this repo, gains none
either. `ApiClient` is a Protocol so the tests drive the loop through FastAPI's
`TestClient` against the real routes instead of a socket.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

from app.services.extractors import (
    DEFAULT_EXTRACTOR,
    Extractor,
    ExtractorUnavailable,
    build_extractor,
    extractor_names,
)

log = logging.getLogger("almagest.extract")

#: Seconds a single HTTP call may take. Generous for the submit of a 400-page
#: document's text; a claim or a blob fetch that takes this long is a broken link
#: and failing is better than hanging a CronJob until the next one overlaps it.
DEFAULT_TIMEOUT = 120.0

#: Documents per claim. One by default: a lease is held for everything claimed, so
#: a worker that grabs a batch and dies parks the whole batch until the leases
#: expire. Batching is an optimisation for a big backfill, not the normal case.
DEFAULT_LIMIT = 1


@dataclass(frozen=True)
class QueuedDocument:
    """One leased document, as the claim route reported it."""

    sha256: str
    media_type: str
    byte_size: int
    #: Relative to the API base, exactly as the API returned it. Resolved by the
    #: client rather than rebuilt here, so the worker never has to know the store's
    #: URL shape.
    url: str
    attempts: int


class ApiClient(Protocol):
    """The three calls the worker makes. A Protocol so it can be driven by a
    `TestClient` in-process — which is how the loop below is tested against the real
    routes without a socket, a port, or a running server."""

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedDocument]: ...

    def download(self, document: QueuedDocument) -> bytes: ...

    def submit_text(self, *, sha256: str, extractor: str, pages: Sequence[str]) -> None: ...

    def submit_failure(self, *, sha256: str, extractor: str, error: str) -> None: ...


class HttpApiClient:
    """`ApiClient` over `urllib.request`.

    No retry logic on purpose. A failed claim means this run does nothing and the
    next one picks the work up; a failed submit means the lease expires and the
    document is offered again. Both are already recoverable by the queue's own
    design, so a retry here would be a second, weaker copy of it.
    """

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        # A trailing slash so `urljoin` treats the base as a directory: without it,
        # `urljoin("http://h/almagest", "/api/x")` and the path-relative case both
        # silently drop a segment.
        self.base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self.timeout = timeout

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedDocument]:
        body = self._post_json("api/extraction/claims", {"worker_id": worker_id, "limit": limit})
        claims = body.get("claims", [])
        return [
            QueuedDocument(
                sha256=str(claim["sha256"]),
                media_type=str(claim["media_type"]),
                byte_size=int(claim["byte_size"]),
                url=str(claim["url"]),
                attempts=int(claim["attempts"]),
            )
            for claim in claims
        ]

    def download(self, document: QueuedDocument) -> bytes:
        # `lstrip("/")` because the API returns a root-relative URL and `urljoin`
        # would otherwise discard any base path the install is mounted under.
        request = urllib.request.Request(
            urljoin(self.base_url, document.url.lstrip("/")), method="GET"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data: bytes = response.read()
        return data

    def submit_text(self, *, sha256: str, extractor: str, pages: Sequence[str]) -> None:
        self._post_json(
            "api/extraction/results",
            {"sha256": sha256, "extractor": extractor, "pages": list(pages)},
        )

    def submit_failure(self, *, sha256: str, extractor: str, error: str) -> None:
        self._post_json(
            "api/extraction/results",
            {"sha256": sha256, "extractor": extractor, "error": error[:2000]},
        )

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            urljoin(self.base_url, path),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            decoded: Any = json.loads(response.read() or b"{}")
        return decoded if isinstance(decoded, dict) else {}


def process_one(client: ApiClient, extractor: Extractor, document: QueuedDocument) -> bool:
    """Extract one claimed document and report the outcome. True if text was stored.

    `ExtractorUnavailable` is deliberately **not** caught: a missing parser is a
    deployment error, not a fact about this document, and reporting it as a
    per-document failure would spend the attempts of everything in the queue on it.

    Everything else is. A parser raising on one malformed PDF must not end the run —
    one unreadable datasheet parking a backlog is precisely the failure mode a queue
    with attempts and a lease exists to avoid.
    """
    data = client.download(document)
    try:
        extracted = extractor.extract(data)
    except ExtractorUnavailable:
        raise
    except Exception as error:  # a parser's exception types are not enumerable
        log.warning("extraction failed for %s: %s", document.sha256[:12], error)
        client.submit_failure(
            sha256=document.sha256,
            extractor=extractor.name,
            error=f"{type(error).__name__}: {error}",
        )
        return False

    client.submit_text(
        sha256=document.sha256, extractor=extractor.name, pages=list(extracted.pages)
    )
    # Logged rather than judged here: the API makes the low-confidence judgement from
    # the text it stores, so a worker that decided for itself would be a second
    # opinion with no authority. The numbers are worth a line anyway — a run of
    # zero-character pages is what an operator wants to see in a log.
    log.info(
        "extracted %s: %d pages, %d chars (%s)",
        document.sha256[:12],
        extracted.page_count,
        extracted.char_count,
        extractor.name,
    )
    return True


def run_once(
    client: ApiClient, extractor: Extractor, *, worker_id: str, limit: int = DEFAULT_LIMIT
) -> int:
    """Claim one batch and work it. Returns how many documents were extracted."""
    claimed = client.claim(worker_id=worker_id, limit=limit)
    if not claimed:
        return 0
    return sum(process_one(client, extractor, document) for document in claimed)


def run(
    client: ApiClient,
    extractor: Extractor,
    *,
    worker_id: str,
    limit: int = DEFAULT_LIMIT,
    poll_seconds: float = 0.0,
    max_batches: int | None = 1,
) -> int:
    """Work the queue until it is empty, or forever if asked.

    `max_batches=1` (the default, and what `--once` means) is the CronJob posture:
    one batch, then exit and release whatever the image was holding. With
    `poll_seconds` set, an empty claim sleeps instead of returning — and
    `max_batches` still bounds the loop, because the failure mode of a polling
    worker nobody bounded is one that outlives the reason it was started.
    """
    extracted = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        batches += 1
        done = run_once(client, extractor, worker_id=worker_id, limit=limit)
        extracted += done
        if done:
            continue
        if poll_seconds <= 0:
            break
        time.sleep(poll_seconds)
    return extracted


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.extract_datasheets",
        description=(
            "Claim documents needing text from the Almagest API, extract it, and "
            "submit it back. Runs outside the API process by design "
            "(docs/adr/0005-extraction-runs-outside-the-api.md)."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="API root. The worker never touches the database or the blob volume.",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Recorded on the lease for diagnostics. Defaults to the hostname.",
    )
    parser.add_argument(
        "--extractor",
        default=DEFAULT_EXTRACTOR,
        choices=extractor_names(),
        help=(
            "Which extractor to run. 'pypdf' needs the 'datasheets' extra; "
            "'docling' is never installed by this repo and needs a worker image "
            "built with it."
        ),
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Documents per claim.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="One batch then exit — the CronJob posture, and the default behaviour.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0.0,
        help="Sleep this long on an empty queue instead of exiting. Implies a loop.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Stop after this many claims. Omit with --poll-seconds to run until killed.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # `--once` and an explicit `--max-batches` say the same thing; the flag is the
    # readable spelling of the common case. Without either, a `--poll-seconds` run is
    # unbounded and anything else is one batch.
    max_batches = args.max_batches
    if args.once or (max_batches is None and args.poll_seconds <= 0):
        max_batches = 1

    worker_id = args.worker_id or _default_worker_id()
    try:
        extractor = build_extractor(args.extractor)
    except ExtractorUnavailable as error:
        log.error("%s", error)
        return 2

    client = HttpApiClient(args.base_url, timeout=args.timeout)
    try:
        extracted = run(
            client,
            extractor,
            worker_id=worker_id,
            limit=args.limit,
            poll_seconds=args.poll_seconds,
            max_batches=max_batches,
        )
    except ExtractorUnavailable as error:
        # Raised on the first `extract`, not at construction — so this is where a
        # worker image missing its parser reports itself, with a document in hand.
        log.error("%s", error)
        return 2
    except urllib.error.URLError as error:
        log.error("cannot reach the API at %s: %s", args.base_url, error)
        return 1

    log.info("extracted %d document(s)", extracted)
    return 0


def _default_worker_id() -> str:
    """The hostname, which in a Job is the pod name — the thing an operator would
    grep for after seeing a stuck lease."""
    return socket.gethostname()[:64]


if __name__ == "__main__":
    raise SystemExit(main())
