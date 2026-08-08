"""The dispatch worker: claim a photograph, read it, propose identities, report.

    python -m app.scripts.dispatch_captures --base-url https://almagest.aether.lan --once

ADR 0021's other half, and deliberately the third sibling of `extract_datasheets` and
`research_datasheets`. Same `ApiClient` Protocol so the tests drive it through FastAPI's
`TestClient` against the real routes; same `--once` posture so a run is a Job that exits;
same standard-library `urllib` so the worker image needs no HTTP client; same three-way
failure split.

## What this worker does not do

**It makes no research call and no extraction call.** It reads, proposes, creates stubs
and submits — and then it is finished with the photograph. The chain continues without it
because `Part.research_state` defaults to `PENDING`: every stub this worker mints arrives
in the research queue on its own, and whatever research validates arrives in the
extraction queue on its own. Three workers, three schedules, no worker importing another.

That is not tidiness. ADR 0021's consequence about the GPU is the reason: **one model
swap per drain, not one per capture.** If this worker also researched, the card would
hold a vision model, give it up, take it back, and do that once per photograph. Staged
as separate workers, a drain loads one set of weights, reads every queued photograph, and
hands the card back.

## One stub part per candidate, including the losers

The tempting version mints a stub only for the best reading. That would throw away the
mechanism ADR 0021 is built on: a wrong identity is eliminated by
`datasheet_validation` failing to find its part number in a PDF that was actually
fetched, which is arithmetic a reviewer can check rather than an opinion they cannot. A
candidate with no `parts` row is a candidate research can never test, so by morning the
person choosing would have exactly the model's own ranking and nothing else.

With stubs for all three, the overnight chain does the discriminating: the reading that
has a real datasheet is visibly different from the two that do not.

**The cost is real and worth stating.** A catalogue accumulates stub rows for part
numbers that do not exist — `CFI4JT100K` alongside `CF14JT100K`. Three things bound it:
every one is `is_stub=True` and therefore already in the review queue's remit, the stub
is minted with a `client_op_id` derived from `(intake_id, mpn)` so a re-run never forks
it, and a person choosing among the candidates leaves the others exactly as they were —
stubs, unreferenced, and visible as such. Pruning them is not this worker's job and
deliberately not automatic; deleting a `parts` row a person has not looked at is a worse
default than leaving one.

## The barcode narrows the request, and that is the common case

A capture whose browser-side pass decoded a symbology needs the model only to confirm the
manufacturer and package: `max_candidates` drops to 1 and the fan-out does not happen.
Saying so matters because the fan-out reads like the normal path and is not — ADR 0021
puts the anchored bag at ~5 s and one candidate, the bare module at ~39 s and two.

## What is deliberately absent

No fallback provider. If the vision provider cannot be constructed the worker exits
non-zero without claiming anything, exactly as `extract_datasheets` refuses to start
without an extractor: a missing model is a deployment error, and reporting it per
photograph would spend the whole queue's attempts discovering it — which here means
spending GPU handovers.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin

from app.services.enrichment.vision import (
    DEFAULT_MAX_CANDIDATES,
    FakeVisionProvider,
    VisionProvider,
    VisionRequest,
    VisionResult,
)

log = logging.getLogger("almagest.dispatch")

#: Seconds one HTTP call may take. Long, and for a different reason from research's
#: sixty: these are calls to *our own* model server, which may be loading weights onto a
#: card another workload just gave up. ADR 0021 measured a hard read at 39 s of inference
#: alone, and the handover in front of it is not instant. The lease is 1800 s, so this
#: stays well inside it.
DEFAULT_TIMEOUT = 300.0

#: Photographs per claim. One by default, as the other two workers: a lease is held for
#: everything claimed, so a worker that grabs a batch and dies parks all of it until the
#: leases expire — and here a parked lease is thirty minutes.
DEFAULT_LIMIT = 1

#: What a stub part is called when the model named no manufacturer. The MPN alone, which
#: is what `PendingRow` in the PWA does with a scanned label for the same reason: the
#: name is a human handle and the MPN is the identity.
DEFAULT_PART_KIND = "component"


@dataclass(frozen=True)
class QueuedCapture:
    """One leased entry, as the claim route reported it."""

    intake_id: int
    capture_id: int
    capture_sha256: str
    media_type: str
    #: What the browser decoded. **An anchor, not an instruction** — see the module
    #: docstring. Non-empty narrows the read to a single candidate.
    barcode_texts: tuple[str, ...]
    #: What the browser's OCR read. The least reliable input and the most useful to
    #: repair.
    ocr_lines: tuple[str, ...]
    #: What the barcode said, if anything. Carried for the log and for nothing else: this
    #: worker has no call that could write it back.
    mpn: str | None
    attempts: int

    @property
    def anchored(self) -> bool:
        return bool(self.barcode_texts)


class ApiClient(Protocol):
    """The five calls the worker makes.

    A Protocol so the tests drive the loop in-process through the real routes, without a
    socket or a port — `research_datasheets.ApiClient`'s reasoning unchanged.
    """

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedCapture]: ...

    def fetch_image(self, sha256: str) -> bytes: ...

    def create_stub_part(
        self, *, name: str, mpn: str, client_op_id: str, device_id: str
    ) -> int | None: ...

    def submit_candidates(
        self,
        *,
        intake_id: int,
        candidates: Sequence[dict[str, Any]],
        label_kind: str | None,
    ) -> None: ...

    def submit_failure(self, *, intake_id: int, error: str) -> None: ...

    def record_run(self, run: dict[str, Any]) -> None: ...


class HttpApiClient:
    """`ApiClient` over `urllib.request`. No retries — the queue's lease is the retry."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self.timeout = timeout

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedCapture]:
        body = self._post_json("api/dispatch/claims", {"worker_id": worker_id, "limit": limit})
        return [
            QueuedCapture(
                intake_id=int(claim["intake_id"]),
                capture_id=int(claim["capture_id"]),
                capture_sha256=str(claim["capture_sha256"]),
                media_type=str(claim["media_type"]),
                barcode_texts=tuple(claim.get("barcode_texts") or ()),
                ocr_lines=tuple(claim.get("ocr_lines") or ()),
                mpn=claim.get("mpn"),
                attempts=int(claim["attempts"]),
            )
            for claim in body.get("claims", [])
        ]

    def fetch_image(self, sha256: str) -> bytes:
        """The photograph's bytes, from the ordinary document route.

        **This is the only place in the pipeline that holds an image.** The API serves it
        and never decodes it; this process decodes nothing either — it hands the bytes to
        a provider, which base64s them. Fetched over HTTP rather than read off the volume
        because the worker is a different pod and has no business mounting the blob store.
        """
        request = urllib.request.Request(
            urljoin(self.base_url, f"api/documents/{sha256}"), method="GET"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data: bytes = response.read()
        return data

    def create_stub_part(
        self, *, name: str, mpn: str, client_op_id: str, device_id: str
    ) -> int | None:
        """Mint the stub for one candidate, idempotently. Returns its id, or None.

        `client_op_id` is derived from `(intake_id, mpn)` by the caller, so a re-read of
        the same photograph updates the candidate row and **reuses** the part rather than
        forking the catalogue. The route replays its stored response for a repeated key,
        which is the same guarantee the PWA relies on when curating a parked label twice.

        `None` on refusal rather than a raise: a candidate whose part could not be minted
        is still worth storing — the reading and its quote are the useful part, and a
        reviewer can act on them without a stub. Raising would discard the whole run's
        readings over one of them.
        """
        try:
            body = self._post_json(
                "api/parts",
                {
                    "name": name,
                    "part_kind": DEFAULT_PART_KIND,
                    "mpn": mpn,
                    # The flag that keeps this out of every curated view and inside the
                    # review queue's remit. A model-proposed identity is precisely what
                    # `is_stub` was added for.
                    "is_stub": True,
                    "client_op_id": client_op_id,
                    "device_id": device_id,
                },
            )
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
            log.warning("could not mint a stub for %s: %s", mpn, error)
            return None
        part = body.get("part")
        return None if not isinstance(part, dict) else int(part["id"])

    def submit_candidates(
        self,
        *,
        intake_id: int,
        candidates: Sequence[dict[str, Any]],
        label_kind: str | None,
    ) -> None:
        self._post_json(
            "api/dispatch/results",
            {
                "intake_id": intake_id,
                "candidates": list(candidates),
                "label_kind": label_kind,
            },
        )

    def submit_failure(self, *, intake_id: int, error: str) -> None:
        self._post_json("api/dispatch/results", {"intake_id": intake_id, "error": error[:2000]})

    def record_run(self, run: dict[str, Any]) -> None:
        """Post one transcript. **Never lets a recording failure fail the read.**

        The run is the diagnostic and the candidates are the work. If this call breaks —
        the route is older than the worker, the body was refused — the right outcome is
        an unrecorded transcript and a submitted reading, not a photograph pushed back
        into the queue to spend a second GPU handover on a bookkeeping error. So it warns
        and returns, the same posture `create_stub_part` takes for the same reason.
        """
        try:
            self._post_json("api/runs", run)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
            log.warning("could not record the model run: %s", error)

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


def build_request(capture: QueuedCapture, image: bytes) -> VisionRequest:
    """The read, with both of the browser's passes in it.

    `max_candidates` is 1 when a barcode decoded and `DEFAULT_MAX_CANDIDATES` otherwise,
    which is ADR 0021's weighting expressed as an argument rather than as a sentence in a
    prompt: an anchored read is held to one candidate **by the decoder**, since
    `vision.schema_for` builds `maxItems` from this number.
    """
    return VisionRequest(
        image=image,
        media_type=capture.media_type,
        document_sha256=capture.capture_sha256,
        barcode_texts=capture.barcode_texts,
        ocr_lines=capture.ocr_lines,
        max_candidates=1 if capture.anchored else DEFAULT_MAX_CANDIDATES,
    )


def _stub_key(capture: QueuedCapture, mpn: str) -> str:
    """The idempotency key for one candidate's stub part.

    Derived from `(intake_id, mpn)` and truncated to the route's 36-character ceiling.
    A hash rather than the strings themselves because an MPN can be 128 characters and
    the key cannot; `intake_id` is kept in the clear so a human reading `client_op_id`
    on a stub can tell which photograph produced it.
    """
    import hashlib

    digest = hashlib.sha256(mpn.encode("utf-8")).hexdigest()
    prefix = f"vd-{capture.intake_id}-"
    return f"{prefix}{digest}"[:36]


def _reports(
    client: ApiClient, capture: QueuedCapture, result: VisionResult, *, device_id: str
) -> list[dict[str, Any]]:
    """Turn one read into submittable candidates, minting a stub for each.

    Every candidate gets a stub, including the ones the model ranked below the first —
    see the module docstring for why the losers are the point.

    Note what is *not* built here: no `quantity`, `date_code` or `lot_code` (they come off
    the barcode deterministically and `vision.schema_for` has no property for them
    either), and nothing that could reach the entry's own `mpn` or `resolved_part_id`.
    """
    reports: list[dict[str, Any]] = []
    for rank, candidate in enumerate(result.candidates):
        part_id = client.create_stub_part(
            name=candidate.mpn,
            mpn=candidate.mpn,
            client_op_id=_stub_key(capture, candidate.mpn),
            device_id=device_id,
        )
        reports.append(
            {
                "mpn": candidate.mpn,
                "manufacturer": candidate.manufacturer,
                "package": candidate.package,
                "confidence": candidate.confidence,
                "source_text": candidate.source_text,
                "note": candidate.note,
                "part_id": part_id,
                "rank": rank,
                "provider": result.provider,
                "model": result.model,
            }
        )
    return reports


def _run_row(
    provider: VisionProvider,
    capture: QueuedCapture,
    *,
    started_at: str,
    result: VisionResult | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """One `model_runs` submission, for a call that worked or one that broke.

    Both cases in one builder because the fields are the same fields, and a second
    builder for the failure path is how the failure path ends up carrying less. It
    already carries less than it should — see below — and that should not compound.

    **The prompt and the raw answer are pulled off the exception on the failure path.**
    `ModelUnavailable` attaches both (`request_json`, `response_text`), and `getattr` is
    what reads them rather than an `isinstance` check: a provider's exception types are
    not enumerable — `run_once` says so where it catches them — and this worker must not
    grow an import of the transport to record what the transport raised. A provider that
    attaches neither still gets a row, with `error` set and a NULL transcript, which is
    the honest shape: something broke and nobody wrote down what was said.
    """
    stats = None if result is None else result.stats
    return {
        "kind": "vision",
        "provider": provider.name,
        "model": provider.model,
        "intake_id": capture.intake_id,
        "document_sha256": capture.capture_sha256,
        "started_at": started_at,
        "finished_at": _now(),
        # Straight through, `None` and all. **Not `or 0`** — a missing count must stay
        # distinguishable from a count of zero, which is `CallStats`' rule and the reason
        # every one of these columns is nullable.
        "latency_ms": None if stats is None else stats.latency_ms,
        "prompt_tokens": None if stats is None else stats.prompt_tokens,
        "completion_tokens": None if stats is None else stats.completion_tokens,
        "finish_reason": None if stats is None else stats.finish_reason,
        "request_json": (
            getattr(error, "request_json", None) if result is None else result.request_json
        ),
        "response_text": (
            getattr(error, "response_text", None) if result is None else result.raw_response
        ),
        "error": None if error is None else f"{type(error).__name__}: {error}"[:4000],
    }


def _now() -> str:
    """Wall clock, as the API's `UtcDateTime` wants it.

    The worker's own clock rather than the provider's `latency_ms`, and both are stored:
    a large gap between them is itself a finding, because it says the time went somewhere
    other than inference — a model swap, a stalled fetch.
    """
    return datetime.now(tz=UTC).isoformat()


def process_one(client: ApiClient, provider: VisionProvider, capture: QueuedCapture) -> bool:
    """Read one claimed photograph. True if any identity was proposed.

    A read that names nothing submits `candidates: []`, which settles the entry
    `unidentified` — **not** an error. ADR 0021: "we could not tell what this is" is a
    photograph problem whose fix is another photograph, and calling it a failure would put
    a blurred label in a health check that exists to surface real breakage.

    **Every call is recorded, including the ones that break.** The transcript is what
    makes the never-auto-accept rule reviewable: a person looking at a wrong reading needs
    to see what the model was *told*, not only what it said. A failed call is the case
    that matters most, because it leaves no candidate row behind at all — so the run is
    recorded and the exception re-raised for `run_once` to report to the queue.
    """
    image = client.fetch_image(capture.capture_sha256)
    request = build_request(capture, image)
    log.info(
        "intake %d: reading %s (%s, %d barcode(s), %d ocr line(s), max %d candidate(s))",
        capture.intake_id,
        capture.capture_sha256[:12],
        "anchored" if capture.anchored else "unanchored",
        len(capture.barcode_texts),
        len(capture.ocr_lines),
        request.max_candidates,
    )

    started_at = _now()
    try:
        result = provider.read(request)
    except Exception as error:  # a provider's exception types are not enumerable
        client.record_run(_run_row(provider, capture, started_at=started_at, error=error))
        raise
    client.record_run(_run_row(provider, capture, started_at=started_at, result=result))
    reports = _reports(client, capture, result, device_id=f"dispatch:{provider.name}")
    client.submit_candidates(
        intake_id=capture.intake_id,
        candidates=reports,
        label_kind=result.label_kind,
    )
    if result.stats is not None:
        # The measurement ADR 0021 asks for: `grab.ts` does not downscale, so this is
        # what says whether sending a phone frame whole is affordable.
        log.info(
            "intake %d: %s prompt token(s), %dms%s",
            capture.intake_id,
            # Not `or 0`: a missing count must stay distinguishable from a count of zero,
            # which is `CallStats`' own rule and the reason those fields are nullable.
            "unreported" if result.stats.prompt_tokens is None else result.stats.prompt_tokens,
            result.stats.latency_ms,
            " (TRUNCATED)" if result.stats.truncated else "",
        )
    log.info(
        "intake %d: %s (%d candidate(s))",
        capture.intake_id,
        "proposed" if reports else "unidentified",
        len(reports),
    )
    return bool(reports)


def run_once(
    client: ApiClient,
    provider: VisionProvider,
    *,
    worker_id: str,
    limit: int = DEFAULT_LIMIT,
) -> int:
    """Claim one batch and work it. Returns how many photographs produced a proposal."""
    claimed = client.claim(worker_id=worker_id, limit=limit)
    if not claimed:
        return 0
    proposed = 0
    for capture in claimed:
        try:
            proposed += process_one(client, provider, capture)
        except Exception as error:  # a provider's exception types are not enumerable
            # The run broke rather than the photograph being unreadable. Reported as a
            # failure so the queue retries it, which is the difference between a model
            # server that was still loading and a label nobody can read.
            log.warning("dispatch failed for intake %d: %s", capture.intake_id, error)
            client.submit_failure(
                intake_id=capture.intake_id, error=f"{type(error).__name__}: {error}"
            )
    return proposed


def run(
    client: ApiClient,
    provider: VisionProvider,
    *,
    worker_id: str,
    limit: int = DEFAULT_LIMIT,
    poll_seconds: float = 0.0,
    max_batches: int | None = 1,
) -> int:
    """Work the queue until it is empty, or forever if asked.

    A **drain** is the intended shape here, more than for the other two workers: the card
    is already holding the vision model, so the marginal cost of the next photograph is
    inference alone. `--poll-seconds 0` with no `--max-batches` therefore reads every
    queued photograph and exits, which is exactly one model load for the whole queue.
    """
    proposed = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        batches += 1
        done = run_once(client, provider, worker_id=worker_id, limit=limit)
        proposed += done
        if done:
            continue
        if poll_seconds <= 0:
            break
        time.sleep(poll_seconds)
    return proposed


def build_provider(args: argparse.Namespace) -> VisionProvider:
    """The reader, from the command line.

    `--fixture` is the offline path and it is not a lesser one: `FakeVisionProvider`
    replays a recorded response through the **real** `parse_response`, so a drain against
    a fixture exercises every refusal the live path does. It is how this worker is tested
    and how a queue can be walked with no GPU at all.

    The live provider is imported lazily, inside the branch that needs it. That is
    deliberate: `vision_openai_compat` is the transport, and keeping it off the
    import path of every `--fixture` run keeps the offline test honest about what it
    loads.
    """
    if args.fixture is not None:
        return FakeVisionProvider(Path(args.fixture))
    from app.services.enrichment.vision_openai_compat import (
        OpenAICompatVisionProvider,
        for_base_url,
    )

    if args.wire is None:
        # `for_base_url` picks the transport from the server rather than from a flag,
        # which is the right default: which wire shape a server wants is a property of
        # the server, and making the operator remember it is how one of them gets it
        # wrong. `--wire` exists to override that, not to be filled in every time.
        return for_base_url(args.model_url, args.model, timeout=args.timeout)
    return OpenAICompatVisionProvider(
        base_url=args.model_url,
        model=args.model,
        image_transport=args.wire,
        timeout=args.timeout,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.dispatch_captures",
        description=(
            "Claim parked scans whose photograph nobody has read, read each with a "
            "vision model, and propose ranked identity candidates "
            "(docs/adr/0021-a-second-reader-for-the-frame-the-browser-already-read.md)."
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
        "--fixture",
        default=None,
        help=(
            "Replay recorded responses instead of calling a model. Runs the real parser, "
            "so every refusal is still exercised — and needs no GPU."
        ),
    )
    parser.add_argument("--model-url", default="http://localhost:11434", help="Model server root.")
    parser.add_argument("--model", default="qwen3-vl:8b", help="Which vision model to ask.")
    parser.add_argument(
        "--wire",
        default=None,
        choices=("ollama_native", "openai_content_parts"),
        help=(
            "Override the multimodal wire shape. Unset, it is chosen from the server "
            "(`for_base_url`), which lands on `ollama_native` for Ollama — the measured "
            "default: on the ambiguous corpus case the native path completed inside an "
            "8192-token budget and the OpenAI path did not (ADR 0021)."
        ),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Photographs per claim.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim one batch and exit. The CronJob posture.",
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
        help=(
            "Stop after this many batches. Unset and without --once, the worker drains "
            "the queue and exits — one model load for the whole queue."
        ),
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
        provider = build_provider(args)
    except Exception as error:
        # A missing model or an unreadable fixture is a deployment error, and exiting
        # before the first claim is what keeps it from spending the queue's attempts —
        # which for this queue means spending GPU handovers.
        log.error("no vision provider: %s", error)
        return 2

    proposed = run(
        HttpApiClient(args.base_url, timeout=args.timeout),
        provider,
        worker_id=worker_id,
        limit=args.limit,
        poll_seconds=args.poll_seconds,
        max_batches=max_batches,
    )
    log.info("done: %d photograph(s) produced a proposal", proposed)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
