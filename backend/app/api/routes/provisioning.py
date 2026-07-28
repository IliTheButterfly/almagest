"""Tag provisioning and the verification walk — the two walks along a cabinet.

Both are one screen doing one thing at a time, and both are shaped by the same
target: **2-3 seconds per drawer**, a 44-drawer cabinet in under two minutes,
walking-paced rather than software-paced. That is why every response here
carries the *whole* next step — the advanced cursor, the progress counters, the
NDEF payload just written, the undo label — rather than a bare acknowledgement
the client has to follow with a second request. A round trip per drawer at
phone-on-wifi latency is the difference between walking and waiting.

Three things in this module are deliberate and would be wrong reversed:

* **The cursor is a derivation, not a field.** Nothing here stores or accepts a
  position. See `app.services.provisioning`.
* **"Already bound elsewhere" is a 200, not a 409.** It is an ordinary branch of
  the flow with a two-button answer (Move here / Cancel), and the response has to
  carry the conflicting binding's `label_path` so the modal can name it. An error
  envelope would make the normal path of a walk read as a failure, and every
  generated client would have to parse it out of an exception.
* **A verification mismatch changes nothing at all.** It is recorded with the
  reverse lookup of where the scanned tag actually belongs, and the cursor stays
  put. Auto-fixing would be choosing between rebinding this drawer and swapping
  two drawers, which are different physical claims about what happened.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import RowId
from app.api.schemas import ReplayableResponse
from app.db.session import get_db
from app.models.enums import ProvisioningDevice, ProvisioningKind
from app.models.layout_authoring import ProvisioningSession, VerificationMismatch
from app.models.storage import Location, LocationTag
from app.services import provisioning
from app.services.provisioning import ProvisioningError

router = APIRouter(prefix="/api/provisioning-sessions", tags=["provisioning"])
verification_router = APIRouter(prefix="/api/verification-sessions", tags=["provisioning"])
#: The two "start a walk here" routes are location-scoped, so they hang off
#: `/api/locations` even though everything they return belongs to this module.
locations_router = APIRouter(prefix="/api/locations", tags=["provisioning"])


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------

#: Generous relative to a 7-byte UID rendered as 14 hex digits: the field is
#: normalised in the service (case folded, separators dropped), and a reader that
#: presents `04:1A:2B:3C:4D:5E:6F` must not be rejected for its colons.
TagUidField = Field(min_length=4, max_length=64)


class SlotCursorRead(BaseModel):
    """Where the walk is now. **Derived on every request** — `MIN(sort_order)`
    among the cabinet's children that still need this walk's attention."""

    location_id: RowId
    slot_label: str | None
    name: str
    label_path: str
    row_idx: int | None
    col_idx: int | None
    sort_order: int
    #: Null until the slot is provisioned: a generated grid cell has no printed
    #: identity until something needs one. Binding mints it.
    short_id: str | None
    has_tag: bool


class TagRead(BaseModel):
    id: RowId
    location_id: RowId
    label_path: str
    tag_uid: str | None
    #: `{base_url}/s/{short_id}` and nothing else. The client writes exactly
    #: this string to the tag's NDEF URI record — the same string a printed QR
    #: carries.
    ndef_url: str
    bind_source: str | None
    is_read_only: bool
    written_at: datetime
    last_verified_at: datetime | None


class ConflictRead(BaseModel):
    """The binding standing in the way, named well enough for the modal:
    "Already bound to {label_path}" with Move here / Cancel."""

    tag_id: RowId
    tag_uid: str | None
    location_id: RowId
    slot_label: str | None
    label_path: str


class ProvisionProgressRead(BaseModel):
    #: Cabinet-wide, not session-wide: `bound` counts every drawer that has a
    #: tag, however it got one, because that is the number the person standing
    #: at the cabinet can check against the drawers.
    total_slots: int
    bound: int
    unbound: int
    #: Session-scoped. A later walk offers a skipped drawer again — "left empty
    #: for now" is not "never".
    skipped: int
    is_complete: bool


class SessionRead(BaseModel):
    id: RowId
    root_location_id: RowId
    kind: str
    device_kind: str | None
    started_at: datetime
    completed_at: datetime | None
    bound_count: int
    skipped_count: int
    note: str | None


class ProvisioningState(BaseModel):
    """Everything the provisioning screen needs, in one shape.

    Returned by every route in the provisioning flow so a bind, a skip and an
    undo all leave the client fully up to date with no follow-up read.
    """

    session: SessionRead | None
    cursor: SlotCursorRead | None
    progress: ProvisionProgressRead
    #: How many more times Undo will do something. Capped at five by design.
    undo_depth: int
    #: The slot label the floating Undo button should show, e.g. "B2".
    undo_label: str | None


class StartSessionRequest(BaseModel):
    device_kind: ProvisioningDevice | None = Field(
        default=None,
        description=(
            "Web NFC on a phone is primary — you are standing at the cabinet anyway; "
            "the station PN532 is the fallback and the only path on iOS."
        ),
    )
    note: str | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class SessionStarted(ReplayableResponse):
    state: ProvisioningState


class BindRequest(BaseModel):
    tag_uid: str = TagUidField
    location_id: RowId | None = Field(
        default=None,
        description=(
            "Tapping any cell jumps the cursor there. Omit to bind at the derived "
            "cursor, which is the walking-paced path."
        ),
    )
    move: bool = Field(
        default=False,
        description=(
            "The human answered 'Move here' to a conflict. Required for anything that "
            "displaces an existing binding — a silent rebind is how a drawer full of "
            "stock ends up answering to a tag that used to mean something else."
        ),
    )
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class BindResponse(ReplayableResponse):
    """`status` is one of:

    * `bound` — a slot that had no tag now has one, and the cursor advanced;
    * `moved` / `rebound` — a confirmed `move` displaced a prior binding, which
      the five-deep undo can put back;
    * `already_bound_here` — the same tag tapped twice, or the client-side
      debounce lost a race. Nothing written, and no extra undo step to unwind;
    * `already_bound_elsewhere` / `slot_already_bound` — **nothing was written.**
      `conflict` names the binding in the way so the UI can offer Move here /
      Cancel.
    """

    status: str
    tag: TagRead | None
    conflict: ConflictRead | None
    state: ProvisioningState


class SkipRequest(BaseModel):
    location_id: RowId | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class SkipResponse(ReplayableResponse):
    skipped: SlotCursorRead
    state: ProvisioningState


class ProvisioningUndoRequest(BaseModel):
    """Prefixed, not plain `UndoRequest` — `stock` already owns that name.

    Two route modules sharing a model name makes FastAPI fully qualify **both**
    in the OpenAPI document, so the collision silently renames the other
    module's schema and breaks generated client code for a route nobody touched.
    Pinned by `test_no_two_modules_share_a_response_model_name`.
    """

    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class UndoneRead(BaseModel):
    action_kind: str
    location_id: RowId
    slot_label: str | None
    label_path: str
    tag_uid: str | None


class ProvisioningUndoResponse(ReplayableResponse):
    undone: UndoneRead
    #: The binding put back, when the undone action was a move or a rebind.
    restored_tag: TagRead | None
    #: Set when a prior binding could not be restored because its slot has been
    #: re-tagged since. Reported rather than forced — overwriting a binding made
    #: after the fact would be a second silent rebind.
    not_restored_reason: str | None
    state: ProvisioningState


class MismatchRead(BaseModel):
    """A tag found somewhere it should not be. **Never auto-fixed.**"""

    id: RowId
    location_id: RowId
    slot_label: str | None
    label_path: str
    expected_tag_uid: str | None
    scanned_tag_uid: str
    #: The reverse lookup — which slot the scanned tag actually belongs to.
    #: "This tag belongs to B2" is actionable in a way "something is wrong" is
    #: not. Null means the tag is bound nowhere at all.
    scanned_resolved_location_id: int | None
    scanned_resolved_slot_label: str | None
    scanned_resolved_label_path: str | None
    created_at: datetime
    resolved_at: datetime | None


class VerifyProgressRead(BaseModel):
    total_tagged: int
    checked: int
    remaining: int
    mismatches: int


class VerificationState(BaseModel):
    session: SessionRead | None
    cursor: SlotCursorRead | None
    progress: VerifyProgressRead
    #: True while some drawer's tag is unexplained. The cursor is still on it,
    #: so the walk cannot step past by itself — which is the stop.
    stopped: bool
    mismatches: list[MismatchRead]


class VerificationStarted(ReplayableResponse):
    state: VerificationState


class CheckRequest(BaseModel):
    tag_uid: str = TagUidField
    location_id: RowId | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class CheckResponse(ReplayableResponse):
    """`status` is `match` (tick and advance) or `mismatch` (record and stop)."""

    status: str
    location_id: RowId
    expected_tag_uid: str | None
    scanned_tag_uid: str
    mismatch: MismatchRead | None
    state: VerificationState


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

#: A walk step can fail for reasons that are genuinely different kinds of thing:
#: a malformed UID is the request's fault, a completed session is a state
#: conflict, a missing slot is a lookup miss. Mapping them here keeps the
#: service layer free of HTTP.
_STATUS_BY_REASON = {
    "invalid_tag_uid": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "out_of_scope": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "unknown_location": status.HTTP_404_NOT_FOUND,
}


def _provisioning_error(error: ProvisioningError) -> HTTPException:
    return HTTPException(
        _STATUS_BY_REASON.get(error.reason, status.HTTP_409_CONFLICT),
        detail={"reason": error.reason, "message": str(error)},
    )


def _require_location(db: Session, location_id: RowId) -> Location:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_location", "message": f"no location with id {location_id}"},
        )
    return location


def _require_session(db: Session, session_id: RowId, kind: ProvisioningKind) -> ProvisioningSession:
    walk = db.get(ProvisioningSession, session_id)
    if walk is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_session", "message": f"no session with id {session_id}"},
        )
    if walk.kind != kind:
        # Two kinds with opposite postconditions share a table; hitting the
        # wrong route with a valid id must not silently do the other thing.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "wrong_session_kind",
                "message": f"session {walk.id} is a {walk.kind} walk, not a {kind} one",
            },
        )
    return walk


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _slot_read(db: Session, slot: Location) -> SlotCursorRead:
    return SlotCursorRead(
        location_id=slot.id,
        slot_label=slot.slot_label,
        name=slot.name,
        label_path=slot.label_path,
        row_idx=slot.row_idx,
        col_idx=slot.col_idx,
        sort_order=slot.sort_order,
        short_id=provisioning.printed_short_id(db, slot.id),
        has_tag=provisioning.tag_at(db, slot.id) is not None,
    )


def _cursor_read(db: Session, slot: Location | None) -> SlotCursorRead | None:
    """None means the walk has nothing left to do, which is a state and not an
    error — a fully-tagged cabinet reports `cursor: null, is_complete: true`."""
    return None if slot is None else _slot_read(db, slot)


def tag_read(db: Session, tag: LocationTag | None) -> TagRead | None:
    if tag is None:
        return None
    location = db.get(Location, tag.location_id)
    return TagRead(
        id=tag.id,
        location_id=tag.location_id,
        label_path=location.label_path if location is not None else "",
        tag_uid=tag.tag_uid,
        ndef_url=tag.ndef_url,
        bind_source=tag.bind_source,
        is_read_only=tag.is_read_only,
        written_at=tag.written_at,
        last_verified_at=tag.last_verified_at,
    )


def _session_read(walk: ProvisioningSession) -> SessionRead:
    return SessionRead(
        id=walk.id,
        root_location_id=walk.root_location_id,
        kind=walk.kind,
        device_kind=walk.device_kind,
        started_at=walk.started_at,
        completed_at=walk.completed_at,
        bound_count=walk.bound_count,
        skipped_count=walk.skipped_count,
        note=walk.note,
    )


def _provisioning_state(db: Session, walk: ProvisioningSession) -> ProvisioningState:
    progress = provisioning.provision_progress(db, walk)
    return ProvisioningState(
        session=_session_read(walk),
        cursor=_cursor_read(db, provisioning.next_slot(db, walk)),
        progress=ProvisionProgressRead(
            total_slots=progress.total_slots,
            bound=progress.bound,
            unbound=progress.unbound,
            skipped=progress.skipped,
            is_complete=progress.is_complete,
        ),
        undo_depth=len(provisioning.undoable_actions(db, walk)),
        undo_label=provisioning.undo_label(db, walk),
    )


def _idle_provisioning_state(db: Session, root: Location) -> ProvisioningState:
    """The cabinet's state with no walk open.

    Reported rather than 404'd because "how many of these 44 drawers are tagged"
    is exactly what the screen offering to start a walk needs to show, and the
    answer does not depend on a session existing — the cursor never did.
    """
    total = provisioning.child_count(db, root)
    bound = provisioning.tagged_child_count(db, root)
    return ProvisioningState(
        session=None,
        cursor=_cursor_read(db, provisioning.next_unbound_child(db, root)),
        progress=ProvisionProgressRead(
            total_slots=total,
            bound=bound,
            unbound=total - bound,
            skipped=0,
            is_complete=total == bound,
        ),
        undo_depth=0,
        undo_label=None,
    )


def _mismatch_read(db: Session, finding: VerificationMismatch) -> MismatchRead:
    location = db.get(Location, finding.location_id)
    belongs_to = (
        db.get(Location, finding.scanned_resolved_location_id)
        if finding.scanned_resolved_location_id is not None
        else None
    )
    return MismatchRead(
        id=finding.id,
        location_id=finding.location_id,
        slot_label=location.slot_label if location is not None else None,
        label_path=location.label_path if location is not None else "",
        expected_tag_uid=finding.expected_tag_uid,
        scanned_tag_uid=finding.scanned_tag_uid,
        scanned_resolved_location_id=finding.scanned_resolved_location_id,
        scanned_resolved_slot_label=belongs_to.slot_label if belongs_to is not None else None,
        scanned_resolved_label_path=belongs_to.label_path if belongs_to is not None else None,
        created_at=finding.created_at,
        resolved_at=finding.resolved_at,
    )


def _verification_state(db: Session, walk: ProvisioningSession) -> VerificationState:
    progress = provisioning.verify_progress(db, walk)
    return VerificationState(
        session=_session_read(walk),
        cursor=_cursor_read(db, provisioning.next_slot(db, walk)),
        progress=VerifyProgressRead(
            total_tagged=progress.total_tagged,
            checked=progress.checked,
            remaining=progress.remaining,
            mismatches=progress.mismatches,
        ),
        stopped=provisioning.is_stopped(db, walk),
        mismatches=[_mismatch_read(db, row) for row in provisioning.mismatches(db, walk)],
    )


# ---------------------------------------------------------------------------
# Starting a walk
# ---------------------------------------------------------------------------


@locations_router.post(
    "/{location_id}/provisioning-sessions",
    response_model=SessionStarted,
    status_code=status.HTTP_201_CREATED,
)
def start_provisioning_session(
    location_id: RowId, request: StartSessionRequest, db: Session = Depends(get_db)
) -> SessionStarted:
    """Begin (or resume) binding tags to this cabinet's drawers.

    An open walk on the same cabinet is handed back rather than duplicated:
    since the cursor is derived, a second session would report the identical
    position, and two "current" sessions is a state with no correct answer.
    """
    root = _require_location(db, location_id)

    def work() -> SessionStarted:
        walk = provisioning.open_session(
            db,
            root,
            kind=ProvisioningKind.PROVISION,
            device_kind=request.device_kind,
            note=request.note,
        )
        return SessionStarted(state=_provisioning_state(db, walk))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/locations/{id}/provisioning-sessions",
        payload=request,
        response_model=SessionStarted,
        work=work,
    )


@locations_router.get(
    "/{location_id}/provisioning-sessions/current", response_model=ProvisioningState
)
def read_current_provisioning_session(
    location_id: RowId, db: Session = Depends(get_db)
) -> ProvisioningState:
    """The open walk on this cabinet and where it is, or the cabinet's tag state
    with `session: null` if no walk is open.

    This is what makes resuming free: nothing was saved when the phone went into
    a pocket mid-cabinet, and nothing needs restoring — the cursor is recomputed
    here, so a drawer bound from somewhere else in the meantime is simply
    already bound.
    """
    root = _require_location(db, location_id)
    walk = provisioning.current_session(db, root, kind=ProvisioningKind.PROVISION)
    if walk is None:
        return _idle_provisioning_state(db, root)
    return _provisioning_state(db, walk)


@locations_router.post(
    "/{location_id}/verification-sessions",
    response_model=VerificationStarted,
    status_code=status.HTTP_201_CREATED,
)
def start_verification_session(
    location_id: RowId, request: StartSessionRequest, db: Session = Depends(get_db)
) -> VerificationStarted:
    """Begin (or resume) re-reading this cabinet's tags.

    **Not optional busywork.** No software can stop a person sticking a tag on
    the wrong drawer; it can only detect it — and a mis-bound tag is invisible
    until it causes a wrong put-away, at which point the stock is somewhere the
    system does not think it is.
    """
    root = _require_location(db, location_id)

    def work() -> VerificationStarted:
        walk = provisioning.open_session(
            db,
            root,
            kind=ProvisioningKind.VERIFY,
            device_kind=request.device_kind,
            note=request.note,
        )
        return VerificationStarted(state=_verification_state(db, walk))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/locations/{id}/verification-sessions",
        payload=request,
        response_model=VerificationStarted,
        work=work,
    )


# ---------------------------------------------------------------------------
# The provisioning walk
# ---------------------------------------------------------------------------


@router.post("/{session_id}/bind", response_model=BindResponse)
def bind_tag(
    session_id: RowId, request: BindRequest, db: Session = Depends(get_db)
) -> BindResponse:
    """Bind the tapped tag to the cursor slot, and advance.

    The response carries the written NDEF payload, the advanced cursor and the
    progress counters together, because the phone is still holding the tag
    against the drawer when this returns: writing the URI record and moving on
    both have to happen without another round trip.
    """
    walk = _require_session(db, session_id, ProvisioningKind.PROVISION)

    def work() -> BindResponse:
        try:
            outcome = provisioning.bind(
                db,
                walk,
                tag_uid=request.tag_uid,
                location_id=request.location_id,
                move=request.move,
            )
        except ProvisioningError as error:
            raise _provisioning_error(error) from error

        conflict: ConflictRead | None = None
        if outcome.conflict_tag is not None and outcome.conflict_location is not None:
            conflict = ConflictRead(
                tag_id=outcome.conflict_tag.id,
                tag_uid=outcome.conflict_tag.tag_uid,
                location_id=outcome.conflict_location.id,
                slot_label=outcome.conflict_location.slot_label,
                label_path=outcome.conflict_location.label_path,
            )
        return BindResponse(
            status=outcome.status,
            tag=tag_read(db, outcome.tag),
            conflict=conflict,
            state=_provisioning_state(db, walk),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/provisioning-sessions/{id}/bind",
        payload=request,
        response_model=BindResponse,
        work=work,
    )


@router.post("/{session_id}/skip", response_model=SkipResponse)
def skip_slot(
    session_id: RowId, request: SkipRequest, db: Session = Depends(get_db)
) -> SkipResponse:
    """Leave this slot untagged and advance past it for the rest of the walk."""
    walk = _require_session(db, session_id, ProvisioningKind.PROVISION)

    def work() -> SkipResponse:
        try:
            skipped = provisioning.skip(db, walk, location_id=request.location_id)
        except ProvisioningError as error:
            raise _provisioning_error(error) from error
        return SkipResponse(skipped=_slot_read(db, skipped), state=_provisioning_state(db, walk))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/provisioning-sessions/{id}/skip",
        payload=request,
        response_model=SkipResponse,
        work=work,
    )


@router.post("/{session_id}/undo", response_model=ProvisioningUndoResponse)
def undo_action(
    session_id: RowId, request: ProvisioningUndoRequest, db: Session = Depends(get_db)
) -> ProvisioningUndoResponse:
    """Reverse the last action, five deep.

    Undoing a Move puts the displaced binding back where it was — which is the
    reason the walk log keeps a copy of it, since the row itself is gone by
    then. Works on a completed walk too, reopening it: the bind that finished
    the cabinet is the one most likely to need taking back.
    """
    walk = _require_session(db, session_id, ProvisioningKind.PROVISION)

    def work() -> ProvisioningUndoResponse:
        try:
            outcome = provisioning.undo(db, walk)
        except ProvisioningError as error:
            raise _provisioning_error(error) from error
        return ProvisioningUndoResponse(
            undone=UndoneRead(
                action_kind=outcome.action_kind,
                location_id=outcome.location.id,
                slot_label=outcome.location.slot_label,
                label_path=outcome.location.label_path,
                tag_uid=outcome.tag_uid,
            ),
            restored_tag=tag_read(db, outcome.restored_tag),
            not_restored_reason=outcome.not_restored_reason,
            state=_provisioning_state(db, walk),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/provisioning-sessions/{id}/undo",
        payload=request,
        response_model=ProvisioningUndoResponse,
        work=work,
    )


# ---------------------------------------------------------------------------
# The verification walk
# ---------------------------------------------------------------------------


@verification_router.post("/{session_id}/check", response_model=CheckResponse)
def check_tag(
    session_id: RowId, request: CheckRequest, db: Session = Depends(get_db)
) -> CheckResponse:
    """Re-read one tag and compare it to the expected UID.

    Match: tick and advance. Mismatch: record `expected_tag_uid`,
    `scanned_tag_uid` and which slot the scanned tag actually belongs to, then
    stop — the cursor stays on this drawer. **No binding is changed on either
    branch.** The two plausible repairs (rebind here, or swap these two drawers)
    have different physical consequences and only the person holding the drawers
    knows which happened.
    """
    walk = _require_session(db, session_id, ProvisioningKind.VERIFY)

    def work() -> CheckResponse:
        try:
            outcome = provisioning.check(
                db, walk, tag_uid=request.tag_uid, location_id=request.location_id
            )
        except ProvisioningError as error:
            raise _provisioning_error(error) from error
        return CheckResponse(
            status=outcome.status,
            location_id=outcome.location.id,
            expected_tag_uid=outcome.expected_tag_uid,
            scanned_tag_uid=outcome.scanned_tag_uid,
            mismatch=(None if outcome.mismatch is None else _mismatch_read(db, outcome.mismatch)),
            state=_verification_state(db, walk),
        )

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="POST /api/verification-sessions/{id}/check",
        payload=request,
        response_model=CheckResponse,
        work=work,
    )


@verification_router.get("/{session_id}", response_model=VerificationState)
def read_verification_session(
    session_id: RowId, db: Session = Depends(get_db)
) -> VerificationState:
    """The walk's findings so far — the report a mis-tagged cabinet is fixed from."""
    return _verification_state(db, _require_session(db, session_id, ProvisioningKind.VERIFY))
