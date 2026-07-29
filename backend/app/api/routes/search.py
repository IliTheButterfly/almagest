"""Parametric search over parts, and full-text search over datasheets.

`POST /api/search/parts` takes a structured filter list; `GET` takes a
querystring and builds the identical `SearchQuery`. Both run the same executor
and the same value parser, so a pasted URL and an API call can never disagree
about what a query means — which is the only reason the GET alias is safe to
offer at all.

`GET /api/search/datasheets` is a different, standalone feature —
`docs/PLAN.md`'s "useful standalone: full-text search across every PDF you
own" — and lives here rather than in `app.api.routes.documents` because it is
search, not storage: see `app.services.search.datasheets` for the ranking and
why it is not simply a datasheet-flavoured part search.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.limits import ResultOffset
from app.api.routes.documents import document_url
from app.api.schemas import FilterIn, PartQueryRequest
from app.db.session import get_db
from app.models.stock import StockLot
from app.services.search import datasheets, query_builder
from app.services.search.datasheets import DatasheetHit, SnippetSegment
from app.services.search.query_builder import FilterError, Mode, UnknownTemplate

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchRequest(PartQueryRequest):
    """Every narrowing field lives on the shared base, so `/api/parameter-templates`
    describes the same set this returns. Only pagination is search's own."""

    limit: int = Field(default=50, ge=1, le=500)
    #: `ResultOffset`, not a bare `int` with `ge=0`: an unbounded offset reaches
    #: SQLite's parameter binding and 500s. See `app.api.limits`.
    offset: ResultOffset = 0


class PartSummary(BaseModel):
    id: int
    name: str
    mpn: str | None
    description: str | None
    is_stub: bool
    category_id: int | None
    # These three are **required, not defaulted**. A default makes them optional
    # in the OpenAPI document, so every generated client has to handle
    # `undefined` for a field the server always sends — and `qty ?? 0` in a
    # client is indistinguishable from a genuine zero. The server computes them
    # for every row, so the schema should say so.
    #
    #: Total across every lot of this part, in thousandths of its unit. Results
    #: are ordered stock-first, so a row that cannot say *how much* leaves the
    #: ordering looking arbitrary — this is the number that explains the sort.
    qty_milli: int
    #: How many lots hold it, because quantity lives on the lot and "500 in 2
    #: lots" is a different physical situation from "500 on one reel". Counted on
    #: the same `qty > 0` test as `in_stock_only` and the ordering, so a row can
    #: never read `0 lots` while sorting as though it were stocked.
    lot_count: int
    #: Distinct containers, for "in 2 bins" — the reason to expand a row.
    location_count: int


class SearchResponse(BaseModel):
    #: Total matches ignoring pagination, so the UI can show "showing 50 of 213".
    total: int
    results: list[PartSummary]


def _stock_by_part(db: Session, part_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    """Quantity, lot count and container count for one page of results.

    One aggregate over the page rather than a per-row query — the page is capped
    at 500, so this is a single bounded round trip instead of N. Deliberately not
    folded into the search statement: the FTS ranking already correlates a
    subquery per row, and adding a stock join there would multiply rows and force
    a DISTINCT that discards the ordering.

    Reads `stock_lots.qty_milli_cached`. **Never `SUM(stock_ledger.delta_milli)`**
    — summing the ledger in an API path is what stops working at 200k rows, and
    the nightly job is what proves the cache honest.
    """
    if not part_ids:
        return {}

    rows = db.execute(
        select(
            StockLot.part_id,
            func.coalesce(func.sum(StockLot.qty_milli_cached), 0),
            func.count(StockLot.id),
            func.count(func.distinct(StockLot.location_id)),
        )
        .where(StockLot.part_id.in_(part_ids), StockLot.qty_milli_cached > 0)
        .group_by(StockLot.part_id)
    ).all()
    return {row[0]: (int(row[1]), int(row[2]), int(row[3])) for row in rows}


def _run(db: Session, request: SearchRequest) -> SearchResponse:
    query = request.to_query(limit=request.limit, offset=request.offset)
    try:
        results = query_builder.execute(db, query)
        total = query_builder.count(db, query)
    except UnknownTemplate as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except FilterError as error:
        # 422, not 400: the request is well-formed, the *value* is not
        # interpretable for that template. The reason code lets the UI say
        # something better than "invalid input" — most usefully, that a
        # megafarad is not a thing.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"template": error.template, "reason": error.reason, "message": str(error)},
        ) from error

    stock = _stock_by_part(db, [part.id for part in results])
    summaries = []
    for part in results:
        qty_milli, lot_count, location_count = stock.get(part.id, (0, 0, 0))
        summaries.append(
            PartSummary(
                id=part.id,
                name=part.name,
                mpn=part.mpn,
                description=part.description,
                is_stub=part.is_stub,
                category_id=part.category_id,
                qty_milli=qty_milli,
                lot_count=lot_count,
                location_count=location_count,
            )
        )

    return SearchResponse(total=total, results=summaries)


