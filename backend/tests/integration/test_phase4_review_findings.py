"""Regressions for the seven defects adversarial review found in Phase 4.

Every one was reproduced before it was fixed, and — as in
`test_phase2_review_findings.py` — the shapes repeat, so they are collected by
shape rather than filed under the feature each belongs to.

* **client text that reaches a response header or a filesystem path.** The
  `Content-Disposition` filename was allowlisted precisely because "a header
  value containing CR/LF is header injection", and then `media_type` reached a
  header the same way with nothing done to it: only the part before the first
  `;` was ever validated, and the *raw* string was stored and served. One POST
  bricked a document's read route permanently, because a re-upload of the same
  bytes is documented to change nothing about the existing row.
* **check-then-insert on a content address.** `store_document` looked a sha256
  up and inserted when it missed. The module docstring calls the address "the
  idempotency key … across devices and across restarts", and route handlers are
  `def`, so two threads land in the window and one gets a `UNIQUE constraint
  failed` 500 — for the exact retry-over-flaky-wifi case the docstring names.
* **a derived copy keyed on a reusable id.** `datasheet_fts.rowid` is
  `documents.id`, which SQLite reuses after a delete, and nothing cleared the
  index when a row was created. Document B then reported document A's text —
  in an envelope whose `state` said `pending`, which promises `text` is null.
* **repair code that destroys what the writers promise to keep.**
  `reconcile()` treated every indexed rowid whose document was not `EXTRACTED`
  as an orphan, while `requeue()` and `record_failure()` both document leaving
  existing text searchable on purpose. For an image the loss is permanent:
  nothing re-queues a `not_applicable` document.
* **an identity key that is not unique.** `claim()` re-read what it had granted
  by `(holder, instant)`, so two claims by one worker in the same microsecond
  each reported everything that worker held — double-spending the only
  expensive step in the pipeline.
* **an unbounded integer in a query string.** `offset` had `ge=0` and no
  ceiling, so `10**30` reached sqlite3's parameter binding and raised
  `OverflowError` — a bare 500 for input `app/api/limits.py` exists to reject.
* **dedup that trusts a name it did not check.** A blob whose bytes do not hash
  to its own filename was adopted by the next upload of the correct bytes,
  served as authoritative with `immutable`, and never repaired — and `verify`,
  the scrub the store's docstring defers to, had no caller anywhere.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.documents import Document
from app.models.enums import DocumentKind, ExtractionState
from app.services import blobstore, document_text, documents
from tests import pdfs
from tests.factories import make_part

PDF = "application/pdf"
PNG = "image/png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"tray photo"

#: One page of prose, long enough to clear `LOW_CONFIDENCE_CHARS_PER_PAGE` so a
#: submission comes out `extracted` and trusted rather than flagged.
PAGE_BODY = (
    "Absolute Maximum Ratings: VCE 60 V, IC 600 mA. Thermal resistance junction "
    "to ambient 200 K/W. Storage temperature -55 to +150 C. Electrical "
    "characteristics measured at Tamb 25 C unless otherwise noted."
)


def _pdf(body: bytes = b"one") -> bytes:
    return b"%PDF-1.7\n" + body + b"\n%%EOF\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload(
    client: TestClient, data: bytes = _pdf(), *, media_type: str = PDF, **params: object
) -> Response:
    return client.post(
        "/api/documents",
        content=data,
        params={"media_type": media_type, **params},
        headers={"Content-Type": "application/octet-stream"},
    )


def _submit(client: TestClient, sha256: str, pages: list[str], *, extractor: str = "pypdf") -> None:
    response = client.post(
        "/api/extraction/results",
        json={"sha256": sha256, "extractor": extractor, "pages": pages},
    )
    assert response.status_code == 200, response.text


def _datasheet_hits(client: TestClient, query: str) -> int:
    response = client.get("/api/search/datasheets", params={"q": query})
    assert response.status_code == 200, response.text
    return int(response.json()["total"])


@pytest.fixture
def second_session(engine: object) -> Iterator[Session]:
    """A second session on the same database — the other request in a race.

    Separate from the `db` fixture on purpose: a race is two connections, and two
    `Session`s sharing one would serialise into the very interleave under test.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# 1. A media type's parameters reached a response header verbatim
