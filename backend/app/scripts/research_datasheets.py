"""The research worker: claim a part, propose, fetch, validate, store, report.

    python -m app.scripts.research_datasheets --base-url https://almagest.aether.lan --once

ADR 0017's other half, and deliberately the twin of `extract_datasheets`. Same
`ApiClient` Protocol so the tests drive it through FastAPI's `TestClient` against
the real routes; same `--once` posture so a run is a Job that exits; same
standard-library `urllib` so the worker image needs no HTTP client on top of its
parser; same three-way failure split.

The one thing this worker does that no other process in the repository does is
**reach the open internet**. That is why the validation is not optional and not
inline: every fetched body goes through `app.services.datasheet_validation`, whose
whole job is to decide whether the PDF that came back is *this part's* datasheet
rather than a real datasheet for something else.

## The shape of one part's run

    providers.gather(...)      propose URLs, best rank first, deduplicated
      for each candidate:
        fetch                  -> fetch_failed on anything that is not a body
        validate               -> not_pdf | too_large | parse_failed | mpn_absent
        upload + attach        -> the blob store, linked as this part's datasheet
      submit the whole list    -> the API derives resolved | exhausted

**Every candidate is fetched, even after one has validated.** The extra fetches are
cheap and the alternative is worse: a part that resolves on its second candidate
tells you nothing about why the first was wrong, and the rejections are the entire
diagnostic value of the candidate table. Stopping early optimises the case that
already worked at the expense of the case that did not.

## Only the first validated candidate becomes the primary

Later ones are recorded as validated and stored, but uploaded with
`is_primary=false`. Ranking already put the most trustworthy proposer first, and a
part whose primary datasheet flips depending on which fetch finished first is a
part whose datasheet link is not reproducible.

## What is deliberately absent

No model. The cascade as shipped is `manual` and `url_pattern` — pure string
construction — and the network-backed providers are constructed only when their
configuration is present. A missing provider is a narrower cascade, never a failed
run, which is the same graceful degradation ADR 0005 gives extraction: the fancy
half can be absent and the cheap half still delivers a datasheet for every part
whose manufacturer publishes a predictable URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

from app.models.enums import ResearchCandidateState
from app.services.datasheet_validation import MAX_DATASHEET_BYTES, Verdict, validate
from app.services.enrichment.providers import (
    Candidate,
    DatasheetProvider,
    PartQuery,
    default_providers,
    gather,
)
from app.services.extractors import (
    DEFAULT_EXTRACTOR,
    Extractor,
    ExtractorUnavailable,
    build_extractor,
    extractor_names,
)
from app.services.research import RejectReason

log = logging.getLogger("almagest.research")

#: Seconds one HTTP call may take. Shorter than extraction's, because these are
#: calls to *other people's* servers: a manufacturer CDN that has not answered in
#: sixty seconds is not going to, and a run that hangs on one of them holds a lease
#: the whole time.
DEFAULT_TIMEOUT = 60.0

#: Parts per claim. One by default, as extraction: a lease is held for everything
#: claimed, so a worker that grabs a batch and dies parks all of it until the leases
#: expire.
DEFAULT_LIMIT = 1

#: Sent on every outbound fetch. A real contact string rather than a spoofed browser
#: — this is a robot fetching manufacturer PDFs, and saying so is both honest and
#: what keeps the traffic from looking like something worth blocking.
USER_AGENT = "almagest-research/0.1 (+https://github.com/IliTheButterfly/almagest)"


@dataclass(frozen=True)
class QueuedPart:
    """One leased part, as the claim route reported it."""

    part_id: int
    name: str
    mpn: str | None
    #: The catalogue's normalisation — **not** re-derived here. See
    #: `app.services.datasheet_validation` for why a second normaliser is a silent
    #: failure rather than a loud one.
    mpn_norm: str | None
    manufacturer: str | None
    attempts: int


@dataclass(frozen=True)
class Fetched:
    """A body that came back, or the reason none did."""

    data: bytes | None
    content_type: str | None = None
    error: str | None = None


class ApiClient(Protocol):
    """The four calls the worker makes. A Protocol so the tests drive the loop
    in-process through the real routes, without a socket or a port."""

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedPart]: ...

    def upload_datasheet(
        self, *, part_id: int, data: bytes, source_url: str, is_primary: bool
    ) -> str: ...

    def submit_candidates(self, *, part_id: int, candidates: Sequence[dict[str, Any]]) -> None: ...

    def submit_failure(self, *, part_id: int, error: str) -> None: ...


class Fetcher(Protocol):
    """The one thing that touches the open internet, behind a seam.

    Separated from `ApiClient` because it is a different trust boundary entirely:
    the API is ours and answers a known contract, while these are arbitrary third
    parties serving whatever they like. Having it be a Protocol is what lets the
    whole worker be tested offline against recorded bodies — including the bodies
    that are *not* PDFs, which are the interesting ones.
    """

    def fetch(self, url: str) -> Fetched: ...


class HttpFetcher:
    """`Fetcher` over `urllib.request`, with a size ceiling enforced while reading.

    The ceiling is applied to the *stream*, not to the finished body: a URL that
    serves a hundred-gigabyte file must not be able to make the worker allocate a
    hundred gigabytes before anybody checks. `read(n + 1)` reads one byte past the
    limit so "exactly at the ceiling" and "over it" stay distinguishable.
    """

    def __init__(
        self, *, timeout: float = DEFAULT_TIMEOUT, max_bytes: int = MAX_DATASHEET_BYTES
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes

    def fetch(self, url: str) -> Fetched:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            # A provider — or a model — proposing `file:///etc/passwd` is exactly the
            # kind of thing that must be refused at the door rather than handed to
            # urllib, which would happily open it.
            return Fetched(None, error=f"refused scheme {parsed.scheme!r}")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data: bytes = response.read(self.max_bytes + 1)
                content_type = response.headers.get("Content-Type")
        except urllib.error.HTTPError as error:
            return Fetched(None, error=f"HTTP {error.code}")
        except (urllib.error.URLError, OSError, ValueError) as error:
            # `URLError` covers DNS and TLS; `OSError` the socket timeouts; `ValueError`
            # a URL malformed enough that urllib will not even try. All three are the
            # same fact about this candidate: nothing was served.
            return Fetched(None, error=f"{type(error).__name__}: {error}")
        return Fetched(data, content_type=content_type)


class HttpApiClient:
    """`ApiClient` over `urllib.request`. No retries — the queue's lease is the retry."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self.timeout = timeout

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedPart]:
        body = self._post_json("api/research/claims", {"worker_id": worker_id, "limit": limit})
        return [
            QueuedPart(
                part_id=int(claim["part_id"]),
                name=str(claim["name"]),
                mpn=claim["mpn"],
                mpn_norm=claim["mpn_norm"],
                manufacturer=claim["manufacturer"],
                attempts=int(claim["attempts"]),
            )
            for claim in body.get("claims", [])
        ]

    def upload_datasheet(
        self, *, part_id: int, data: bytes, source_url: str, is_primary: bool
    ) -> str:
        """Store the PDF and attach it to the part in one call. Returns the sha256.

        Dedup is free and happens on the API side: the store is content-addressed,
        so a family sheet already fetched for a sibling part is recognised by its
        hash and not written twice.
        """
        query = urllib.parse.urlencode(
            {
                "media_type": "application/pdf",
                "part_id": part_id,
                "source_url": source_url[:2048],
                "role": "datasheet",
                "is_primary": str(is_primary).lower(),
            }
        )
        request = urllib.request.Request(
            urljoin(self.base_url, f"api/documents?{query}"),
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            decoded: Any = json.loads(response.read() or b"{}")
        return str(decoded["document"]["sha256"])

    def submit_candidates(self, *, part_id: int, candidates: Sequence[dict[str, Any]]) -> None:
        self._post_json(
            "api/research/results", {"part_id": part_id, "candidates": list(candidates)}
        )

    def submit_failure(self, *, part_id: int, error: str) -> None:
        self._post_json("api/research/results", {"part_id": part_id, "error": error[:2000]})

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


def _rejection(candidate: Candidate, reason: str, note: str | None) -> dict[str, Any]:
    return {
        "source": candidate.source,
        "url": candidate.url,
        "state": ResearchCandidateState.REJECTED.value,
        "reject_reason": reason,
        "rank": candidate.rank,
        "note": note,
    }


def process_one(
    client: ApiClient,
    fetcher: Fetcher,
    extractor: Extractor,
    providers: Sequence[DatasheetProvider],
    part: QueuedPart,
) -> bool:
    """Research one claimed part. True if a datasheet was found and stored.

    `ExtractorUnavailable` is deliberately **not** caught, for the reason
    `extract_datasheets.process_one` gives: a missing parser is a deployment error
    and reporting it per part would spend the whole queue's attempts on it.

    A part with no MPN is reported as an empty candidate list rather than an error.
    It is not broken — there is simply nothing to search on, and `exhausted` is the
    honest state for it. Marking it `failed` would put it in a health check that is
    supposed to surface things worth fixing in the code.
    """
    if not part.mpn or not part.mpn_norm:
        log.info("part %d (%s) has no MPN; nothing to search on", part.part_id, part.name)
        client.submit_candidates(part_id=part.part_id, candidates=[])
        return False

    query = PartQuery(
        mpn=part.mpn,
        mpn_norm=part.mpn_norm,
        manufacturer=part.manufacturer,
        name=part.name,
    )
    proposed = gather(providers, query)
    log.info("part %d (%s): %d candidates proposed", part.part_id, part.mpn, len(proposed))

    reports: list[dict[str, Any]] = []
    found = False
    for candidate in proposed:
        fetched = fetcher.fetch(candidate.url)
        if fetched.data is None:
            reports.append(_rejection(candidate, RejectReason.FETCH_FAILED, fetched.error))
            continue

        verdict: Verdict = validate(
            fetched.data,
            mpn_norm=part.mpn_norm,
            extractor=extractor,
            content_type=fetched.content_type,
        )
        if not verdict.accepted:
            reports.append(_rejection(candidate, verdict.reason or "unknown", verdict.note))
            continue

        # The first validated candidate becomes the primary; later ones are stored
        # and linked but do not steal it. See the module docstring.
        sha256 = client.upload_datasheet(
            part_id=part.part_id,
            data=fetched.data,
            source_url=candidate.url,
            is_primary=not found,
        )
        reports.append(
            {
                "source": candidate.source,
                "url": candidate.url,
                "state": ResearchCandidateState.VALIDATED.value,
                "document_sha256": sha256,
                "rank": candidate.rank,
                "note": candidate.note,
            }
        )
        found = True

    client.submit_candidates(part_id=part.part_id, candidates=reports)
    log.info(
        "part %d: %s (%d tried)",
        part.part_id,
        "resolved" if found else "exhausted",
        len(reports),
    )
    return found


def run_once(
    client: ApiClient,
    fetcher: Fetcher,
    extractor: Extractor,
    providers: Sequence[DatasheetProvider],
    *,
    worker_id: str,
    limit: int = DEFAULT_LIMIT,
) -> int:
    """Claim one batch and work it. Returns how many parts got a datasheet."""
    claimed = client.claim(worker_id=worker_id, limit=limit)
    if not claimed:
        return 0
    found = 0
    for part in claimed:
        try:
            found += process_one(client, fetcher, extractor, providers, part)
        except ExtractorUnavailable:
            raise
        except Exception as error:  # a provider's exception types are not enumerable
            # The run broke rather than the part being unfindable. Reported as a
            # failure so the queue retries it, which is the difference between a
            # transient egress fault and a genuinely obscure part.
            log.warning("research failed for part %d: %s", part.part_id, error)
            client.submit_failure(part_id=part.part_id, error=f"{type(error).__name__}: {error}")
    return found


def run(
    client: ApiClient,
    fetcher: Fetcher,
    extractor: Extractor,
    providers: Sequence[DatasheetProvider],
    *,
    worker_id: str,
    limit: int = DEFAULT_LIMIT,
    poll_seconds: float = 0.0,
    max_batches: int | None = 1,
) -> int:
    """Work the queue until it is empty, or forever if asked.

    `max_batches=1` (what `--once` means) is the CronJob posture: one batch, then
    exit. `poll_seconds` is for a laptop drain, and `max_batches` still bounds it,
    because a polling worker nobody bounded is one that outlives its reason.
    """
    found = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        batches += 1
        done = run_once(client, fetcher, extractor, providers, worker_id=worker_id, limit=limit)
        found += done
        if done:
            continue
        if poll_seconds <= 0:
            break
        time.sleep(poll_seconds)
    return found


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.research_datasheets",
        description=(
            "Claim parts needing a datasheet from the Almagest API, propose "
            "candidate URLs, fetch and validate each, and report what was tried "
            "(docs/adr/0017-the-researcher-proposes-and-never-asserts.md)."
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
        help="Which extractor validates a candidate's text. 'pypdf' needs the 'datasheets' extra.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Parts per claim.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim one batch and exit. The default, and the CronJob posture.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0.0,
        help="Sleep this long on an empty claim instead of exiting. For a drain.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Stop after this many batches. Unset with --poll-seconds means forever.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    worker_id = args.worker_id or socket.gethostname()[:64]
    max_batches = 1 if args.once else args.max_batches

    try:
        extractor = build_extractor(args.extractor)
    except ExtractorUnavailable as error:
        log.error("%s", error)
        return 2

    found = run(
        HttpApiClient(args.base_url),
        HttpFetcher(),
        extractor,
        default_providers(),
        worker_id=worker_id,
        limit=args.limit,
        poll_seconds=args.poll_seconds,
        max_batches=max_batches,
    )
    log.info("done: %d part(s) got a datasheet", found)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
