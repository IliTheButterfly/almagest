"""`/api/extraction` — the work queue and the submit door, and nothing else.

ADR 0005's whole interface, in four routes plus a read. The API hands out documents
that need text and takes text back; **it never parses a PDF**, and there is no
route here that could tempt it to. The worker is `app.scripts.extract_datasheets`,
which talks to exactly these paths over HTTP — the ADR refuses it direct database
access, because two SQLite writers is corruption and that is the one rule the whole
single-replica deployment shape exists to protect.

## `GET /api/documents/{sha256}/text` answers 200 for a document with no text

That is the ADR's load-bearing consequence expressed as a wire type. A document
whose text has never been read is stored, served, attached and **fine**: only search
over its contents waits. So the read route reports `state` and a null `text` rather
than 404-ing, because a 404 is what a client renders as an error, and there is no
error here — the extraction stack is allowed to be absent, broken, or out of GPU
forever.

## No `client_op_id` on any of these writes

The submit door is keyed on `documents.sha256`, which is already the document's
identity, and every field it writes is a function of the submitted text — so a
retry lands the same row by construction, with nothing stored to recognise it by.
More decisively, a *replay* would be wrong: re-reading a document with a better
extractor is a legitimate second submission that must overwrite. See
`app.services.document_text`'s module docstring for the full argument.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from app.api.limits import ClaimLimit

# `document_url` is imported from the store's own router module rather than
# re-derived here, so a claim's URL and a `DocumentRead.url` cannot come to differ.
# Safe in this direction only: `routes.documents` knows nothing about extraction.
from app.api.routes.documents import document_url
from app.db.session import get_db
from app.models.documents import SHA256_LENGTH, Document
from app.models.enums import ExtractionState
from app.services import document_text, documents
from app.services.blobstore import BlobError
from app.services.document_text import ExtractionError

router = APIRouter(prefix="/api/extraction", tags=["extraction"])

#: The per-document text read hangs off the document's own path rather than off
#: `/api/extraction`, because it is a property of the document and is read by the
#: part screen, not by the worker. Same split `documents.parts_router` makes.
documents_router = APIRouter(prefix="/api/documents", tags=["extraction"])

#: A worker's self-declared name, recorded in `documents.extraction_claimed_by` for
#: diagnostics only. Bounded and pattern-free: nothing branches on it, it is never
#: interpolated into SQL or a path, and a hostname or a pod name is what will
#: arrive.
WorkerId = Annotated[str, StringConstraints(min_length=1, max_length=64)]

#: One page's extracted text. Bounded per page as well as in total (see
#: `document_text.MAX_EXTRACTED_CHARS`) because a single runaway page is the cheaper
#: thing to refuse — 200k characters is ~50x a dense datasheet page.
PageText = Annotated[str, StringConstraints(max_length=200_000)]

#: Pages in one submission. A 400-page databook is the fat tail of what is real; the
#: bound stops an extractor looping on a malformed page tree from sending an
#: unbounded array before any of it is counted.
MAX_SUBMITTED_PAGES = 5_000

_REASON_STATUS = {
    "invalid_sha256": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "text_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class DocumentTextRead(BaseModel):
    """One document's text and the judgement about it. Every field but `state` and
    `attempts` may be null, and null is the ordinary case for a fresh upload."""

    sha256: str
    #: `pending` for a stored PDF nobody has read yet — normal, not an error.
    #: `not_applicable` for an image. See `app.models.enums.ExtractionState`.
    state: ExtractionState
    #: Null when nothing has been extracted. **Empty string** when a page-image PDF
    #: was extracted and genuinely yielded nothing, which is a different fact: that
    #: one has been looked at, and `low_confidence` is set on it.
    text: str | None
    page_count: int | None
    char_count: int | None
    #: The mean, and the escalation signal: `≈ 0` means the PDF is page images and
    #: needs OCR. Computed in one place (`document_text.chars_per_page`) rather than
    #: by each client dividing.
    chars_per_page: float | None
    #: **The stored judgement, derived by the API from the text it stores** — never
    #: supplied by the worker. Null means no judgement has been made yet: a
    #: never-extracted document is neither trusted nor distrusted, and `false` here
    #: would make an unread scanned datasheet look vouched for.
    low_confidence: bool | None
    #: Which `Extractor` produced the current text, so "re-read everything the cheap
    #: one did" is a query.
    extractor: str | None
    extracted_at: datetime | None
    #: Claims handed out so far, counted at claim time so a worker that dies without
    #: reporting still burns one.
    attempts: int
    #: The last failure, verbatim from the worker. For a human reading a health
    #: check; nothing branches on it.
    error: str | None


class ExtractionQueueStatus(BaseModel):
    """Queue depth by state, with every state present even at zero — a key that
    disappears when it is zero cannot distinguish "nothing failed" from "the failure
    count stopped being reported"."""

    counts: dict[ExtractionState, int]
    #: Convenience for the health check `docs/PLAN.md` asks for ("failed datasheet
    #: extractions" among the deterministic, always-actionable items).
    pending: int
    failed: int
    #: The parameters a worker is being held to, reported rather than assumed so an
    #: operator can see why a document came back round.
    lease_seconds: int
    max_attempts: int


