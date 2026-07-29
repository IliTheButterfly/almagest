"""Datasheet text extraction: the queue, the lease, the judgement, the worker.

## What this file is really guarding

Two failures, both silent, both expensive.

**A scanned datasheet stored as extracted, confident and empty.** Nobody ever
discovers it: the symptom is a search that fails to match, not an error anybody sees.
`docs/PLAN.md` makes `extracted-chars-per-page ≈ 0` the OCR escalation signal *and*
the low-confidence flag, so the tests below check that a no-text-layer PDF comes out
**flagged** — and that the flag is derived by the API from the text it stores rather
than asserted by whatever submitted it, because a wire field a worker could get wrong
is a wire field that will be wrong eventually.

**A queue that stops moving while every count reads clean.** A worker dies mid-claim
without reporting anything — a node drain, a segfault in a parser, a power cut. The
lease, the attempt counted *at claim time*, and the abandoned-claim sweep are three
halves of one mechanism, and the test for it walks a document all the way to `failed`
while checking a second document keeps being served.

## PDF fixtures are hand-assembled — see `tests/pdfs.py`

Nothing here uses a PDF library to *write* a PDF, so `PyPdfExtractor` is tested
against the format rather than against pypdf's own writer. Tests that need pypdf to
*read* one `importorskip` it: the `datasheets` extra is installed in CI
(`uv sync --all-extras`) but is deliberately absent from the API's own install, per
`docs/adr/0005-extraction-runs-outside-the-api.md`, and a suite that could not run
without it would be quietly asserting the opposite.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Sequence
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.documents import Document
from app.models.enums import DocumentKind, ExtractionState
from app.scripts.extract_datasheets import QueuedDocument, process_one, run, run_once
from app.services import document_text, documents, extractors
from app.services.extractors import DoclingExtractor, ExtractorUnavailable, PyPdfExtractor
from tests import pdfs
from tests.conftest import BACKEND_DIR
from tests.factories import make_part

PDF = "application/pdf"
PNG = "image/png"

#: A minimal PNG — enough magic for `blobstore` and nothing more. Images are here
#: only to prove they never enter the queue.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"tray photo"

#: One page's worth of plausible datasheet prose. Long enough to clear
#: `LOW_CONFIDENCE_CHARS_PER_PAGE` on its own, because the threshold is a real one
#: and a fixture that sat under it would make "extracted and trusted" untestable.
PAGE_BODY = (
    "Absolute Maximum Ratings: VCE 60 V, IC 600 mA, Ptot 500 mW at 25 C. "
    "Thermal resistance junction to ambient 200 K/W. Storage temperature "
    "-55 to +150 C. Recommended operating conditions and electrical "
    "characteristics measured at Tamb 25 C unless otherwise noted."
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload(
    client: TestClient, data: bytes, *, media_type: str = PDF, **params: object
) -> Response:
    """Raw body, metadata in the query string — the store takes no multipart."""
    return client.post(
        "/api/documents",
        content=data,
        params={"media_type": media_type, **params},
        headers={"Content-Type": "application/octet-stream"},
    )


def _store(db: Session, data: bytes, *, media_type: str = PDF) -> Document:
    stored = documents.store_document(
        db, data=data, media_type=media_type, kind=DocumentKind.DATASHEET
    )
    db.flush()
    return stored.document


def _submit(
    client: TestClient, sha256: str, *, extractor: str = "pypdf", **body: object
) -> Response:
    return client.post(
        "/api/extraction/results",
        json={"sha256": sha256, "extractor": extractor, **body},
    )


def _indexed_rowids(db: Session) -> list[int]:
    return list(db.execute(text("SELECT rowid FROM datasheet_fts ORDER BY rowid")).scalars())


def _fts_matches(db: Session, query: str) -> set[int]:
    return set(
        db.execute(
            text("SELECT rowid FROM datasheet_fts WHERE datasheet_fts MATCH :q"), {"q": query}
        ).scalars()
    )


# ---------------------------------------------------------------------------
# The queue: what is in it
# ---------------------------------------------------------------------------


def test_a_stored_pdf_joins_the_queue_and_an_image_does_not(db: Session) -> None:
    """`initial_state` is read off the declared media type, never off the file.

    A tray photograph left `PENDING` would sit in the queue forever, and a queue whose
    depth is not the depth of the work is a queue nobody reads. This is also the whole
    of what the API "knows" about a document's contents: one string comparison.
    """
    pdf = _store(db, pdfs.with_text([PAGE_BODY]))
    image = _store(db, PNG_BYTES, media_type=PNG)

    assert pdf.extraction_state == ExtractionState.PENDING
    assert image.extraction_state == ExtractionState.NOT_APPLICABLE

    claimed = document_text.claim(db, worker_id="w1", limit=10)
    assert [document.id for document in claimed] == [pdf.id]


def test_status_counts_report_every_state_including_the_empty_ones(db: Session) -> None:
    """A key that vanishes when it is zero cannot distinguish "nothing failed" from
    "the failure count stopped being reported"."""
    _store(db, pdfs.with_text([PAGE_BODY]))
    counts = document_text.status_counts(db)

    assert set(counts) == set(ExtractionState)
    assert counts[ExtractionState.PENDING] == 1
    assert counts[ExtractionState.FAILED] == 0


# ---------------------------------------------------------------------------
# Claiming, and dying while holding a claim
# ---------------------------------------------------------------------------


def test_claim_takes_a_lease_and_counts_the_attempt_up_front(db: Session) -> None:
    """The attempt is burned when the work is handed out, not when a failure is
    reported — a worker that is killed reports nothing at all."""
    document = _store(db, pdfs.with_text([PAGE_BODY]))

    (claimed,) = document_text.claim(db, worker_id="worker-a", limit=5)

    assert claimed.extraction_state == ExtractionState.CLAIMED
    assert claimed.extraction_attempts == 1
    assert claimed.extraction_claimed_by == "worker-a"
    assert claimed.extraction_claimed_at is not None
    assert claimed.id == document.id


def test_a_live_lease_is_not_handed_to_a_second_worker(db: Session) -> None:
    _store(db, pdfs.with_text([PAGE_BODY]))

    first = document_text.claim(db, worker_id="worker-a", limit=5)
    second = document_text.claim(db, worker_id="worker-b", limit=5)

    assert len(first) == 1
    assert second == []


def test_a_second_claim_gets_only_what_the_first_left(db: Session) -> None:
    """Batches are disjoint, and a `limit` bigger than the queue is not an error."""
    for index in range(3):
        _store(db, pdfs.with_text([f"{PAGE_BODY} page {index}"]))

    first = {document.id for document in document_text.claim(db, worker_id="a", limit=2)}
    second = {document.id for document in document_text.claim(db, worker_id="b", limit=2)}

    assert len(first) == 2
    assert len(second) == 1
    assert first & second == set()


def test_a_claim_whose_candidates_were_taken_in_between_takes_nothing(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compare-and-swap, tested at the only point it is reachable.

    Route handlers are `def`, so two concurrent `POST /api/extraction/claims` are two
    threadpool threads on two connections, and pysqlite holds no read transaction
    across the pick — so a claim really can land between one request's `SELECT` and its
    `UPDATE`. `_candidates` is monkeypatched to a **stale** list to reproduce exactly
    that, because nothing single-threaded can. Without the re-check in the update, the
    slow request re-stamps a live lease and two workers extract the same PDF.
    """
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    (won,) = document_text.claim(db, worker_id="fast", limit=1)
    assert won.extraction_claimed_by == "fast"

    monkeypatch.setattr(document_text, "_candidates", lambda *_args, **_kwargs: [document.id])
    stolen = document_text.claim(db, worker_id="slow", limit=1)

    assert stolen == []
    assert document.extraction_claimed_by == "fast"
    assert document.extraction_attempts == 1, "a refused claim must not burn an attempt either"


