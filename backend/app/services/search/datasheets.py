"""Full-text search over `datasheet_fts` — Phase 4's standalone value.

`docs/PLAN.md` calls this out on its own: "useful standalone: full-text search
across every PDF you own." It is deliberately **not** folded into
`app.services.search.query_builder` (part search): that engine ranks *parts*,
composing `part_fts` and `datasheet_fts` with a dampening factor per
`docs/PLAN.md`'s line 296 — a part-search feature for a later chunk. This module
answers a different question — "which documents, and where in them" — and its
result is a document plus a snippet, not a part.

## Filter first, then rank — same shape as part search, same reason

`_matching_ids` narrows to the rowids FTS5 says match; the outer query only ever
ranks and snippets what already survived that filter. The candidate set for a
personal PDF collection is at most a few hundred documents, so this stays a term
lookup rather than a scan.

## The MATCH is repeated in every auxiliary-function subquery, on purpose

`bm25()` and `snippet()` are FTS5 **auxiliary functions**: each one scores or
extracts against *the query that is currently running against the FTS5 table*,
not against an arbitrary row. A correlated subquery that only says
`WHERE datasheet_fts.rowid = documents.id` has no such query — SQLite does not
error, it silently returns `-0.0` for `bm25()` for every row and a
first-few-tokens slice for `snippet()`. That is the exact bug
`app.services.search.query_builder._bm25_expression`'s docstring already warns
about for `part_fts`, verified interactively:

    >>> # no MATCH in the correlated subquery
    >>> bm25(t)   # every row, regardless of content
    -0.0

So both `_bm25_expression` and `_snippet_expression` below repeat
`datasheet_fts MATCH :query` in their own `WHERE`, exactly as the outer filter
does. `test_ranking_reflects_relevance_not_insertion_order` in
`tests/integration/test_datasheet_search.py` is the regression test: with the
repeat removed, `bm25()` ties every row at `-0.0` and the order silently
collapses to the `Document.id` tie-break, which that test would catch without a
crash to point at the cause.

## No prefix wildcard on the last token

`build_match_query`'s `prefix_last=True` default makes the part-search box
type-ahead. Datasheet search does not get that: the migration that created
`datasheet_fts` (`dcfe797424e9`) deliberately gave it no prefix index, reasoning
that "searching datasheets is a whole-word activity ('thermal resistance'), not
a type-ahead," and a wildcard query against a table with no prefix index is
exactly the unindexed term-dictionary scan that decision was avoiding. So this
module calls `build_match_query(text, prefix_last=False)`.

## Snippets are pre-split, never raw markup

`snippet()`'s highlight markers are two ASCII control characters
(`\\x01`/`\\x02`), not HTML. Datasheet text is attacker-reachable — anyone who
can upload a PDF controls what ends up in this index — so if the wire format
were `<mark>…</mark>` sitting in a string, a client would need to either render
it with `dangerouslySetInnerHTML` (an XSS on a hostile PDF's text) or reimplement
the marker parsing itself. `_split_snippet` does that parsing once, here, and the
wire type is a list of `{text, highlighted}` segments that any client renders as
plain text nodes — there is no HTML for a malicious datasheet's own words to hide
inside.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ColumnElement, ScalarSelect, column, func, select, text
from sqlalchemy.orm import Session

from app.models.documents import Document
from app.services.search.fts import build_match_query

#: Control characters, not HTML — see the module docstring. Chosen because
#: extracted PDF text is prose; a stray SOH/STX surviving inside a match window
#: is not a realistic collision, and even if one occurred the fallback in
#: `_split_snippet` degrades to "one fewer split point," never to broken markup.
_SNIPPET_START = "\x01"
_SNIPPET_END = "\x02"

#: FTS5 caps `snippet()`'s token count at 64. A window this wide would show more
#: of the document's *neighbourhood* than of *why it matched*; a search result
#: list is trying to answer the second question.
_SNIPPET_TOKENS = 24

#: A page of dense datasheet text; `_MAX_TOKENS` on `build_match_query` already
#: bounds token count, so this is a belt-and-braces cap on the raw querystring
#: itself, matching the shape of every other bounded API string field.
MAX_QUERY_LENGTH = 200

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True)
class SnippetSegment:
    """One run of a snippet: plain text, or the matched term it surrounds."""

    text: str
    highlighted: bool


@dataclass(frozen=True)
class DatasheetHit:
    """One matching document, plus why it matched."""

    document: Document
    snippet: tuple[SnippetSegment, ...]


def _matching_ids(match_query: str) -> ScalarSelect[int]:
    """Rowids FTS5 says match — the filter half of "filter first, then rank"."""
    return (
        select(column("rowid"))
        .select_from(text("datasheet_fts"))
        .where(text("datasheet_fts MATCH :fts_query"))
        .params(fts_query=match_query)
        .scalar_subquery()
    )


def _bm25_expression(match_query: str) -> ColumnElement[float]:
    """Relevance for the current row, correlated on `datasheet_fts.rowid`.

    See the module docstring: the `MATCH` here is not redundant with the outer
    filter's — it is what gives `bm25()` a query to score this row against.
    `datasheet_fts` has one column, so no per-column weights are needed (compare
    `query_builder._bm25_expression`'s five for `part_fts`).
    """
    return (
        select(func.bm25(text("datasheet_fts")))
        .select_from(text("datasheet_fts"))
        .where(text("datasheet_fts MATCH :rank_query"), text("datasheet_fts.rowid = documents.id"))
        .params(rank_query=match_query)
        .scalar_subquery()
    )


def _snippet_expression(match_query: str) -> ColumnElement[str]:
    """The excerpt `snippet()` builds around the match, with the same repeated
    `MATCH` `_bm25_expression` needs and for the identical reason."""
    return (
        select(
            func.snippet(
                text("datasheet_fts"), 0, _SNIPPET_START, _SNIPPET_END, "…", _SNIPPET_TOKENS
            )
        )
        .select_from(text("datasheet_fts"))
        .where(text("datasheet_fts MATCH :snip_query"), text("datasheet_fts.rowid = documents.id"))
        .params(snip_query=match_query)
        .scalar_subquery()
    )


def _split_snippet(raw: str) -> tuple[SnippetSegment, ...]:
    """Turn `snippet()`'s marker-delimited string into plain-text segments.

    Tolerant of a missing closing marker (treats the remainder as unhighlighted)
    rather than raising: `snippet()`'s own output is trusted, but a parser that
    can crash on its input is a second thing that can be wrong, for no benefit —
    the worst a malformed split can do is under-highlight.
    """
    segments: list[SnippetSegment] = []
    remaining = raw
    while remaining:
        start = remaining.find(_SNIPPET_START)
        if start == -1:
            segments.append(SnippetSegment(text=remaining, highlighted=False))
            break
        if start > 0:
            segments.append(SnippetSegment(text=remaining[:start], highlighted=False))
        end = remaining.find(_SNIPPET_END, start + 1)
        if end == -1:
            segments.append(SnippetSegment(text=remaining[start + 1 :], highlighted=False))
            break
        segments.append(SnippetSegment(text=remaining[start + 1 : end], highlighted=True))
        remaining = remaining[end + 1 :]
    return tuple(segments)


def search(
    session: Session, query_text: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> list[DatasheetHit]:
    """Documents whose extracted text matches `query_text`, best match first.

    Empty when `query_text` has no searchable token (pure punctuation) or when
    nothing matches — both are ordinary "no results," never an error. A document
    that has not been extracted yet cannot appear: it simply has no row in
    `datasheet_fts` to be found by, which is `app.services.document_text`'s
    first-class "not extracted" state working exactly as intended here too.
    """
    match_query = build_match_query(query_text, prefix_last=False)
    if match_query is None:
        return []

    matching = _matching_ids(match_query)
    statement = (
        select(Document, _snippet_expression(match_query).label("snippet"))
        .where(Document.id.in_(matching))
        .order_by(_bm25_expression(match_query), Document.id)
        .limit(limit)
        .offset(offset)
    )
    rows = session.execute(statement).all()
    return [
        DatasheetHit(document=document, snippet=_split_snippet(snippet))
        for document, snippet in rows
    ]


def count(session: Session, query_text: str) -> int:
    """How many documents match, ignoring `limit`/`offset` — for "N results"."""
    match_query = build_match_query(query_text, prefix_last=False)
    if match_query is None:
        return 0

    matching = _matching_ids(match_query)
    inner = select(Document.id).where(Document.id.in_(matching)).subquery()
    return int(session.execute(select(func.count()).select_from(inner)).scalar_one())


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_QUERY_LENGTH",
    "DatasheetHit",
    "SnippetSegment",
    "count",
    "search",
]
