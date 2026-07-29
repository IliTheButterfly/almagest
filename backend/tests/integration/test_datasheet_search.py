"""`GET /api/search/datasheets` — full-text search over `datasheet_fts`.

`docs/PLAN.md` calls this out as Phase 4's standalone value, so these tests hit
the route over real HTTP against a real FTS5 index, the same posture
`test_fts_search.py` takes for part search.

Fixtures store a document with `app.services.documents.store_document`, then
write its text directly with `app.services.document_text.record_text` — the
same two-step split `test_extraction.py` uses — rather than going through the
extraction HTTP door, because the ranking and snippet behaviour under test does
not depend on how the text arrived.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.documents import Document
from app.models.enums import DocumentKind
from app.services import document_text, documents
from tests import pdfs

PDF = "application/pdf"


def _store_with_text(db: Session, pages: list[str], *, seed: bytes = b"") -> Document:
    """A document whose extracted text is exactly `pages` — not derived from the
    PDF's own content stream, so the fixture can say what it matches on without
    needing the PDF's embedded text to agree."""
    stored = documents.store_document(
        db,
        data=pdfs.with_text([seed.decode() or "filler"]),
        media_type=PDF,
        kind=DocumentKind.DATASHEET,
    )
    db.flush()
    document_text.record_text(db, document=stored.document, extractor="pypdf", pages=pages)
    db.commit()
    return stored.document


def _never_extracted(db: Session) -> Document:
    stored = documents.store_document(
        db, data=pdfs.with_text(["untouched"]), media_type=PDF, kind=DocumentKind.DATASHEET
    )
    db.commit()
    return stored.document


def _search(client: TestClient, q: str, **params: int) -> Any:
    # `Any`, not a typed dict: the point of this helper is the same one-liner
    # every other route test in this suite uses for `response.json()` — see
    # `test_extraction.py` — and giving it a concrete return type would just
    # move the untyped-JSON boundary here instead of removing it.
    query: dict[str, str | int] = {"q": q, **params}
    response = client.get("/api/search/datasheets", params=query)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The basics: a hit, and honestly nothing
# ---------------------------------------------------------------------------


def test_a_query_matching_extracted_text_finds_the_document(
    db: Session, client: TestClient
) -> None:
    document = _store_with_text(db, ["Thermal resistance junction to ambient 200 K/W."], seed=b"a")

    body = _search(client, "thermal resistance")

    assert body["total"] == 1
    assert [hit["sha256"] for hit in body["results"]] == [document.sha256]


def test_a_query_matching_nothing_returns_empty_not_an_error(
    db: Session, client: TestClient
) -> None:
    _store_with_text(db, ["absolute maximum ratings for a bipolar transistor"], seed=b"b")

    body = _search(client, "zzznonexistentword")

    assert body == {"total": 0, "results": []}


def test_a_never_extracted_document_is_absent_without_erroring(
    db: Session, client: TestClient
) -> None:
    """The ADR 0005 state: stored, served, attached — and simply not yet
    searchable. A stored-but-unread PDF must not make search error, and must not
    itself appear, even while other documents do."""
    unread = _never_extracted(db)
    findable = _store_with_text(db, ["capacitance tolerance and dielectric absorption"], seed=b"c")

    body = _search(client, "capacitance")

    sha256s = [hit["sha256"] for hit in body["results"]]
    assert sha256s == [findable.sha256]
    assert unread.sha256 not in sha256s


# ---------------------------------------------------------------------------
# Hostile input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ['" OR 1=1 --', "mpn:secret", "^anchor", "a NEAR b", "(unbalanced", "***", "resistor*"],
)
def test_a_hostile_query_never_errors_over_http(
    db: Session, client: TestClient, hostile: str
) -> None:
    """Mirrors `test_fts_search.py`'s part-search version of this test. A stray
    quote or FTS5 operator typed into a search box must 200, not 500 — this is
    the failure `build_match_query`'s allowlist exists to prevent, verified
    against real SQLite rather than only against the query builder."""
    _store_with_text(db, ["ordinary datasheet prose"], seed=b"d")

    body = _search(client, hostile)

    assert "results" in body