def test_an_expired_lease_is_offered_again(db: Session) -> None:
    """`now` is a parameter precisely so this is testable without sleeping for the
    fifteen minutes a real lease lasts."""
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    (claimed,) = document_text.claim(db, worker_id="dead-worker", limit=1)
    later = claimed.extraction_claimed_at
    assert later is not None

    again = document_text.claim(
        db,
        worker_id="live-worker",
        limit=1,
        now=later + timedelta(seconds=document_text.LEASE_SECONDS + 1),
    )

    assert [item.id for item in again] == [document.id]
    assert again[0].extraction_attempts == 2
    assert again[0].extraction_claimed_by == "live-worker"


def test_a_worker_that_dies_mid_claim_does_not_wedge_the_queue(db: Session) -> None:
    """The required end-to-end story: a worker claims and is killed, repeatedly.

    A worker takes a lease and is killed — no submission, no failure report, nothing.
    Each expired lease is re-offered and burns one attempt; after
    `MAX_EXTRACTION_ATTEMPTS` the document is moved to `FAILED` and **not** left
    sitting in `CLAIMED`, which is the state that is neither pending nor failed nor
    claimable, and therefore the state in which a queue silently stops making progress
    while every count reads clean.

    The document is alone in the queue for the first phase on purpose: with a second
    one present the ordering rule would serve the untried document instead, which is
    correct behaviour (`test_fresh_work_is_served_before_retries`) and would hide the
    lease recovery this is about.
    """
    poison = _store(db, pdfs.with_text([f"{PAGE_BODY} poison"]))
    now = document_text.utcnow()

    for attempt in range(1, document_text.MAX_EXTRACTION_ATTEMPTS + 1):
        claimed = document_text.claim(db, worker_id=f"killed-{attempt}", limit=1, now=now)
        # Nothing is submitted: this is what a `SIGKILL` looks like from here.
        assert [item.id for item in claimed] == [poison.id], (
            f"attempt {attempt} should have been re-offered after the lease expired"
        )
        assert claimed[0].extraction_attempts == attempt
        now += timedelta(seconds=document_text.LEASE_SECONDS + 1)

    # The sweep at the top of the next claim retires it rather than re-offering it a
    # fourth time, and reports an empty queue instead of a document nobody holds.
    assert document_text.claim(db, worker_id="live", limit=1, now=now) == []

    db.refresh(poison)
    assert poison.extraction_state == ExtractionState.FAILED
    assert poison.extraction_error is not None
    assert "abandoned" in poison.extraction_error
    counts = document_text.status_counts(db)
    assert counts == {**counts, ExtractionState.FAILED: 1, ExtractionState.CLAIMED: 0}

    # And the queue has moved on: work uploaded afterwards is served immediately,
    # which is what "not wedged" actually means.
    healthy = _store(db, pdfs.with_text([f"{PAGE_BODY} healthy"]))
    after = document_text.claim(db, worker_id="live", limit=1, now=now)
    assert [item.id for item in after] == [healthy.id]