# ---------------------------------------------------------------------------


def test_a_media_type_parameter_cannot_brick_the_documents_own_read_route(
    client: TestClient, db: Session
) -> None:
    """The defect: `suffix_for` validated `media_type.split(";")[0]` and the row
    stored the **raw** string, which `read_document` then handed to
    `FileResponse(media_type=...)`.

    `application/pdf; charset=x\\r\\nX-Injected: yes` was accepted, stored, and
    every later GET of that document died in the server's header writer
    (`RuntimeError: Invalid HTTP header value.` under uvicorn); a non-latin-1
    parameter raised `UnicodeEncodeError` in Starlette's `init_headers` instead,
    which is a 500 even under `TestClient`.

    Permanent, too: re-uploading the identical bytes with a clean media type
    answers `{"created": false}` and is documented to change nothing about the
    existing row, so nothing short of DB surgery could repair it.
    """
    injected = "application/pdf; charset=x\r\nX-Injected: yes"
    upload = _upload(client, _pdf(b"injected"), media_type=injected)
    assert upload.status_code == 200, upload.text
    digest = upload.json()["document"]["sha256"]

    # Stored normalised, not verbatim: the parameters were never validated, so
    # they are not carried into a column that is served as a header.
    stored = db.execute(select(Document).where(Document.sha256 == digest)).scalar_one()
    assert stored.media_type == PDF

    read = client.get(f"/api/documents/{digest}")
    assert read.status_code == 200
    assert read.headers["content-type"] == PDF
    assert "x-injected" not in read.headers


def test_a_non_latin_1_media_type_parameter_is_not_stored(client: TestClient) -> None:
    """The same defect, in the variant that 500s even in-process: a header value
    outside latin-1 cannot be encoded at all, so the read route raised rather
    than answering."""
    upload = _upload(client, _pdf(b"unicode"), media_type="application/pdf;x=中")
    assert upload.status_code == 200, upload.text
    digest = upload.json()["document"]["sha256"]
    assert upload.json()["document"]["media_type"] == PDF

    read = client.get(f"/api/documents/{digest}")
    assert read.status_code == 200
    assert read.headers["content-type"] == PDF


def test_media_type_case_and_padding_are_folded_before_they_are_stored(
    client: TestClient,
) -> None:
    """`" APPLICATION/PDF "` was stored and served verbatim too. It addresses the
    same format, so it must not produce a second spelling of `media_type` that
    every consumer — the queue's `EXTRACTABLE_MEDIA_TYPES`, a client's `switch` —
    has to know to fold for itself."""
    upload = _upload(client, _pdf(b"padded"), media_type="  APPLICATION/PDF  ")
    assert upload.status_code == 200, upload.text
    assert upload.json()["document"]["media_type"] == PDF


def test_a_row_whose_media_type_cannot_be_a_header_is_still_served(
    client: TestClient, db: Session
) -> None:
    """The second half of the fix, and the reason it is worth having: intake
    normalisation protects rows this API writes, and nothing protects a row a
    migration, a restore or `sqlite3` wrote. A document must never be able to make
    its own read route unserviceable, so the header value is checked where it is
    used — exactly as `content_disposition` already does for the filename."""
    upload = _upload(client, _pdf(b"hand-edited"))
    digest = upload.json()["document"]["sha256"]
    db.execute(
        text("UPDATE documents SET media_type = :m WHERE sha256 = :s"),
        {"m": "application/pdf; x=\r\nX-Injected: yes", "s": digest},
    )
    db.commit()

    read = client.get(f"/api/documents/{digest}")
    assert read.status_code == 200
    # Degraded to a download rather than made unreachable: the bytes are fine.
    assert read.headers["content-type"] == "application/octet-stream"
    assert "x-injected" not in read.headers
    assert read.content == _pdf(b"hand-edited")