class ExtractionClaim(BaseModel):
    """One document leased to a worker.

    Carries only what is needed to fetch and identify it. **No storage path and no
    text**: the worker reads the bytes from `url`, over HTTP like any other client,
    because ADR 0005 gives it neither the database nor the volume.
    """

    sha256: str
    media_type: str
    byte_size: int
    #: Relative, resolved against whatever origin the worker was pointed at — the
    #: same reasoning as `DocumentRead.url`.
    url: str
    #: Including this one. A value above 1 means an earlier attempt died or failed,
    #: which is worth logging when this document turns out to be the poison one.
    attempts: int
    #: When this lease stops being honoured. After it, the document is offered to
    #: whoever asks next — a submission from the old holder is still accepted, since
    #: text extracted slowly is still correct text.
    lease_expires_at: datetime


class ExtractionClaimBatch(BaseModel):
    worker_id: str
    claims: list[ExtractionClaim]


class ExtractionClaimRequest(BaseModel):
    worker_id: WorkerId
    limit: ClaimLimit = 1


class ExtractionResultRequest(BaseModel):
    """One run's outcome: pages, or an error. Never both, always one.

    **Success and failure come through the same door on purpose.** Both are outcomes
    of a claim and both have to settle the same lease and the same attempt counter;
    two routes would be two places to get that bookkeeping right, and the failure
    path is the one that gets exercised least and would therefore be the one that
    was wrong.

    There is deliberately **no character-count or confidence field.** The API counts
    the pages it is given and makes the judgement itself, so no bug and no client
    can report a scanned datasheet as extracted-confident-and-empty. See
    `app.services.document_text`.
    """

    sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    #: The `Extractor.name` that produced this, recorded on the document.
    extractor: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    #: One string per page, in page order. An empty list is a legitimate result — a
    #: PDF whose page tree yielded nothing — and comes out flagged, not trusted.
    pages: list[PageText] | None = Field(default=None, max_length=MAX_SUBMITTED_PAGES)
    #: Set instead of `pages` when the run failed. The document goes back to the
    #: queue if it has attempts left, and to `failed` if it does not.
    error: Annotated[str, StringConstraints(min_length=1, max_length=2_000)] | None = None


class ExtractionResultResponse(BaseModel):
    #: The document as it now stands, so a worker never has to re-read to find out
    #: whether its text was accepted or whether the judgement flagged it.
    document: DocumentTextRead


class ExtractionRequeueRequest(BaseModel):
    sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)


class ExtractionRequeueResponse(BaseModel):
    document: DocumentTextRead
    #: True when this document already had text. Then the requeue is the
    #: `PyPdfExtractor` → `DoclingExtractor` upgrade path, and the old text stays
    #: searchable until the better run replaces it.
    had_text: bool


# ---------------------------------------------------------------------------
# Mapping and lookup
# ---------------------------------------------------------------------------


def _text_read(session: Session, document: Document) -> DocumentTextRead:
    described = document_text.describe(session, document)
    return DocumentTextRead(
        sha256=document.sha256,
        state=described.state,
        text=described.body,
        page_count=described.page_count,
        char_count=described.char_count,
        chars_per_page=described.chars_per_page,
        low_confidence=described.low_confidence,
        extractor=described.extractor,
        extracted_at=described.extracted_at,
        attempts=described.attempts,
        error=described.error,
    )


def _require_document(db: Session, sha256: str) -> Document:
    """Resolve a document by address, refusing a malformed digest as 422.

    Imported behaviour rather than a duplicate of
    `app.api.routes.documents._require_document` would have been nicer; it is
    repeated because importing a private helper across route modules couples two
    routers' error vocabularies, and this module's `_REASON_STATUS` is a different
    (smaller) map.
    """
    try:
        document = documents.by_sha256(db, sha256)
    except BlobError as error:
        raise HTTPException(
            _REASON_STATUS.get(error.reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
            detail={"reason": error.reason, "message": str(error)},
        ) from error
    if document is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_document", "message": f"no document with sha256 {sha256}"},
        )
    return document


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@documents_router.get("/{sha256}/text", response_model=DocumentTextRead)
def read_document_text(sha256: str, db: Session = Depends(get_db)) -> DocumentTextRead:
    """The extracted text of one document, or the honest absence of it.

    **200 with `text: null` for a document nobody has extracted yet**, which is the
    normal state of every PDF the moment it is uploaded. 404 only when the
    *document* is unknown — the thing that really is missing.
    """
    return _text_read(db, _require_document(db, sha256))