def test_fresh_work_is_served_before_retries(db: Session) -> None:
    """`ORDER BY (attempts, id)`. Without it one document that kills its worker sits
    at the head of the queue and every upload behind it waits on a lease timeout."""
    retried = _store(db, pdfs.with_text([f"{PAGE_BODY} retried"]))
    (claimed,) = document_text.claim(db, worker_id="a", limit=1)
    assert claimed.id == retried.id
    document_text.record_failure(db, document=claimed, error="boom")

    fresh = _store(db, pdfs.with_text([f"{PAGE_BODY} fresh"]))

    (next_up,) = document_text.claim(db, worker_id="b", limit=1)
    assert next_up.id == fresh.id


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------


def test_a_text_layer_document_is_recorded_and_trusted(db: Session) -> None:
    document = _store(db, pdfs.with_text([PAGE_BODY, PAGE_BODY]))

    recorded = document_text.record_text(
        db, document=document, extractor="pypdf", pages=[PAGE_BODY, PAGE_BODY]
    )

    assert recorded.state == ExtractionState.EXTRACTED
    assert recorded.page_count == 2
    assert recorded.char_count == 2 * len(PAGE_BODY)
    assert recorded.low_confidence is False
    assert recorded.chars_per_page == pytest.approx(len(PAGE_BODY))
    assert recorded.body is not None and PAGE_BODY in recorded.body
    assert document.page_count == 2


def test_a_document_with_no_text_layer_is_flagged_rather_than_trusted(db: Session) -> None:
    """The one that matters. A scanned datasheet must not come out looking read.

    It is `EXTRACTED` — something did look at it — and `low_confidence` is True, which
    is simultaneously the OCR escalation signal. The pair is what distinguishes "read
    and found nothing" from "not read yet", and both from "read and trusted".
    """
    document = _store(db, pdfs.no_text_layer(3))

    recorded = document_text.record_text(
        db, document=document, extractor="pypdf", pages=["", "", ""]
    )

    assert recorded.state == ExtractionState.EXTRACTED
    assert recorded.low_confidence is True
    assert recorded.char_count == 0
    assert recorded.chars_per_page == 0.0
    # **The empty string, not None, and not "\n\n".** This document has been looked at,
    # which is a different fact from never having been read — and a client tells the two
    # apart as `"" `versus `null`, so a body of bare separators would read as truthy.
    assert recorded.body == ""


