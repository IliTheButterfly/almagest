"""`/api/location-tags` — reading a tag back, reporting what a write achieved, and
forgetting a binding.

Three routes, all outside any walk, because each is needed when a walk has gone
wrong: `resolve` is what a bench-station or phone tap calls to ask "what is
this?", `write-result` is the second half of "write the NDEF URI → read back to
verify" and the only way the server learns a write failed, and `unbind` is one of
the two repairs a verification mismatch leaves to a human.

**Resolution is NDEF-first with a UID fallback.** The NDEF URI record is the
payload this system authored (`{base_url}/s/{short_id}`), so it is what a tag
*means*; the UID is whatever the factory burned in, and it is recorded as well
because a write that fails partway can leave a tag with an intact UID and a
damaged NDEF record — the UID lives in factory-locked pages 0-2, physically
separate from user memory at page 4, so the worst case of writing is degrading
to a UID-only tag rather than losing the tag.

When the two disagree, both are reported. A tag written for one slot and bound
to another is exactly the condition the verification walk exists to find, and
silently preferring either answer would hide it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import RowId
from app.api.routes.provisioning import TagRead, tag_read
from app.api.schemas import ReplayableResponse
from app.db.session import get_db
from app.models.enums import NdefState
from app.models.storage import LocationTag
from app.services import provisioning
from app.services.provisioning import ProvisioningError

router = APIRouter(prefix="/api/location-tags", tags=["provisioning"])


class ResolvedLocationRead(BaseModel):
    location_id: RowId
    name: str
    slot_label: str | None
    #: Always derived, never read off the tag: a container that moves would make
    #: an encoded path a lie the moment the drawer changed cabinet.
    label_path: str
    short_id: str | None


class TagResolveRequest(BaseModel):
    """Either carrier, or both. Both is the useful case — it is the only way the
    server can tell that a tag's payload and its binding disagree."""

    tag_uid: str | None = Field(default=None, min_length=4, max_length=64)
    ndef_url: str | None = Field(
        default=None,
        max_length=2048,
        description="The URI record read off the tag, verbatim. Matched host-agnostically: "
        "a tag written before a hostname change is still perfectly correct.",
    )

    @model_validator(mode="after")
    def _needs_one_carrier(self) -> TagResolveRequest:
        if not self.tag_uid and not self.ndef_url:
            raise ValueError("give tag_uid, ndef_url, or both")
        return self


class TagResolveResponse(BaseModel):
    """`matched_by` is `ndef`, `uid`, or `none`.

    `status: unknown` is not an error — a blank tag is the normal state of a tag
    before it is provisioned, and the UI's answer is "provision this container
    now" rather than a dead end.
    """

    status: str
    matched_by: str
    location: ResolvedLocationRead | None
    tag: TagRead | None
    #: True when the NDEF payload names one slot and the UID is bound to
    #: another. Surfaced rather than resolved: that is a mis-bound tag, and only
    #: a human standing at the drawers can say which of the two is right.
    disagreement: bool