@router.get("/status", response_model=ExtractionQueueStatus)
def read_extraction_status(db: Session = Depends(get_db)) -> ExtractionQueueStatus:
    """Queue depth. One grouped count over an indexed column, cheap enough to poll."""
    counts = document_text.status_counts(db)
    return ExtractionQueueStatus(
        counts=counts,
        pending=counts[ExtractionState.PENDING],
        failed=counts[ExtractionState.FAILED],
        lease_seconds=document_text.LEASE_SECONDS,
        max_attempts=document_text.MAX_EXTRACTION_ATTEMPTS,
    )


@router.post("/claims", response_model=ExtractionClaimBatch)
def claim_extraction_work(
    request: ExtractionClaimRequest, db: Session = Depends(get_db)
) -> ExtractionClaimBatch:
    """Lease up to `limit` documents that need text.

    A POST rather than a GET despite reading like a query, because it **writes**: it
    takes a lease and burns an attempt. A GET that mutated the queue would be
    retried by every proxy and prefetched by every crawler.

    An empty `claims` list is the ordinary answer and is not an error — it means the
    queue is drained, which is where a healthy install spends most of its life. The
    worker sleeps on it.
    """
    claimed = document_text.claim(db, worker_id=request.worker_id, limit=request.limit)
    batch = ExtractionClaimBatch(
        worker_id=request.worker_id,
        claims=[
            ExtractionClaim(
                sha256=document.sha256,
                media_type=document.media_type,
                byte_size=document.byte_size,
                url=document_url(document.sha256),
                attempts=document.extraction_attempts,
                lease_expires_at=_lease_expiry(document),
            )
            for document in claimed
        ],
    )
    # Committed before the response is written, deliberately: a lease that is not
    # durable is not a lease, and a claim lost on the way back would leave the
    # worker extracting a document the queue still considers unclaimed.
    db.commit()
    return batch


@router.post("/results", response_model=ExtractionResultResponse)
def submit_extraction_result(
    request: ExtractionResultRequest, db: Session = Depends(get_db)
) -> ExtractionResultResponse:
    """Record one run's outcome — text, or a failure.

    Idempotent by content address: submitting the same pages twice lands the same
    row and the same index entry, and submitting *different* pages replaces them,
    which is exactly what re-reading with a better extractor has to do.
    """
    pages, failure = request.pages, request.error
    if (pages is None) == (failure is None):
        # Both or neither. Refused rather than resolved by precedence: a worker that
        # sent both has a bug, and picking one of the two would record an outcome it
        # did not unambiguously report.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "ambiguous_result",
                "message": "send exactly one of `pages` (a result) or `error` (a failure)",
            },
        )

    document = _require_document(db, request.sha256)
    try:
        if pages is not None:
            document_text.record_text(
                db, document=document, extractor=request.extractor, pages=pages
            )
        elif failure is not None:
            document_text.record_failure(db, document=document, error=failure)
    except ExtractionError as error:
        raise HTTPException(
            _REASON_STATUS.get(error.reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
            detail={"reason": error.reason, "message": str(error)},
        ) from error

    response = ExtractionResultResponse(document=_text_read(db, document))
    db.commit()
    return response


@router.post("/requeue", response_model=ExtractionRequeueResponse)
def requeue_extraction(
    request: ExtractionRequeueRequest, db: Session = Depends(get_db)
) -> ExtractionRequeueResponse:
    """Offer a document to the queue again, from zero attempts.

    Two uses, one operation: retry a `failed` document once its cause is fixed, and
    **re-read an extracted one with a better extractor**. The second is ADR 0005's
    whole upgrade path, and it needs no new machinery because it is this.
    """
    document = _require_document(db, request.sha256)
    had_text = document.extraction_state == ExtractionState.EXTRACTED
    document_text.requeue(db, document=document)
    response = ExtractionRequeueResponse(
        document=_text_read(db, document),
        had_text=had_text,
    )
    db.commit()
    return response


def _lease_expiry(document: Document) -> datetime:
    """When a just-granted lease runs out.

    Derived from the stored `extraction_claimed_at` rather than from a fresh clock
    read, so the worker's deadline is the same instant the queue will use to decide
    the lease is gone. Reading the clock twice would put them microseconds apart in
    the direction that has the worker believing it has longer than it does.
    """
    claimed_at = document.extraction_claimed_at
    if claimed_at is None:  # pragma: no cover - claim() always writes it
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"reason": "unclaimed", "message": "claimed document has no claim timestamp"},
        )
    return claimed_at + timedelta(seconds=document_text.LEASE_SECONDS)