# ---------------------------------------------------------------------------
# 2. Two uploads racing on the same new bytes
# ---------------------------------------------------------------------------


def test_an_upload_that_loses_the_insert_race_answers_created_false(
    db: Session, second_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: check-then-insert with no unique-violation handling. Eight
    parallel uploads of one new PDF answered 4x200 and 4x500 with
    `UNIQUE constraint failed: documents.sha256`.

    Reproduced deterministically rather than with threads, for the reason
    `document_text._candidates` exists as a named function: the interleave is the
    whole point, and a test that has to win a race to see it is a test that
    passes on a fast machine. `by_sha256` is the seam — the other request commits
    the row between our check and our insert, which is exactly the window.
    """
    data = _pdf(b"racing")
    digest = _sha(data)
    original = documents.by_sha256

    def racing(session: Session, sha256: str) -> Document | None:
        found = original(session, sha256)
        if session is db and found is None:
            documents.store_document(
                second_session, data=data, media_type=PDF, kind=DocumentKind.DATASHEET
            )
            second_session.commit()
        return found

    monkeypatch.setattr(documents, "by_sha256", racing)

    stored = documents.store_document(db, data=data, media_type=PDF, kind=DocumentKind.DATASHEET)

    # The content address *is* the idempotency key, so the loser's answer is the
    # row the winner wrote — never an error, and never a second row.
    assert stored.created is False
    assert stored.document.sha256 == digest
    db.commit()
    assert db.execute(select(Document).where(Document.sha256 == digest)).scalars().all() != []
    count = db.execute(text("SELECT COUNT(*) FROM documents WHERE sha256 = :s"), {"s": digest})
    assert count.scalar_one() == 1


def test_the_losing_upload_still_reports_the_blob_it_wrote(
    db: Session, second_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deduplicated` must keep meaning "nothing was written to disk", which is a
    fact about the filesystem and not about who won the insert. The loser here
    wrote the blob itself (it got there first on disk), so it reports
    `deduplicated=False` alongside `created=False` — the mirror of the
    interrupted-upload case the wire type already documents."""
    data = _pdf(b"blob-then-row")
    original = documents.by_sha256

    def racing(session: Session, sha256: str) -> Document | None:
        found = original(session, sha256)
        if session is db and found is None:
            # The winner dedups onto the blob this call already wrote.
            documents.store_document(
                second_session, data=data, media_type=PDF, kind=DocumentKind.DATASHEET
            )
            second_session.commit()
        return found

    monkeypatch.setattr(documents, "by_sha256", racing)
    stored = documents.store_document(db, data=data, media_type=PDF, kind=DocumentKind.DATASHEET)
    assert (stored.created, stored.deduplicated) == (False, False)


# ---------------------------------------------------------------------------
# 3. A reused `documents.id` inherited the deleted document's text
# ---------------------------------------------------------------------------


def test_a_new_document_never_inherits_a_deleted_documents_indexed_text(
    client: TestClient, db: Session
) -> None:
    """The defect: `documents.id` is a rowid alias with no `AUTOINCREMENT`, so
    SQLite reuses it; `datasheet_fts` is keyed on it with no FK and no delete
    trigger; and creating a row never cleared the index at that id.

    So the next upload after a delete answered `GET .../text` with the previous
    document's text and `state: "pending"` — an envelope that contradicts itself,
    since `pending` promises nothing has been extracted — and appeared in
    datasheet search with the other document's snippet.
    """
    first = _upload(client, pdfs.with_text([PAGE_BODY]), filename="first.pdf")
    assert first.status_code == 200, first.text
    first_sha = first.json()["document"]["sha256"]
    _submit(client, first_sha, [f"CONFIDENTIAL {PAGE_BODY}"] * 3)
    assert _datasheet_hits(client, "CONFIDENTIAL") == 1

    reused_id = db.execute(select(Document.id).where(Document.sha256 == first_sha)).scalar_one()
    db.execute(text("DELETE FROM documents WHERE id = :id"), {"id": reused_id})
    db.commit()

    second = _upload(client, pdfs.with_text(["unrelated"]), filename="second.pdf")
    assert second.status_code == 200, second.text
    second_sha = second.json()["document"]["sha256"]
    # The premise: the id really is handed out again. Without this the test could
    # pass for the wrong reason if SQLite ever stopped reusing rowids.
    assert (
        db.execute(select(Document.id).where(Document.sha256 == second_sha)).scalar_one()
        == reused_id
    )

    body = client.get(f"/api/documents/{second_sha}/text")
    assert body.status_code == 200, body.text
    assert body.json()["state"] == ExtractionState.PENDING.value
    assert body.json()["text"] is None
    assert _datasheet_hits(client, "CONFIDENTIAL") == 0


# ---------------------------------------------------------------------------
# 4. `reconcile` deleted text the writers promise to keep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("advance_to", ["requeued", "claimed", "failed"])
def test_reconcile_keeps_text_a_re_extraction_deliberately_left_searchable(
    client: TestClient, db: Session, advance_to: str
) -> None:
    """The defect: `reconcile` deleted every indexed rowid whose document was not
    `EXTRACTED`, and three documented states legitimately hold text while not
    being `EXTRACTED` — all three reachable from public routes.

    `requeue`'s docstring: the existing text "stays searchable until the better
    run replaces it, because clearing it first would make search strictly
    worse". `record_failure`'s: text "is left alone. A failed re-run with a
    better extractor must not make search worse than it was before somebody
    tried to improve it." Repair code that contradicts both is worse than no
    repair code, because it runs unattended.
    """
    upload = _upload(client, pdfs.with_text([PAGE_BODY]))
    digest = upload.json()["document"]["sha256"]
    _submit(client, digest, [f"tantalum polymer {PAGE_BODY}"] * 2)
    assert _datasheet_hits(client, "tantalum") == 1

    requeue = client.post("/api/extraction/requeue", json={"sha256": digest})
    assert requeue.status_code == 200, requeue.text
    assert requeue.json()["had_text"] is True
    if advance_to in {"claimed", "failed"}:
        claim = client.post("/api/extraction/claims", json={"worker_id": "w1", "limit": 1})
        assert [c["sha256"] for c in claim.json()["claims"]] == [digest]
    if advance_to == "failed":
        failure = client.post(
            "/api/extraction/results",
            json={"sha256": digest, "extractor": "docling", "error": "docling segfaulted"},
        )
        assert failure.status_code == 200, failure.text

    report = document_text.reconcile(db)
    db.commit()

    assert report.orphaned_text == 0
    assert _datasheet_hits(client, "tantalum") == 1


def test_reconcile_does_not_delete_ocr_text_nothing_can_regenerate(
    client: TestClient, db: Session
) -> None:
    """The unrecoverable half. An image is `not_applicable`, which `_claimable`
    never selects, so text submitted for it — which `record_text` blesses, for
    exactly the photographed-marking case — can never be produced again.
    `reconcile` deleted it anyway, and for a PDF that is a re-run while for a
    PNG it is gone."""
    upload = _upload(client, PNG_BYTES, media_type=PNG, filename="marking.png")
    assert upload.status_code == 200, upload.text
    digest = upload.json()["document"]["sha256"]
    _submit(client, digest, [f"LM317T {PAGE_BODY}"], extractor="ocr")
    assert _datasheet_hits(client, "LM317T") == 1

    requeue = client.post("/api/extraction/requeue", json={"sha256": digest})
    assert requeue.status_code == 200, requeue.text
    assert requeue.json()["document"]["state"] == ExtractionState.NOT_APPLICABLE.value

    report = document_text.reconcile(db)
    db.commit()
    assert report.orphaned_text == 0
    assert _datasheet_hits(client, "LM317T") == 1


def test_reconcile_still_deletes_an_entry_no_document_ever_extracted(
    client: TestClient, db: Session
) -> None:
    """The other side of the same change: an index entry for a document that has
    recorded no extraction at all is still an orphan and is still deleted.
    Widening the predicate must not turn the repair into a no-op — that is how a
    fix to over-deletion becomes a leak like defect 3."""
    upload = _upload(client, pdfs.with_text([PAGE_BODY]))
    digest = upload.json()["document"]["sha256"]
    document = documents.by_sha256(db, digest)
    assert document is not None

    # Text at a rowid nothing has extracted: what a reused id or a hand-edited
    # row leaves behind.
    db.execute(
        text("INSERT INTO datasheet_fts (rowid, text) VALUES (:id, 'ghost tantalum')"),
        {"id": document.id},
    )
    db.commit()
    assert _datasheet_hits(client, "ghost") == 1

    report = document_text.reconcile(db)
    db.commit()
    assert report.orphaned_text == 1
    assert _datasheet_hits(client, "ghost") == 0


# ---------------------------------------------------------------------------
# 5. `claim` reported documents it had not granted
# ---------------------------------------------------------------------------


def test_a_claim_reports_only_the_documents_it_granted(db: Session) -> None:
    """The defect: the post-update re-read was keyed on
    `(extraction_claimed_by, extraction_claimed_at, CLAIMED)`, which is not
    unique per call. Two claims by one worker at the same instant each returned
    everything that worker held: `limit=1` answered with N claims, and the worker
    re-downloaded and re-extracted documents already in flight — the double-spend
    the compare-and-swap exists to prevent.

    Only reachable through the `now=` parameter today, because `utcnow()` is
    microsecond-resolution and a request round trip is far longer. That makes it
    a latent bug in the claim's identity key, which is precisely the kind that
    surfaces the day the clock gets coarser or a batch gets faster.
    """
    for index in range(3):
        documents.store_document(
            db, data=_pdf(f"claim-{index}".encode()), media_type=PDF, kind=DocumentKind.DATASHEET
        )
    db.flush()

    moment = document_text.utcnow()
    batches = [document_text.claim(db, worker_id="w1", limit=1, now=moment) for _ in range(3)]
    assert [len(batch) for batch in batches] == [1, 1, 1]
    granted = [document.id for batch in batches for document in batch]
    assert len(set(granted)) == 3


# ---------------------------------------------------------------------------
# 6. An unbounded `offset` reached sqlite3's parameter binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/search/datasheets",
        "/api/search/parts",
        # Not review findings themselves — the same defect, in the three routes
        # that already had it before this phase. They are fixed here because the
        # point of `app/api/limits.py` is that a bound is defined once: leaving
        # three call sites inventing their own is how the ninth inherits the gap,
        # which is the sentence its docstring opens with. Verified 500s first.
        "/api/intake/pending",
        "/api/projects",
    ],
)
def test_an_absurd_search_offset_is_a_422_not_a_500(client: TestClient, path: str) -> None:
    """The defect: `offset: int = Query(default=0, ge=0)` has no ceiling, so
    `10**30` reached SQLite's bind and raised `OverflowError` — the exact failure
    `app/api/limits.py`'s docstring was written about, which is why the fix is a
    shared alias there rather than an `le=` per call site."""
    response = client.get(path, params={"q": "tantalum", "offset": 10**30})
    assert response.status_code == 422, response.text