def test_punctuation_only_query_finds_nothing_rather_than_everything(
    db: Session, client: TestClient
) -> None:
    _store_with_text(db, ["ordinary datasheet prose"], seed=b"e")

    assert _search(client, "!!!") == {"total": 0, "results": []}


# ---------------------------------------------------------------------------
# Ranking — the regression test for the repeated-MATCH bug
# ---------------------------------------------------------------------------


def test_ranking_reflects_relevance_not_insertion_order(db: Session, client: TestClient) -> None:
    """`bm25()` is an auxiliary function: called in a correlated subquery that
    does not itself repeat `datasheet_fts MATCH`, it does not error — it returns
    `-0.0` for every row, which ties every match and lets the query fall back to
    the `Document.id` tie-break. So this test deliberately stores the *weaker*
    match first: if `_bm25_expression` or `_snippet_expression` ever loses its
    repeated `MATCH`, insertion order silently reasserts itself and this goes
    red without a single line raising an exception anywhere.
    """
    weak = _store_with_text(db, ["ferrite is mentioned exactly once in passing here"], seed=b"weak")
    strong = _store_with_text(
        db,
        ["ferrite bead ferrite core ferrite choke, all ferrite, entirely about ferrite"],
        seed=b"strong",
    )
    assert weak.id < strong.id  # the order a broken ranking would fall back to

    body = _search(client, "ferrite")

    assert [hit["sha256"] for hit in body["results"]] == [strong.sha256, weak.sha256]


def test_pagination_does_not_repeat_or_drop_documents(db: Session, client: TestClient) -> None:
    documents_ = [
        _store_with_text(db, [f"widget number {i} thermal profile"], seed=f"pg{i}".encode())
        for i in range(6)
    ]
    del documents_

    page_one = _search(client, "thermal", limit=3, offset=0)
    page_two = _search(client, "thermal", limit=3, offset=3)

    assert page_one["total"] == 6
    assert page_two["total"] == 6
    one = {hit["sha256"] for hit in page_one["results"]}
    two = {hit["sha256"] for hit in page_two["results"]}
    assert len(one) == 3
    assert len(two) == 3
    assert not one & two


# ---------------------------------------------------------------------------
# Snippets
# ---------------------------------------------------------------------------


def test_the_snippet_highlights_the_matched_term(db: Session, client: TestClient) -> None:
    _store_with_text(
        db,
        ["Recommended operating conditions and electrical characteristics at 25 C."],
        seed=b"snip",
    )

    body = _search(client, "electrical characteristics")

    [hit] = body["results"]
    segments = hit["snippet"]
    assert any(seg["highlighted"] for seg in segments)
    highlighted_text = " ".join(seg["text"] for seg in segments if seg["highlighted"]).lower()
    assert "electrical" in highlighted_text or "characteristics" in highlighted_text
    # Unhighlighted context survives too, so the hit reads as a sentence.
    assert any(not seg["highlighted"] for seg in segments)


def test_a_document_with_no_extracted_pages_yields_no_snippet_and_no_hit(
    db: Session, client: TestClient
) -> None:
    """`record_text` with an empty page list is a legitimate outcome (a
    page-image PDF read and found genuinely blank) — it must not crash a search
    that would otherwise have matched, and it correctly matches nothing since
    there is no text to match against."""
    stored = documents.store_document(
        db, data=pdfs.with_text(["blank"]), media_type=PDF, kind=DocumentKind.DATASHEET
    )
    db.flush()
    document_text.record_text(db, document=stored.document, extractor="pypdf", pages=[""])
    db.commit()

    body = _search(client, "anything")
    assert body == {"total": 0, "results": []}