@router.post("/parts", response_model=SearchResponse)
def search_parts(request: SearchRequest, db: Session = Depends(get_db)) -> SearchResponse:
    return _run(db, request)


@router.get("/parts", response_model=SearchResponse)
def search_parts_by_querystring(
    db: Session = Depends(get_db),
    f: list[str] = Query(
        default_factory=list,
        description="Repeatable `template:value`, e.g. `f=capacitance:20-30uF&f=mounting_type:THT`",
    ),
    text: str | None = None,
    category: str | None = None,
    part_kind: str | None = None,
    in_stock_only: bool = False,
    include_stubs: bool = True,
    mode: Mode = "search",
    limit: int = Query(default=50, ge=1, le=500),
    offset: Annotated[ResultOffset, Query()] = 0,
) -> SearchResponse:
    filters = []
    for raw in f:
        template, separator, value = raw.partition(":")
        if not separator or not template.strip() or not value.strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"filter {raw!r} is not in `template:value` form",
            )
        filters.append(FilterIn(template=template.strip(), value=value.strip()))

    return _run(
        db,
        SearchRequest(
            filters=filters,
            text=text,
            category=category,
            part_kind=part_kind,
            in_stock_only=in_stock_only,
            include_stubs=include_stubs,
            mode=mode,
            limit=limit,
            offset=offset,
        ),
    )


# ---------------------------------------------------------------------------
# Datasheet full-text search — see `app.services.search.datasheets`
# ---------------------------------------------------------------------------


class DatasheetSnippetSegment(BaseModel):
    """One run of a snippet. `highlighted` spans are the matched term(s);
    everything else is surrounding context. Plain text on both sides — see
    `app.services.search.datasheets`'s module docstring for why this is a list
    of segments and not a string with embedded markup."""

    text: str
    highlighted: bool


class DatasheetSearchHit(BaseModel):
    """One matching document. Deliberately the same document shape a client
    already knows from `app.api.routes.documents.DocumentRead` (sha256, url,
    page_count, ...), so a search result and an attached-document row render
    with the same component."""

    sha256: str
    kind: str
    media_type: str
    byte_size: int
    page_count: int | None
    original_filename: str | None
    url: str
    snippet: list[DatasheetSnippetSegment]


class DatasheetSearchResponse(BaseModel):
    #: Total matches ignoring pagination, mirroring `SearchResponse.total`.
    total: int
    results: list[DatasheetSearchHit]


def _snippet_read(segments: tuple[SnippetSegment, ...]) -> list[DatasheetSnippetSegment]:
    return [DatasheetSnippetSegment(text=s.text, highlighted=s.highlighted) for s in segments]


def _hit_read(hit: DatasheetHit) -> DatasheetSearchHit:
    document = hit.document
    return DatasheetSearchHit(
        sha256=document.sha256,
        kind=document.kind,
        media_type=document.media_type,
        byte_size=document.byte_size,
        page_count=document.page_count,
        original_filename=document.original_filename,
        url=document_url(document.sha256),
        snippet=_snippet_read(hit.snippet),
    )


@router.get("/datasheets", response_model=DatasheetSearchResponse)
def search_datasheets(
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=datasheets.MAX_QUERY_LENGTH,
            description="Free text, matched against every PDF's extracted text.",
        ),
    ],
    db: Session = Depends(get_db),
    limit: int = Query(default=datasheets.DEFAULT_LIMIT, ge=1, le=datasheets.MAX_LIMIT),
    offset: Annotated[ResultOffset, Query()] = 0,
) -> DatasheetSearchResponse:
    """Full-text search across every stored PDF's extracted text.

    **Never errors on hostile input.** `q` reaches `build_match_query`, which
    allowlists tokens rather than escaping FTS5 syntax — see
    `app.services.search.fts`'s module docstring — so a stray `"`, `*` or `NEAR`
    in the box narrows to nothing (or to a literal word) instead of a 500.

    A document nobody has extracted yet — the normal state of a freshly
    uploaded PDF per ADR 0005 — is absent from `results` and does not affect
    `total`. That is not a bug to route around; it is search over the text that
    exists, honestly reporting that some of it does not exist yet.
    """
    return DatasheetSearchResponse(
        total=datasheets.count(db, q),
        results=[_hit_read(hit) for hit in datasheets.search(db, q, limit=limit, offset=offset)],
    )