def test_an_absurd_offset_in_the_search_body_is_a_422_not_a_500(client: TestClient) -> None:
    """`POST /api/search/parts` carries the same field in a Pydantic model rather
    than a `Query`, which is a second place for the bound to be missing."""
    response = client.post("/api/search/parts", json={"offset": 10**30})
    assert response.status_code == 422, response.text


def test_a_reachable_page_is_still_served(client: TestClient) -> None:
    """The ceiling must be past anything real: a bound that rejected page 20 would
    have turned a 500 into a broken feature, which is the failure mode of fixing
    an input bound by guessing at it."""
    response = client.get("/api/search/parts", params={"offset": 1_000_000})
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 7. Dedup adopted a blob whose bytes do not hash to its name
# ---------------------------------------------------------------------------


def test_an_upload_repairs_a_blob_whose_size_contradicts_its_name() -> None:
    """The defect: dedup was `path.exists()`, so an upload of the correct bytes
    adopted a corrupt blob, answered `deduplicated: true`, and served the wrong
    bytes as authoritative with `Cache-Control: immutable` — actively hiding the
    corruption instead of fixing it, since the blob is only ever written when it
    is *absent*.

    The size check is free: the correct bytes and their length are already in
    memory, so a stored blob of a different length is provably not them. It does
    not replace `verify` (an equal-length corruption still needs the scrub) — it
    catches the truncated write and the half-restored file, which are the
    corruptions that actually happen.
    """
    data = _pdf(b"repairable" * 20)
    digest = _sha(data)
    path = blobstore.blob_path(digest, ".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF- TOTALLY DIFFERENT BYTES")
    assert blobstore.verify(digest, ".pdf") is False

    stored = blobstore.store(data, media_type=PDF)

    assert stored.deduplicated is False
    assert blobstore.verify(digest, ".pdf") is True
    assert path.read_bytes() == data


