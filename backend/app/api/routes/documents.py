"""`/api/documents` — upload, stream, and attach a stored file to a part.

Four routes, and every one of them is deliberately dull. The API's whole job here
is storage and service: per ADR 0005 it **never parses a PDF**, so nothing in this
module opens the bytes it stores beyond the magic-byte check in
`app.services.blobstore`. Page counts and extracted text arrive later from a
worker that is allowed to be absent.

## The path deviates from `docs/PLAN.md`, on purpose

`PLAN.md` writes the read route as `GET /api/datasheets/{sha256}`. It is
`/api/documents/{sha256}` here, because `PLAN.md`'s own `count_sessions` sketch
points `image_document_id` at the same table: serving a tray photograph from a URL
that says "datasheets" would be a lie one phase from now, and two routes for one
resource is worse. Everything else about the route is as specified — addressed by
sha256, streamed **inline** so the browser's native PDF viewer opens it rather
than downloading it, and `GET /api/parts/{id}/datasheet` redirects to the primary.

## No `client_op_id` anywhere in this module

Every other write route takes one (`app.api.idempotency`), and the omission is
the point: **the content address already is the idempotency key.** A retried
upload of the same bytes lands on the same `documents` row by construction, which
is exactly what a stored-and-replayed response would buy, and it works across
devices and across restarts rather than only for one recognised retry. Attach is
idempotent for the same structural reason — `uq_document_links_binding` makes a
repeat attach an update of the row it would have duplicated.

The upload route also cannot carry one: its body is the raw file, so there is
nowhere to put a key that `idempotency.run` could digest, and moving the file into
a JSON envelope to make room would mean base64 for every datasheet.

## `multipart/form-data` is not used

An `UploadFile` would need `python-multipart`, a dependency the default API
install does not have and (per ADR 0005's sizing argument) does not want. The body
is the file's bytes and the metadata rides in the query string, which costs
nothing, needs no dependency, and is trivial for a `fetch()` in the PWA.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.db.session import get_db
from app.models.catalog import Part
from app.models.documents import SHA256_LENGTH, Document, DocumentLink
from app.models.enums import DocumentKind, DocumentRole, EntityType
from app.services import blobstore, documents
from app.services.blobstore import BlobError

router = APIRouter(prefix="/api/documents", tags=["documents"])

#: A part's documents live under `/api/parts`, on its own router in this module
#: rather than in `app.api.routes.parts`, for the same reason the provisioning
#: walk's location routes do: everything they return belongs to the document
#: store, not to the part.
parts_router = APIRouter(prefix="/api/parts", tags=["documents"])


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


#: `BlobError.reason` -> status. Every refusal in the store is a statement about
#: the request, so all of these are 4xx; `missing_blob` and `path_escape` are the
#: exceptions and are handled where they arise, because a row pointing at a file
#: that is gone is our fault and must not be reported as the client's.
_REASON_STATUS = {
    "empty_document": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "document_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "unsupported_media_type": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "content_mismatch": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_sha256": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "hash_mismatch": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _blob_error(error: BlobError) -> HTTPException:
    return HTTPException(
        _REASON_STATUS.get(error.reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
        detail={"reason": error.reason, "message": str(error)},
    )


def _require_part(db: Session, part_id: RowId) -> Part:
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_part", "message": f"no part with id {part_id}"},
        )
    return part


def _require_document(db: Session, sha256: str) -> Document:
    try:
        document = documents.by_sha256(db, sha256)
    except BlobError as error:
        raise _blob_error(error) from error
    if document is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_document", "message": f"no document with sha256 {sha256}"},
        )
    return document


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class DocumentRead(BaseModel):
    id: int
    #: The address, and the only identifier a client needs: `url` is built from it
    #: and a re-upload of the same bytes returns the same value.
    sha256: str
    kind: str
    media_type: str
    byte_size: int
    #: NULL until an extraction run fills it in. The API cannot derive it — see
    #: `docs/adr/0005-extraction-runs-outside-the-api.md` — so a client must treat
    #: "unknown" as normal rather than as a broken document.
    page_count: int | None
    source_url: str | None
    original_filename: str | None
    created_at: datetime
    #: Ready to put in an `<iframe>` or an `<a href>`. Relative on purpose: the
    #: PWA, a LAN host and a reverse proxy must all resolve it against whatever
    #: origin served the page, and `ALMAGEST_BASE_URL` is for the things that get
    #: physically printed onto tags.
    url: str


class DocumentLinkRead(BaseModel):
    role: str
    #: Exactly one link per (part, role) has this set — maintained in
    #: `app.services.documents`, not by a constraint.
    is_primary: bool
    created_at: datetime
    document: DocumentRead


class DocumentUploadResult(BaseModel):
    document: DocumentRead
    #: False when this content was already stored. Then `document` is the row that
    #: already existed and **nothing about it was modified** — not its kind, not
    #: its source URL. Re-uploading a family datasheet for the twelfth part in the
    #: family is this case, and it is the normal one.
    created: bool
    #: False when the bytes had to be written to disk. Can be True while `created`
    #: is also True: an upload interrupted between the write and the commit leaves
    #: the blob behind, and the next attempt adopts it.
    deduplicated: bool
    #: Set when `part_id` was given, so uploading a datasheet and attaching it to
    #: the part is one request.
    link: DocumentLinkRead | None = None


class DocumentAttachRequest(BaseModel):
    #: An already-stored document. Attaching by address rather than by row id is
    #: what makes "this family sheet, for this part too" a single call from a
    #: client that only ever saw the hash.
    sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    role: DocumentRole = DocumentRole.DATASHEET
    is_primary: bool = Field(
        default=True,
        description=(
            "Make this the link the role resolves to. Ignored when it would leave "
            "the role with no primary: the first document attached in a role is "
            "always primary."
        ),
    )


class DocumentAttachResult(BaseModel):
    link: DocumentLinkRead
    #: False when the link already existed, in which case the request may still
    #: have promoted it to primary.
    created: bool


class DocumentDetachResult(BaseModel):
    #: How many links were removed — every role this document held for this part.
    detached: int
    #: What was promoted in place of a removed primary, one per affected role. The
    #: invariant is repaired before the response is written, so a client never has
    #: to re-read to find out whether `/datasheet` still resolves.
    promoted: list[DocumentLinkRead]


class DocumentLinkList(BaseModel):
    part_id: int
    links: list[DocumentLinkRead]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _document_read(document: Document) -> DocumentRead:
    return DocumentRead(
        id=document.id,
        sha256=document.sha256,
        kind=document.kind,
        media_type=document.media_type,
        byte_size=document.byte_size,
        page_count=document.page_count,
        source_url=document.source_url,
        original_filename=document.original_filename,
        created_at=document.created_at,
        url=document_url(document.sha256),
    )


def _document_of(db: Session, link: DocumentLink) -> Document:
    """The document a link points at. `db.get` rather than a relationship: the
    models here declare no ORM relationships (nothing else in this schema does
    either), so the fetch is explicit and cannot lazy-load N times."""
    document = db.get(Document, link.document_id)
    if document is None:  # pragma: no cover - CASCADE makes this unreachable
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"reason": "dangling_link", "message": f"link {link.id} has no document"},
        )
    return document


def _link_read(link: DocumentLink, document: Document) -> DocumentLinkRead:
    return DocumentLinkRead(
        role=link.role,
        is_primary=link.is_primary,
        created_at=link.created_at,
        document=_document_read(document),
    )


def document_url(sha256: str) -> str:
    return f"{router.prefix}/{sha256}"


#: Everything outside this is replaced in a `Content-Disposition` filename.
#: `original_filename` is client-controlled text, and a header value containing
#: CR/LF is header injection while one containing a quote breaks the parameter
#: it sits in. An allowlist rather than an escape: the field is a convenience for
#: the person clicking "save as", so mangling an unusual name costs nothing and
#: guessing at correct quoting costs a vulnerability.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def content_disposition(document: Document) -> str:
    """`inline`, so the browser's own PDF viewer opens it — `docs/PLAN.md`'s
    stated requirement, and the difference between one tap and a download.

    The fallback suffix is read off `storage_path`, not re-derived from
    `media_type`: the stored path is what is actually on disk, so this keeps
    working for a row whose media type has since been dropped from
    `blobstore.MEDIA_TYPES` — a served document must not start failing because the
    *accepted* set narrowed.
    """
    suffix = PurePosixPath(document.storage_path).suffix
    raw = document.original_filename or f"{document.sha256[:12]}{suffix}"
    safe = _UNSAFE_FILENAME_CHARS.sub("_", raw)[:120].lstrip(".") or f"document{suffix}"
    return f'inline; filename="{safe}"'


#: `type/subtype` optionally followed by `; key=value` parameters, in RFC 9110's
#: token characters and nothing else. Notably no CR, no LF and no non-latin-1.
_MEDIA_TYPE_RE = re.compile(
    r"\A[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+"
    r"(?:\s*;\s*[A-Za-z0-9!#$%&'*+.^_`|~-]+=[A-Za-z0-9!#$%&'*+.^_`|~-]+)*\Z"
)


def _response_media_type(document: Document) -> str:
    """The stored media type, or a safe stand-in if the row cannot be a header.

    `app.services.blobstore.canonical_media_type` means every row written by this
    API already holds a bare registered token, so this cannot fire today — it is
    here for the same reason `blob_path` re-checks containment: the value is
    *stored* data reaching a *header*, and one row written by a migration, a
    restore or a hand-edit must not be able to make its own read route
    unserviceable. Starlette encodes header values as latin-1, so a stray CR/LF is
    header injection and a non-latin-1 character is a 500.

    A fallback rather than a refusal, because the bytes are still perfectly
    serveable: `application/octet-stream` downloads instead of opening inline,
    which is a degraded document rather than an unreachable one. Deliberately not
    re-derived from `MEDIA_TYPES` — a served document must not start failing
    because the *accepted* set narrowed, which is the same reasoning
    `content_disposition` reads its suffix off `storage_path`.
    """
    if _MEDIA_TYPE_RE.match(document.media_type):
        return document.media_type
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=DocumentUploadResult)
def upload_document(
    data: Annotated[bytes, Body(media_type="application/octet-stream")],
    media_type: Annotated[
        str,
        Query(
            max_length=128,
            description=(
                "IANA media type of the body: `application/pdf`, `image/png`, `image/jpeg`."
            ),
        ),
    ],
    db: Session = Depends(get_db),
    kind: DocumentKind = DocumentKind.DATASHEET,
    sha256: Annotated[
        str | None,
        Query(
            max_length=SHA256_LENGTH,
            description=(
                "Optional. The digest the client believes it is sending. Checked "
                "against the bytes and refused on mismatch; never trusted in place "
                "of hashing them."
            ),
        ),
    ] = None,
    source_url: Annotated[str | None, Query(max_length=2048)] = None,
    filename: Annotated[str | None, Query(max_length=255)] = None,
    part_id: Annotated[
        RowId | None,
        Query(description="Attach to this part in the same request."),
    ] = None,
    role: DocumentRole = DocumentRole.DATASHEET,
    is_primary: bool = True,
) -> DocumentUploadResult:
    """Store a file under the sha256 of its bytes, and optionally attach it.

    **200, not 201, always.** A content-addressed store cannot promise it created
    anything — the honest answer to "did this upload produce a new document" is in
    `created`, and encoding it in the status code instead would mean one of the two
    outcomes going undeclared in the OpenAPI document that every client is
    generated from.
    """
    part = _require_part(db, part_id) if part_id is not None else None

    try:
        stored = documents.store_document(
            db,
            data=data,
            media_type=media_type,
            kind=kind,
            source_url=source_url,
            original_filename=filename,
            claimed_sha256=sha256,
        )
    except BlobError as error:
        raise _blob_error(error) from error

    link_read: DocumentLinkRead | None = None
    if part is not None:
        link, _ = documents.attach(
            db,
            document=stored.document,
            entity_type=EntityType.PART,
            entity_pk=part.id,
            role=role,
            is_primary=is_primary,
        )
        link_read = _link_read(link, stored.document)

    result = DocumentUploadResult(
        document=_document_read(stored.document),
        created=stored.created,
        deduplicated=stored.deduplicated,
        link=link_read,
    )
    db.commit()
    return result


@router.get(
    "/{sha256}",
    response_class=FileResponse,
    # Declared explicitly because FastAPI cannot infer a media type from
    # `FileResponse`, and the generated clients are built from this document: left
    # blank the 200 has no content at all, so a generated client would expect JSON
    # from a route that streams a PDF.
    responses={
        status.HTTP_200_OK: {
            "description": "The document itself, served inline.",
            "content": {media_type: {} for media_type in blobstore.MEDIA_TYPES},
        }
    },
)
def read_document(sha256: str, db: Session = Depends(get_db)) -> FileResponse:
    """Stream one document inline, by address.

    `sha256` is annotated as a plain `str` and validated in
    `app.services.blobstore.validate_sha256` rather than by a Pydantic `pattern`,
    so the refusal carries this module's own `reason` vocabulary and — more to the
    point — so the test for it exercises the defence that actually protects the
    filesystem instead of a duplicate of it in the validation layer.
    """
    document = _require_document(db, sha256)
    try:
        path = blobstore.path_for(document.storage_path)
    except BlobError as error:  # pragma: no cover - unreachable via a stored row
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"reason": error.reason, "message": str(error)},
        ) from error
    if not path.is_file():
        # Our fault, and loud on purpose: this is the state the blob-before-row
        # write order in `app.services.documents` exists to make impossible, so it
        # means the volume lost data rather than that the client asked for
        # anything wrong.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "reason": "missing_blob",
                "message": f"document {document.sha256} has a row but no stored file",
            },
        )
    return FileResponse(
        path,
        media_type=_response_media_type(document),
        headers={
            "Content-Disposition": content_disposition(document),
            # Safe to cache forever *because* the URL is a content address: the
            # bytes at this path cannot change without the path changing, so the
            # usual staleness risk does not exist here. This is what makes a family
            # datasheet one download for all twelve parts that share it, and what
            # keeps a phone from re-fetching 4 MB every time someone taps a QR at
            # the shelf.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@parts_router.get("/{part_id}/documents", response_model=DocumentLinkList)
def read_part_documents(part_id: RowId, db: Session = Depends(get_db)) -> DocumentLinkList:
    """Every document attached to a part, primary first within each role."""
    part = _require_part(db, part_id)
    return DocumentLinkList(
        part_id=part.id,
        links=[
            _link_read(link, document)
            for link, document in documents.links_for(
                db, entity_type=EntityType.PART, entity_pk=part.id
            )
        ],
    )


@parts_router.post("/{part_id}/documents", response_model=DocumentAttachResult)
def attach_part_document(
    part_id: RowId, request: DocumentAttachRequest, db: Session = Depends(get_db)
) -> DocumentAttachResult:
    """Attach an already-stored document to a part, or promote its existing link.

    An upsert, so this is also the "make this the primary datasheet" operation and
    there is no second route for it. One family PDF covering twelve MPNs is
    twelve calls here and one blob on disk.
    """
    part = _require_part(db, part_id)
    document = _require_document(db, request.sha256)
    link, created = documents.attach(
        db,
        document=document,
        entity_type=EntityType.PART,
        entity_pk=part.id,
        role=request.role,
        is_primary=request.is_primary,
    )
    result = DocumentAttachResult(link=_link_read(link, document), created=created)
    db.commit()
    return result


@parts_router.delete("/{part_id}/documents/{sha256}", response_model=DocumentDetachResult)
def detach_part_document(
    part_id: RowId, sha256: str, db: Session = Depends(get_db)
) -> DocumentDetachResult:
    """Unlink a document from a part. Neither the row nor the blob is deleted.

    A part dropping its link says nothing about whether the file is still wanted —
    one sheet serves a whole family — so reclaiming unreferenced blobs is a sweep
    somebody runs, never a side effect of a click. 404 when there was no link, so
    a client that thinks it removed something is right.
    """
    part = _require_part(db, part_id)
    document = _require_document(db, sha256)
    detachment = documents.detach(
        db, document=document, entity_type=EntityType.PART, entity_pk=part.id
    )
    if detachment.removed == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_link",
                "message": f"document {document.sha256} is not attached to part {part.id}",
            },
        )
    result = DocumentDetachResult(
        detached=detachment.removed,
        promoted=[_link_read(link, _document_of(db, link)) for link in detachment.promoted],
    )
    db.commit()
    return result


@parts_router.get(
    "/{part_id}/datasheet",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
)
def read_part_datasheet(part_id: RowId, db: Session = Depends(get_db)) -> RedirectResponse:
    """Redirect to the part's primary datasheet — `docs/PLAN.md`'s route.

    A redirect rather than a proxy, so the PDF is served from its own cacheable,
    content-addressed URL and two parts sharing a family sheet share the cache
    entry. 307 rather than 301/308 because which document this resolves to is
    **mutable** — attaching a better datasheet re-points it — and a permanent
    redirect is cached by browsers essentially forever. 307 over 302 for
    method preservation, which costs nothing here and is the correct default;
    nothing in this app answers `HEAD` (an app-wide 405), so this says nothing
    about that.

    404 when the part has no datasheet, which is an ordinary state: a part created
    from a scan in one tap has none, and that is the whole intake design.
    """
    part = _require_part(db, part_id)
    link = documents.primary_link(
        db, entity_type=EntityType.PART, entity_pk=part.id, role=DocumentRole.DATASHEET
    )
    if link is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "no_datasheet",
                "message": f"part {part.id} has no primary datasheet",
            },
        )
    document = _document_of(db, link)
    return RedirectResponse(
        document_url(document.sha256), status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
