"""`/s/{short_id}` — the URL that is physically written into every tag.

This route is the entire reason NFC costs almost nothing to build. The NDEF
URI record on a tag and the payload of a printed QR are the *same string*:
`{base_url}/s/{short_id}`. There is no second payload format, no separate
resolver, and no app required — an iPhone tapping a tag opens this URL in
Safari, and Android's tap-to-open does the same.

Two consequences follow, and both are load-bearing:

* **The path must never change.** It is stamped into physical objects. A
  redirect could paper over a rename, but only for tags that reach a server
  that still has the redirect.
* **Nothing mutable is encoded in it.** Not a count, not a location path. A tag
  carries an opaque identifier and nothing else, so a bulk import or a
  reconciliation job — which cannot touch a tag it does not physically hold —
  can never make a tag stale while it still looks authoritative.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.db.session import get_db
from app.models.enums import EntityType
from app.models.identity import ObjectId
from app.services import shortid
from app.services.scanning.describe import describe
from app.services.shortid import InvalidShortId

router = APIRouter(tags=["resolve"])


class ResolvedTarget(BaseModel):
    short_id: str
    #: Grouped for display, `4K7T-92M8`, with the cosmetic type prefix.
    display: str
    entity_type: str
    entity_pk: RowId
    #: Human-readable identity of whatever was scanned.
    label: str
    #: Full derived path, for a location. Always freshly computed — never
    #: stored on the tag, because a container that moves would make an encoded
    #: path a lie the moment the drawer changed cabinet.
    label_path: str | None = None


class ResolveResponse(BaseModel):
    status: str
    target: ResolvedTarget | None = None
    #: Present when the code is well-formed but unknown, so the UI can offer
    #: "provision this container now" rather than a dead end.
    normalized: str | None = None


def _describe(db: Session, binding: ObjectId) -> ResolvedTarget:
    """Render the binding via the shared describer.

    `services.scanning.describe` is the single answer to "what do I call this
    row", shared with `/api/scan/resolve`. Two spellings of that answer would mean
    one drawer labelling itself differently depending on which endpoint found it.
    """
    entity = describe(db, binding.entity_type, binding.entity_pk, short_id=binding.short_id)
    return ResolvedTarget(
        short_id=binding.short_id,
        display=shortid.format_display(binding.short_id, binding.entity_type),
        entity_type=entity.entity_type,
        entity_pk=entity.entity_pk,
        label=entity.label,
        label_path=entity.label_path,
    )


@router.get("/api/resolve/{short_id}", response_model=ResolveResponse)
def resolve_short_id(short_id: str, db: Session = Depends(get_db)) -> ResolveResponse:
    """Machine-readable resolution, for the PWA and `deviceagent`."""
    try:
        canonical = shortid.validate(short_id)
    except InvalidShortId as error:
        # 404 rather than 400: from the caller's point of view "this code is
        # malformed" and "this code names nothing" are the same dead end, and
        # the reason is in the body for anyone who cares.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"status": "invalid", "reason": error.reason},
        ) from error

    binding = shortid.resolve(db, canonical)
    if binding is None:
        return ResolveResponse(status="unknown", normalized=canonical)

    return ResolveResponse(status="resolved", target=_describe(db, binding))


@router.get("/s/{short_id}", include_in_schema=False)
def open_short_id(short_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """The physical entry point. Redirects a tag tap into the PWA.

    Excluded from the OpenAPI schema on purpose: it is a human-facing landing
    URL, not part of the API contract the clients are generated from.

    A redirect rather than a rendered page, so the resolution rule lives in
    exactly one place and the PWA owns every piece of presentation.
    """
    try:
        canonical = shortid.validate(short_id)
    except InvalidShortId:
        return RedirectResponse(url=f"/scan?unknown={short_id}", status_code=302)

    binding = shortid.resolve(db, canonical)
    if binding is None:
        # A well-formed code that resolves to nothing is the *provisioning*
        # case — a blank tag, or one written before its row existed. The UI
        # offers to bind it rather than reporting an error.
        return RedirectResponse(url=f"/provision?code={canonical}", status_code=302)

    destination = {
        EntityType.LOCATION: f"/locations/{binding.entity_pk}",
        EntityType.PART: f"/parts/{binding.entity_pk}",
        EntityType.STOCK_LOT: f"/lots/{binding.entity_pk}",
    }.get(EntityType(binding.entity_type), f"/scan?code={canonical}")

    return RedirectResponse(url=destination, status_code=302)