def test_the_scrub_finds_what_dedup_cannot(db: Session) -> None:
    """`blobstore.verify` had no caller anywhere in `app/`: the scrub job its
    module docstring defers to ("it belongs in a scrub job, which is what
    `verify` exists for") did not exist, so bit rot and a truncated restore were
    undetectable — and an upload of the correct bytes hid an equal-length
    corruption rather than reporting it.

    Both failure modes are reported separately because the recoveries differ: a
    missing blob is re-uploadable, a corrupt one must be replaced before anything
    trusts what it served.
    """
    good = documents.store_document(
        db, data=_pdf(b"intact"), media_type=PDF, kind=DocumentKind.DATASHEET
    ).document
    corrupt = documents.store_document(
        db, data=_pdf(b"rotten"), media_type=PDF, kind=DocumentKind.DATASHEET
    ).document
    missing = documents.store_document(
        db, data=_pdf(b"vanished"), media_type=PDF, kind=DocumentKind.DATASHEET
    ).document
    db.flush()

    assert documents.scrub(db).is_clean

    # Equal length, different bytes: exactly what dedup's size check cannot see.
    corrupt_path = blobstore.path_for(corrupt.storage_path)
    corrupt_path.write_bytes(b"X" * len(corrupt_path.read_bytes()))
    blobstore.path_for(missing.storage_path).unlink()

    report = documents.scrub(db)
    assert report.is_clean is False
    assert report.checked == 3
    assert report.corrupt == (corrupt.sha256,)
    assert report.missing == (missing.sha256,)
    assert good.sha256 not in report.corrupt + report.missing


def test_a_part_with_a_document_is_untouched_by_the_scrub(client: TestClient, db: Session) -> None:
    """The scrub reads and hashes; it must never write. A repair that deleted a
    row would turn one bad sector into a lost datasheet, and the blob is
    re-fetchable while the row's metadata is not."""
    part = make_part(db, mpn="SCRUB-1")
    db.commit()
    upload = _upload(client, _pdf(b"scrubbed"), part_id=part.id)
    assert upload.status_code == 200, upload.text
    digest = upload.json()["document"]["sha256"]

    blobstore.path_for(
        db.execute(select(Document.storage_path).where(Document.sha256 == digest)).scalar_one()
    ).write_bytes(b"%PDF-corrupted-same-length!!")

    report = documents.scrub(db)
    assert report.corrupt == (digest,)
    # Still listed, still attached, still recorded — the scrub reports, nothing else.
    assert (
        client.get(f"/api/parts/{part.id}/documents").json()["links"][0]["document"]["sha256"]
        == digest
    )