def test_a_sparse_text_layer_is_flagged_too(db: Session) -> None:
    """A stamped page number is not a text layer. The threshold is a document mean, so
    a handful of characters across several pages still reads as ≈ 0."""
    document = _store(db, pdfs.no_text_layer(4))

    recorded = document_text.record_text(
        db, document=document, extractor="pypdf", pages=["1", "2", "3", "4"]
    )

    assert recorded.low_confidence is True


def test_no_pages_at_all_is_flagged_and_does_not_divide_by_zero(db: Session) -> None:
    document = _store(db, pdfs.with_text([PAGE_BODY]))

    recorded = document_text.record_text(db, document=document, extractor="pypdf", pages=[])

    assert recorded.page_count == 0
    assert recorded.chars_per_page == 0.0
    assert recorded.low_confidence is True


def test_the_judgement_is_computed_in_one_place(db: Session) -> None:
    """`is_low_confidence` and the stored column must agree by construction — the
    threshold is not a number three modules each remember."""
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    recorded = document_text.record_text(
        db, document=document, extractor="pypdf", pages=[PAGE_BODY]
    )

    assert recorded.low_confidence == document_text.is_low_confidence(len(PAGE_BODY), 1)
    assert document_text.is_low_confidence(99, 1) is True
    assert document_text.is_low_confidence(100, 1) is False


