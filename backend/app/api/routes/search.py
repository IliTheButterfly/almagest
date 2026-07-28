"""Parametric search endpoints.

`POST` takes a structured filter list; `GET` takes a querystring and builds the
identical `SearchQuery`. Both run the same executor and the same value parser,
so a pasted URL and an API call can never disagree about what a query means —
which is the only reason the GET alias is safe to offer at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.search import query_builder
from app.services.search.query_builder import (
    Filter,
    FilterError,
    Mode,
    SearchQuery,
    UnknownTemplate,
)

router = APIRouter(prefix="/api/search", tags=["search"])


class FilterIn(BaseModel):
    template: str = Field(description="`parameter_template.name`, e.g. 'capacitance'")
    value: str = Field(
        description=(
            "Interpreted according to the template. Numeric templates accept the "
            "full shorthand grammar ('4k7', '20-30uF', '>=50V'); enum templates "
            "accept a choice key or any alias, comma-separated for OR."
        )
    )


class SearchRequest(BaseModel):
    filters: list[FilterIn] = Field(default_factory=list)
    text: str | None = None
    category: str | None = Field(default=None, description="Category slug; includes descendants")
    part_kind: str | None = None
    in_stock_only: bool = False
    include_stubs: bool = True
    mode: Mode = Field(
        default="search",
        description=(
            "'search' matches a requirement; 'substitute' finds parts that would "
            "satisfy it, using each template's substitution_direction."
        ),
    )
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class PartSummary(BaseModel):
    id: int
    name: str
    mpn: str | None
    description: str | None
    is_stub: bool
    category_id: int | None

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    #: Total matches ignoring pagination, so the UI can show "showing 50 of 213".
    total: int
    results: list[PartSummary]


def _to_query(request: SearchRequest) -> SearchQuery:
    return SearchQuery(
        filters=tuple(Filter(f.template, f.value) for f in request.filters),
        text=request.text,
        category_slug=request.category,
        part_kind_slug=request.part_kind,
        in_stock_only=request.in_stock_only,
        include_stubs=request.include_stubs,
        mode=request.mode,
        limit=request.limit,
        offset=request.offset,
    )


def _run(db: Session, request: SearchRequest) -> SearchResponse:
    query = _to_query(request)
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

    return SearchResponse(
        total=total, results=[PartSummary.model_validate(part) for part in results]
    )


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
    offset: int = Query(default=0, ge=0),
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