class WriteResultRequest(BaseModel):
    """What the device found on the tag after trying to write it.

    Deliberately the read-back URI rather than a boolean the client computed: the
    comparison is host-agnostic (a tag written before a hostname change is still
    correct), and that rule belongs in one place on the server rather than in
    every client that ever writes a tag. `read_back_url: null` covers both "the
    write threw" and "the read-back found no URI record", which are the same fact
    about the tag.
    """

    read_back_url: str | None = Field(
        default=None,
        max_length=2048,
        description="The URI record read back off the tag immediately after writing, "
        "verbatim. Null when the write failed or user memory came back empty.",
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class WriteResultResponse(ReplayableResponse):
    tag: TagRead
    #: True when the tag now carries a readable URI matching its binding — the
    #: only state in which tapping it with a phone opens anything.
    verified: bool


class UnbindRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        max_length=255,
        description="Free text for the operator's own record, e.g. 'drawer retired'.",
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class UnbindResponse(ReplayableResponse):
    #: A snapshot of what was removed. The row is gone by the time this is read.
    unbound: TagRead


@router.post("/resolve", response_model=TagResolveResponse)
def resolve_location_tag(
    request: TagResolveRequest, db: Session = Depends(get_db)
) -> TagResolveResponse:
    """What container is this tag stuck to?

    A pure read despite being a POST: an NDEF URI does not belong in a path
    segment, and a UID read off a tag is data rather than an identifier the
    client should be constructing URLs from.
    """
    try:
        resolution = provisioning.resolve_tag(
            db, tag_uid=request.tag_uid, ndef_url=request.ndef_url
        )
    except ProvisioningError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": error.reason, "message": str(error)},
        ) from error

    location = resolution.location
    return TagResolveResponse(
        status=resolution.status,
        matched_by=resolution.matched_by,
        location=(
            None
            if location is None
            else ResolvedLocationRead(
                location_id=location.id,
                name=location.name,
                slot_label=location.slot_label,
                label_path=location.label_path,
                short_id=provisioning.printed_short_id(db, location.id),
            )
        ),
        tag=tag_read(db, resolution.tag),
        disagreement=resolution.disagreement,
    )


def _require_tag(db: Session, tag_id: RowId) -> LocationTag:
    tag = db.get(LocationTag, tag_id)
    if tag is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_tag", "message": f"no location tag with id {tag_id}"},
        )
    return tag


@router.post("/{tag_id}/write-result", response_model=WriteResultResponse)
def record_tag_write_result(
    tag_id: RowId, request: WriteResultRequest, db: Session = Depends(get_db)
) -> WriteResultResponse:
    """Report what the tag holds after a write attempt.

    The one fact about a tag the server cannot observe. `bind` records the
    binding and stamps `written_at` at that moment, which is necessarily *before*
    any device has tried to write — so a binding whose write silently failed would
    otherwise look identical to one that worked, and PLAN.md's "write the NDEF URI
    → read back to verify" would have nowhere to report the second half.

    A failed write is never a failed bind. The UID lives in factory-locked pages
    0-2, so the tag still identifies itself at the station and only a phone tap is
    lost; the binding stays and the walk offers a rewrite.

    Idempotent by `client_op_id`, like every other write here — a phone that
    loses wifi mid-report retries the same key rather than second-guessing which
    of two states it left the record in.
    """
    tag = _require_tag(db, tag_id)

    def work() -> WriteResultResponse:
        updated = provisioning.record_write_result(db, tag, read_back_url=request.read_back_url)
        snapshot = tag_read(db, updated)
        if snapshot is None:  # pragma: no cover - `updated` is not None here
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason": "unknown_tag"})
        return WriteResultResponse(tag=snapshot, verified=updated.ndef_state == NdefState.VERIFIED)

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/location-tags/{id}/write-result",
        payload=request,
        response_model=WriteResultResponse,
        work=work,
    )


@router.post("/{tag_id}/unbind", response_model=UnbindResponse)
def unbind_location_tag(
    tag_id: RowId, request: UnbindRequest, db: Session = Depends(get_db)
) -> UnbindResponse:
    """Forget a binding, leaving the tag itself resolvable.

    Only the location-to-tag link is removed; the tag's payload is an opaque
    short id, so `/s/{short_id}` still resolves afterwards. That asymmetry is
    the point of "a tag is a foreign key, not a record" — the server can drop a
    binding it holds, and can never rewrite a tag it does not physically hold.

    Provisioning then treats the slot as untagged, so it re-enters the cursor of
    the next walk. This is also one of the two repairs a verification mismatch
    hands to a human, the other being a swap.
    """
    tag = _require_tag(db, tag_id)

    def work() -> UnbindResponse:
        snapshot = tag_read(db, tag)
        if snapshot is None:  # pragma: no cover - `tag` is not None here
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason": "unknown_tag"})
        provisioning.unbind(db, tag)
        return UnbindResponse(unbound=snapshot)

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/location-tags/{id}/unbind",
        payload=request,
        response_model=UnbindResponse,
        work=work,
    )