def test_the_wire_gives_a_worker_no_way_to_assert_the_flag(client: TestClient) -> None:
    """There is no `char_count`, `chars_per_page` or `low_confidence` field on the
    submission, and there must never be one. Given a count field, one bug reporting
    "3000 chars/page" beside empty text stores a scanned datasheet as trusted, and
    nothing downstream can detect it. Pages on the wire make the judgement a function
    of what is actually stored."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]

    assert set(schema["ExtractionResultRequest"]["properties"]) == {
        "sha256",
        "extractor",
        "pages",
        "error",
    }


# ---------------------------------------------------------------------------
# Idempotency, keyed on the content address
# ---------------------------------------------------------------------------


def test_resubmitting_the_same_pages_is_a_no_op(db: Session) -> None:
    """No `client_op_id` anywhere: the sha256 is already the key, and every field
    written is a function of the submitted text, so a retry lands the same row."""
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    first = document_text.record_text(db, document=document, extractor="pypdf", pages=[PAGE_BODY])

    second = document_text.record_text(db, document=document, extractor="pypdf", pages=[PAGE_BODY])

    assert (second.body, second.char_count, second.page_count) == (
        first.body,
        first.char_count,
        first.page_count,
    )
    assert _indexed_rowids(db) == [document.id]


def test_re_extracting_with_a_better_extractor_replaces_the_text(db: Session) -> None:
    """The case that rules out `app.api.idempotency` here rather than merely making it
    unnecessary: a replay guard would hand back the pypdf answer and discard the
    Docling one, breaking the upgrade path the `Extractor` Protocol exists for."""
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    document_text.record_text(db, document=document, extractor="pypdf", pages=["thin ocr guess"])

    better = document_text.record_text(
        db, document=document, extractor="docling", pages=[PAGE_BODY, PAGE_BODY]
    )

    assert better.extractor == "docling"
    assert better.page_count == 2
    assert _indexed_rowids(db) == [document.id]
    assert "thin ocr guess" not in (better.body or "")


def test_submitted_text_is_searchable(db: Session) -> None:
    """Population of `datasheet_fts`, whose rowid the FTS migration reserved for
    `documents.id` before this code existed."""
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    document_text.record_text(db, document=document, extractor="pypdf", pages=[PAGE_BODY])

    assert _fts_matches(db, '"thermal" AND "resistance"') == {document.id}
    assert _fts_matches(db, '"nonexistent"') == set()


def test_text_beyond_the_limit_is_refused(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_text, "MAX_EXTRACTED_CHARS", 10)
    document = _store(db, pdfs.with_text([PAGE_BODY]))

    with pytest.raises(document_text.ExtractionError) as caught:
        document_text.record_text(db, document=document, extractor="pypdf", pages=["x" * 11])

    assert caught.value.reason == "text_too_large"


# ---------------------------------------------------------------------------
# Failures and requeueing
# ---------------------------------------------------------------------------


def test_a_reported_failure_returns_it_to_the_queue_until_attempts_run_out(db: Session) -> None:
    document = _store(db, pdfs.with_text([PAGE_BODY]))

    for attempt in range(1, document_text.MAX_EXTRACTION_ATTEMPTS + 1):
        (claimed,) = document_text.claim(db, worker_id="w", limit=1)
        assert claimed.extraction_attempts == attempt
        reported = document_text.record_failure(db, document=claimed, error=f"boom {attempt}")

    assert reported.state == ExtractionState.FAILED
    assert document.extraction_error == "boom 3"
    assert document_text.claim(db, worker_id="w", limit=1) == []


def test_a_failed_re_run_leaves_the_previous_text_searchable(db: Session) -> None:
    """Trying to improve a result must not make search worse than not trying."""
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    document_text.record_text(db, document=document, extractor="pypdf", pages=[PAGE_BODY])
    document_text.requeue(db, document=document)
    (claimed,) = document_text.claim(db, worker_id="w", limit=1)

    document_text.record_failure(db, document=claimed, error="docling fell over")

    assert _fts_matches(db, '"thermal"') == {document.id}
    assert document_text.text_of(db, document) is not None


def test_requeue_reopens_a_document_and_keeps_its_text_meanwhile(db: Session) -> None:
    document = _store(db, pdfs.with_text([PAGE_BODY]))
    document_text.record_text(db, document=document, extractor="pypdf", pages=[PAGE_BODY])

    reopened = document_text.requeue(db, document=document)

    assert reopened.state == ExtractionState.PENDING
    assert reopened.attempts == 0
    # Not cleared: it stays searchable until a better run replaces it.
    assert reopened.body is not None
    assert [item.id for item in document_text.claim(db, worker_id="w", limit=1)] == [document.id]


def test_requeueing_an_image_returns_it_to_not_applicable(db: Session) -> None:
    """`requeue` re-derives the state from the media type rather than assuming
    `PENDING`, so a hand-submitted OCR result for a photo does not put the photo into
    the automatic queue forever."""
    image = _store(db, PNG_BYTES, media_type=PNG)
    document_text.record_text(db, document=image, extractor="ocr", pages=["STM32F103C8T6"])

    reopened = document_text.requeue(db, document=image)

    assert reopened.state == ExtractionState.NOT_APPLICABLE


def test_reconcile_repairs_state_that_disagrees_with_the_index(db: Session) -> None:
    """The state column and the index are two facts that ought to be one, so — like
    every other cache here — the derived copy is rebuildable in one pass."""
    extracted = _store(db, pdfs.with_text([PAGE_BODY]))
    document_text.record_text(db, document=extracted, extractor="pypdf", pages=[PAGE_BODY])
    orphan = _store(db, pdfs.with_text([f"{PAGE_BODY} orphan"]))
    # Simulate both drifts: text claimed but absent, and text present but unclaimed.
    db.execute(text("DELETE FROM datasheet_fts WHERE rowid = :id"), {"id": extracted.id})
    db.execute(
        text("INSERT INTO datasheet_fts (rowid, text) VALUES (:id, 'stale')"), {"id": orphan.id}
    )
    db.flush()

    report = document_text.reconcile(db)

    assert report == document_text.Reconciliation(missing_text=1, orphaned_text=1)
    assert extracted.extraction_state == ExtractionState.PENDING
    assert _indexed_rowids(db) == []


# ---------------------------------------------------------------------------
# A document that is never extracted is completely usable
# ---------------------------------------------------------------------------


def test_a_never_extracted_document_is_fully_usable(client: TestClient, db: Session) -> None:
    """ADR 0005's load-bearing consequence, asserted end to end.

    The extraction stack is allowed to be absent, broken, or out of GPU forever. A PDF
    with no text is stored, served, attached, redirected to and listed exactly like any
    other; **only search over its contents waits.** Nothing here may 404, 500, hide the
    document, or block.
    """
    part = make_part(db, "TIP120", mpn="TIP120")
    db.commit()
    data = pdfs.with_text([PAGE_BODY])
    digest = _sha(data)

    upload = _upload(client, data, part_id=part.id, filename="tip120.pdf")
    assert upload.status_code == 200

    # The blob itself.
    served = client.get(f"/api/documents/{digest}")
    assert served.status_code == 200
    assert served.content == data

    # Listed against the part, with an honest null page count.
    listed = client.get(f"/api/parts/{part.id}/documents")
    assert listed.status_code == 200
    (link,) = listed.json()["links"]
    assert link["document"]["page_count"] is None
    assert link["is_primary"] is True

    # The QR-to-datasheet path still resolves.
    redirect = client.get(f"/api/parts/{part.id}/datasheet", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == f"/api/documents/{digest}"

    # And the text route says "not yet" with a 200, because there is no error here.
    body = client.get(f"/api/documents/{digest}/text").json()
    assert body["state"] == ExtractionState.PENDING
    assert body["text"] is None
    assert body["low_confidence"] is None, "an unread document is neither trusted nor distrusted"
    assert body["char_count"] is None
    assert body["extractor"] is None
    assert body["attempts"] == 0

    # The part screen and free-text search are unaffected by the missing text.
    assert client.get(f"/api/parts/{part.id}").status_code == 200
    search = client.get("/api/search/parts", params={"text": "TIP120"})
    assert search.status_code == 200
    assert [row["id"] for row in search.json()["results"]] == [part.id]

    # And it is visible as work rather than as a problem.
    status_body = client.get("/api/extraction/status").json()
    assert status_body["pending"] == 1
    assert status_body["failed"] == 0


def test_the_text_of_an_unknown_document_is_a_404(client: TestClient) -> None:
    """404 for the document, not for the text: the thing that is missing is the
    document."""
    response = client.get(f"/api/documents/{'a' * 64}/text")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_document"


def test_a_malformed_digest_on_the_text_route_is_a_422(client: TestClient) -> None:
    response = client.get("/api/documents/not-a-digest/text")

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_sha256"


# ---------------------------------------------------------------------------
# The HTTP doors
# ---------------------------------------------------------------------------


def test_claim_and_submit_over_http(client: TestClient, db: Session) -> None:
    data = pdfs.with_text([PAGE_BODY])
    digest = _sha(data)
    assert _upload(client, data).status_code == 200

    claim = client.post("/api/extraction/claims", json={"worker_id": "worker-1", "limit": 5})
    assert claim.status_code == 200
    (leased,) = claim.json()["claims"]
    assert leased["sha256"] == digest
    assert leased["url"] == f"/api/documents/{digest}"
    assert leased["media_type"] == PDF
    assert leased["attempts"] == 1
    assert leased["lease_expires_at"] is not None

    submitted = _submit(client, digest, pages=[PAGE_BODY])
    assert submitted.status_code == 200
    document = submitted.json()["document"]
    assert document["state"] == ExtractionState.EXTRACTED
    assert document["low_confidence"] is False
    assert document["page_count"] == 1

    # And the queue is drained, which is an empty list rather than an error.
    assert (
        client.post("/api/extraction/claims", json={"worker_id": "worker-1"}).json()["claims"] == []
    )


def test_a_submission_carrying_both_a_result_and_an_error_is_refused(
    client: TestClient, db: Session
) -> None:
    """Refused rather than resolved by precedence: a worker that sent both has a bug,
    and picking one would record an outcome it never unambiguously reported."""
    data = pdfs.with_text([PAGE_BODY])
    _upload(client, data)

    response = _submit(client, _sha(data), pages=[PAGE_BODY], error="also broken")

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "ambiguous_result"


def test_a_submission_carrying_neither_is_refused(client: TestClient) -> None:
    data = pdfs.with_text([PAGE_BODY])
    _upload(client, data)

    response = _submit(client, _sha(data))

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "ambiguous_result"


def test_a_failure_over_http_records_the_error_and_reopens_it(client: TestClient) -> None:
    data = pdfs.no_text_layer()
    digest = _sha(data)
    _upload(client, data)
    client.post("/api/extraction/claims", json={"worker_id": "w"})

    response = _submit(client, digest, error="PdfReadError: EOF marker not found")

    assert response.status_code == 200
    document = response.json()["document"]
    assert document["state"] == ExtractionState.PENDING
    assert document["attempts"] == 1
    assert "PdfReadError" in document["error"]
    assert document["text"] is None


def test_requeue_over_http_reports_whether_there_was_text(client: TestClient) -> None:
    data = pdfs.with_text([PAGE_BODY])
    digest = _sha(data)
    _upload(client, data)

    first = client.post("/api/extraction/requeue", json={"sha256": digest})
    assert first.status_code == 200
    assert first.json()["had_text"] is False

    _submit(client, digest, pages=[PAGE_BODY])
    second = client.post("/api/extraction/requeue", json={"sha256": digest})

    assert second.json()["had_text"] is True
    assert second.json()["document"]["state"] == ExtractionState.PENDING


def test_oversized_text_over_http_is_a_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(document_text, "MAX_EXTRACTED_CHARS", 32)
    data = pdfs.with_text([PAGE_BODY])
    _upload(client, data)

    response = _submit(client, _sha(data), pages=["x" * 100])

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "text_too_large"


def test_the_status_route_reports_the_lease_terms(client: TestClient) -> None:
    """Reported rather than assumed, so an operator looking at a document that came
    back round can see why."""
    body = client.get("/api/extraction/status").json()

    assert body["lease_seconds"] == document_text.LEASE_SECONDS
    assert body["max_attempts"] == document_text.MAX_EXTRACTION_ATTEMPTS
    assert body["counts"][ExtractionState.PENDING] == 0


# ---------------------------------------------------------------------------
# The extractors
# ---------------------------------------------------------------------------


def test_pypdf_extracts_a_text_layer_page_by_page() -> None:
    pytest.importorskip("pypdf", reason="the `datasheets` extra; the API never has it")
    pages = [f"{PAGE_BODY} one", f"{PAGE_BODY} two"]

    extracted = PyPdfExtractor().extract(pdfs.with_text(pages))

    assert extracted.page_count == 2
    assert "one" in extracted.pages[0]
    assert "two" in extracted.pages[1]
    assert extracted.chars_per_page == tuple(len(page) for page in extracted.pages)
    assert extracted.char_count == sum(extracted.chars_per_page)
    assert document_text.is_low_confidence(extracted.char_count, extracted.page_count) is False


def test_pypdf_reports_zero_characters_for_a_page_image_document() -> None:
    """The other half of the same fact: the extractor reports the numbers, and it is
    the *numbers* that make a scanned sheet detectable rather than any judgement made
    inside the parser."""
    pytest.importorskip("pypdf", reason="the `datasheets` extra; the API never has it")

    extracted = PyPdfExtractor().extract(pdfs.no_text_layer(3))

    assert extracted.chars_per_page == (0, 0, 0)
    assert document_text.is_low_confidence(extracted.char_count, extracted.page_count) is True


def test_pypdf_raises_on_bytes_that_are_not_a_pdf() -> None:
    """Which is what the worker turns into a reported failure rather than a crash."""
    pytest.importorskip("pypdf", reason="the `datasheets` extra; the API never has it")

    with pytest.raises(Exception, match=r"(?i)xref|eof|pdf|stream"):
        PyPdfExtractor().extract(b"%PDF-1.7\nnot really\n%%EOF\n")


def test_docling_is_not_installed_and_says_so_actionably() -> None:
    """The seam, and the honest limit of it. `DoclingExtractor` satisfies `Extractor`
    — mypy checks that where `_BUILDERS` is declared — and this repo installs torch
    and transformers nowhere, so the failure has to name the fix."""
    with pytest.raises(ExtractorUnavailable) as caught:
        DoclingExtractor().extract(pdfs.with_text([PAGE_BODY]))

    message = str(caught.value)
    assert "docling" in message
    assert "worker image" in message


def test_the_extractor_registry_refuses_an_unknown_name() -> None:
    assert extractors.build_extractor().name == "pypdf"
    assert extractors.extractor_names() == ("docling", "pypdf")

    with pytest.raises(ExtractorUnavailable, match="no extractor named"):
        extractors.build_extractor("tesseract")


@pytest.mark.live
def test_docling_extracts_a_text_layer() -> None:
    """The one contract test for the heavy path, skipped by default exactly as
    `docs/PLAN.md` requires. It needs an image built with Docling — this repo does not
    install it, and CI must never download 2-5 GB of weights to test a route."""
    extracted = DoclingExtractor().extract(pdfs.with_text([PAGE_BODY]))

    assert extracted.page_count == 1
    assert extracted.char_count > 0


# ---------------------------------------------------------------------------
# The worker loop
# ---------------------------------------------------------------------------


class _ClientApi:
    """`app.scripts.extract_datasheets.ApiClient` driven through `TestClient`.

    The loop is therefore tested against the **real routes, real service and real
    database** with no socket, no port and no running server — which is the whole
    reason `ApiClient` is a Protocol rather than a concrete `urllib` class.
    """

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.failures: list[tuple[str, str]] = []

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedDocument]:
        body = self.client.post(
            "/api/extraction/claims", json={"worker_id": worker_id, "limit": limit}
        ).json()
        return [
            QueuedDocument(
                sha256=claim["sha256"],
                media_type=claim["media_type"],
                byte_size=claim["byte_size"],
                url=claim["url"],
                attempts=claim["attempts"],
            )
            for claim in body["claims"]
        ]

    def download(self, document: QueuedDocument) -> bytes:
        response = self.client.get(document.url)
        response.raise_for_status()
        return bytes(response.content)

    def submit_text(self, *, sha256: str, extractor: str, pages: Sequence[str]) -> None:
        response = self.client.post(
            "/api/extraction/results",
            json={"sha256": sha256, "extractor": extractor, "pages": list(pages)},
        )
        response.raise_for_status()

    def submit_failure(self, *, sha256: str, extractor: str, error: str) -> None:
        self.failures.append((sha256, error))
        response = self.client.post(
            "/api/extraction/results",
            json={"sha256": sha256, "extractor": extractor, "error": error},
        )
        response.raise_for_status()


class _BrokenExtractor:
    """Whatever a parser does on a document it cannot read."""

    name = "broken"

    def extract(self, data: bytes) -> extractors.ExtractedText:
        raise ValueError("PdfReadError: could not find xref table")


class _MissingExtractor:
    """A worker image built without its parser — a deployment error, not a fact about
    any document."""

    name = "absent"

    def extract(self, data: bytes) -> extractors.ExtractedText:
        raise ExtractorUnavailable("pypdf is not installed")


def test_the_worker_drains_the_queue_over_http(client: TestClient, db: Session) -> None:
    pytest.importorskip("pypdf", reason="the `datasheets` extra; the API never has it")
    digests = []
    for index in range(3):
        data = pdfs.with_text([f"{PAGE_BODY} document {index}"])
        assert _upload(client, data).status_code == 200
        digests.append(_sha(data))

    extracted = run(
        _ClientApi(client),
        PyPdfExtractor(),
        worker_id="worker-1",
        limit=2,
        max_batches=None,
        poll_seconds=0,
    )

    assert extracted == 3
    for digest in digests:
        body = client.get(f"/api/documents/{digest}/text").json()
        assert body["state"] == ExtractionState.EXTRACTED
        assert body["low_confidence"] is False
        assert body["extractor"] == "pypdf"
    assert client.get("/api/extraction/status").json()["pending"] == 0


def test_the_worker_reports_a_parser_failure_and_keeps_going(
    client: TestClient, db: Session
) -> None:
    """One unreadable datasheet must not park the backlog behind it."""
    first = pdfs.with_text([f"{PAGE_BODY} a"])
    second = pdfs.with_text([f"{PAGE_BODY} b"])
    _upload(client, first)
    _upload(client, second)
    api = _ClientApi(client)

    extracted = run_once(api, _BrokenExtractor(), worker_id="w", limit=2)

    assert extracted == 0
    assert len(api.failures) == 2
    for digest in (_sha(first), _sha(second)):
        body = client.get(f"/api/documents/{digest}/text").json()
        # Back in the queue with the attempt counted, not failed outright.
        assert body["state"] == ExtractionState.PENDING
        assert body["attempts"] == 1
        assert "PdfReadError" in body["error"]


def test_the_worker_does_not_blame_a_document_for_a_missing_extractor(
    client: TestClient,
) -> None:
    """Reporting it per document would spend the attempts of everything in the queue on
    a deployment mistake, and the documents are fine."""
    data = pdfs.with_text([PAGE_BODY])
    _upload(client, data)
    api = _ClientApi(client)
    (document,) = api.claim(worker_id="w", limit=1)

    with pytest.raises(ExtractorUnavailable):
        process_one(api, _MissingExtractor(), document)

    assert api.failures == []


def test_the_worker_stops_on_an_empty_queue(client: TestClient) -> None:
    """`--once` semantics: nothing to do is not an error and does not sleep."""
    assert run(_ClientApi(client), PyPdfExtractor(), worker_id="w", max_batches=None) == 0


# ---------------------------------------------------------------------------
# The separation ADR 0005 is actually about
# ---------------------------------------------------------------------------


def test_the_api_never_imports_a_pdf_library() -> None:
    """The invariant the whole ADR exists for, checked rather than trusted.

    In a **subprocess**, because an in-process check would pass or fail depending on
    which test imported `pypdf` first. If this ever fails it means something reachable
    from a route imported an extractor, and the consequence is a multi-gigabyte image
    built in CI on every push to serve a route that streams a file.
    """
    probe = (
        "import sys, app.main;"
        "leaked = [name for name in ('pypdf', 'app.services.extractors')"
        " if name in sys.modules];"
        "print(','.join(leaked));"
        "sys.exit(1 if leaked else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"the API imported: {result.stdout.strip()}"
