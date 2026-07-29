"""Bulk tag provisioning, and the verification walk that proves it worked.

**Apply every tag physically first, then walk the cabinet binding them.** That
ordering is the whole trick: you are confirming whatever tag is *already on that
drawer*, so there is no loose-tag hand-off where two units can be swapped.
Provisioning loose tags at the station carries exactly that risk and is for
pre-provisioning before the drawers exist — always followed by a verification
pass.

**The cursor is never stored.** It is always `MIN(sort_order)` among the
cabinet's children that lack a `location_tags` row, computed fresh on every
request. Resuming a half-finished cabinet is therefore free and immune to
anything bound out of band — a stored cursor goes wrong the first time someone
binds one drawer from their phone mid-session, and it would go wrong silently,
pointing at a drawer that already has a tag.

The one thing a `location_tags`-only cursor cannot recover is that a slot was
*skipped*, so skips are written to `provisioning_actions` and subtracted from
the same derivation. They are session-scoped on purpose: a later walk should
offer a skipped drawer again, because "left empty for now" is not "never".

**Nothing mutable ever goes on a tag.** The NDEF payload is
`{base_url}/s/{short_id}` and nothing else — no count, no fill state. A remote
mutation (bulk import, reconciliation job, BOM pick) cannot touch a tag it does
not physically hold, so a tag carrying state would go stale while still looking
authoritative. A tag is a foreign key, not a record.

**Verification is a separate session kind, and it never repairs anything.** It
re-reads every tag in order and compares to the expected UID. A mismatch records
the expected UID, the scanned UID, and the reverse lookup of which slot the
scanned tag actually belongs to — then stops. The two plausible repairs (rebind
here, or swap the two drawers) have different physical consequences, and only
the person holding the drawers knows which happened. No software can stop
someone tagging the wrong drawer; it can only detect it, and a mis-bound tag is
otherwise invisible until it causes a wrong put-away.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from idcodec.tagpayload import InvalidTagUid
from idcodec.tagpayload import normalize_tag_uid as _normalize_tag_uid

# `as` is what makes this a re-export under `mypy --strict`, which disables
# implicit re-export. The module has no `__all__` to add a name to.
from idcodec.tagpayload import parse_ndef_url as parse_ndef_url
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import (
    EntityType,
    ProvisioningActionKind,
    ProvisioningDevice,
    ProvisioningKind,
)
from app.models.layout_authoring import (
    ProvisioningAction,
    ProvisioningSession,
    VerificationMismatch,
)
from app.models.storage import Location, LocationTag
from app.models.types import utcnow
from app.services import shortid

#: How far back the always-visible Undo button reaches. Five is the design's
#: number; the depth is enforced against the *whole* action log rather than the
#: not-undone tail, so undoing five times empties the stack instead of walking
#: back through the session five at a time.
UNDO_DEPTH = 5


class ProvisioningError(ValueError):
    """A walk step that cannot mean what it says.

    Carries a `reason` the route maps to a status code, matching
    `app.services.layout_authoring.LayoutError`. Note what is *not* in here:
    "this tag is already bound elsewhere" is an ordinary branch of the flow with
    a two-button answer, not an error, so it comes back as a normal response.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Tag payloads: one payload, two carriers
#
# Both rules live in the dependency-free `idcodec.tagpayload`, because the
# station agent on the Pi folds a UID several times a second and must fold it
# *identically* — a UID folded by a different rule is invisible to the
# `location_tags` binding it should match while looking perfectly correct in both
# places. They are re-exported here so every existing call site is unchanged.
#
# `parse_ndef_url` is re-exported verbatim. `normalize_tag_uid` is wrapped rather
# than re-exported, for one reason only: it is the one of the two that raises,
# and four routes catch `ProvisioningError` to turn a bad UID into a 422. The
# codec cannot raise `ProvisioningError` — that class is defined above, in a
# module full of SQLAlchemy — so it raises `InvalidTagUid` and this translates,
# carrying the `reason` across rather than restating the string. The wire
# contract (`reason="invalid_tag_uid"`) is therefore unchanged and is pinned by
# `tests/unit/test_provisioning_payloads.py`.
# ---------------------------------------------------------------------------


def normalize_tag_uid(raw: str) -> str:
    """Canonicalise a UID to bare upper-case hex."""
    try:
        return _normalize_tag_uid(raw)
    except InvalidTagUid as error:
        raise ProvisioningError(str(error), reason=error.reason) from error


def printed_short_id(session: Session, location_id: int) -> str | None:
    """The short id already on this location's label, if it has one.

    Delegates rather than repeating the query: the tie-break between a
    superseded id and the current one is a rule, and two copies of a rule drift.
    """
    return shortid.primary_short_id(session, EntityType.LOCATION, location_id)


def ndef_url_for(session: Session, location: Location) -> str:
    """The payload to write to this slot's tag, minting a short id if needed.

    A generated grid cell starts with no printed identity — nobody sticks 96
    labels on an 8x12 box — so provisioning one is where it earns one. The
    payload is the same string a printed QR carries: one payload, two carriers.
    """
    short_id = printed_short_id(session, location.id) or shortid.allocate(
        session, EntityType.LOCATION, location.id
    )
    return f"{get_settings().base_url}/s/{short_id}"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def open_session(
    session: Session,
    root: Location,
    *,
    kind: ProvisioningKind,
    device_kind: ProvisioningDevice | None = None,
    note: str | None = None,
) -> ProvisioningSession:
    """The open walk of this kind on this cabinet, resuming one if it exists.

    Resuming is free precisely because there is no cursor to restore, so
    handing back the existing session is strictly better than starting a second
    one that would report the identical derived position.
    """
    existing = (
        session.execute(
            select(ProvisioningSession)
            .where(
                ProvisioningSession.root_location_id == root.id,
                ProvisioningSession.kind == kind,
                ProvisioningSession.completed_at.is_(None),
            )
            .order_by(ProvisioningSession.id.desc())
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    walk = ProvisioningSession(
        root_location_id=root.id,
        kind=kind,
        device_kind=device_kind,
        note=note,
    )
    session.add(walk)
    session.flush()
    return walk


def current_session(
    session: Session, root: Location, *, kind: ProvisioningKind
) -> ProvisioningSession | None:
    return (
        session.execute(
            select(ProvisioningSession)
            .where(
                ProvisioningSession.root_location_id == root.id,
                ProvisioningSession.kind == kind,
                ProvisioningSession.completed_at.is_(None),
            )
            .order_by(ProvisioningSession.id.desc())
        )
        .scalars()
        .first()
    )


def require_root(session: Session, walk: ProvisioningSession) -> Location:
    root = session.get(Location, walk.root_location_id)
    if root is None:  # pragma: no cover - FK ON DELETE CASCADE removes the walk
        raise ProvisioningError("the walk's root location is gone", reason="unknown_location")
    return root


def _require_kind(walk: ProvisioningSession, kind: ProvisioningKind) -> None:
    if walk.kind != kind:
        raise ProvisioningError(
            f"session {walk.id} is a {walk.kind} walk, not a {kind} one",
            reason="wrong_session_kind",
        )


def _require_open(walk: ProvisioningSession) -> None:
    """A finished walk accepts no further binds.

    Undo is deliberately exempt (see `undo`): binding the last drawer completes
    the walk, and an Undo button that stops working on the last drawer is the
    one place it is most likely to be needed.
    """
    if walk.completed_at is not None:
        raise ProvisioningError(
            f"session {walk.id} is already complete; start a new walk", reason="session_completed"
        )


def _sync_completion(session: Session, walk: ProvisioningSession) -> None:
    """Mark a walk complete exactly when its derived cursor runs out.

    Also *un*-marks it: undoing the bind that finished a cabinet means the
    cabinet is no longer finished, and the walk it belongs to has to reopen or
    the next bind would be refused.
    """
    root = require_root(session, walk)
    exhausted = next_slot(session, walk, root=root) is None
    if exhausted and walk.completed_at is None:
        walk.completed_at = utcnow()
    elif not exhausted and walk.completed_at is not None:
        walk.completed_at = None


# ---------------------------------------------------------------------------
# The derived cursor
# ---------------------------------------------------------------------------


def _skipped_location_ids(session: Session, walk: ProvisioningSession) -> set[int]:
    return set(
        session.execute(
            select(ProvisioningAction.location_id).where(
                ProvisioningAction.session_id == walk.id,
                ProvisioningAction.kind == ProvisioningActionKind.SKIP,
                ProvisioningAction.undone_at.is_(None),
            )
        )
        .scalars()
        .all()
    )


def _checked_location_ids(session: Session, walk: ProvisioningSession) -> set[int]:
    return set(
        session.execute(
            select(ProvisioningAction.location_id).where(
                ProvisioningAction.session_id == walk.id,
                ProvisioningAction.kind == ProvisioningActionKind.CHECK,
                ProvisioningAction.undone_at.is_(None),
            )
        )
        .scalars()
        .all()
    )


def _first_child(session: Session, query: Select[tuple[Location]]) -> Location | None:
    ordered = query.order_by(Location.sort_order, Location.id).limit(1)
    return session.execute(ordered).scalars().first()


def next_unbound_child(
    session: Session, root: Location, *, skipped: Collection[int] = ()
) -> Location | None:
    """`MIN(sort_order)` among the children with no `location_tags` row.

    This *is* the provisioning cursor, and computing it here rather than storing
    it is what makes a half-finished cabinet free to resume: a drawer bound from
    a phone in the middle of a session is simply already bound.
    """
    query = (
        select(Location)
        .outerjoin(LocationTag, LocationTag.location_id == Location.id)
        .where(Location.parent_id == root.id, LocationTag.id.is_(None))
    )
    if skipped:
        query = query.where(Location.id.notin_(set(skipped)))
    return _first_child(session, query)


def next_unchecked_tagged_child(
    session: Session, root: Location, *, checked: Collection[int] = ()
) -> Location | None:
    """The verification cursor: the first *tagged* child not yet ticked.

    A mismatch writes no tick, so this keeps returning the offending drawer.
    That is the stop — the walk cannot step past a drawer nobody has explained.
    """
    query = (
        select(Location)
        .join(LocationTag, LocationTag.location_id == Location.id)
        .where(Location.parent_id == root.id)
    )
    if checked:
        query = query.where(Location.id.notin_(set(checked)))
    return _first_child(session, query)


def next_slot(
    session: Session, walk: ProvisioningSession, *, root: Location | None = None
) -> Location | None:
    """Where the walk is, computed from scratch every single time."""
    root = root if root is not None else require_root(session, walk)
    if walk.kind == ProvisioningKind.VERIFY:
        return next_unchecked_tagged_child(
            session, root, checked=_checked_location_ids(session, walk)
        )
    return next_unbound_child(session, root, skipped=_skipped_location_ids(session, walk))


def resolve_target(
    session: Session,
    walk: ProvisioningSession,
    location_id: int | None,
) -> Location:
    """The slot this step acts on: the tapped one, or the derived cursor.

    Tapping any cell jumps the cursor there — auto-advance is a fast path, not a
    lock — so an explicit id wins, subject only to being part of this walk. The
    cabinet itself is in scope because its own tag is legitimately part of the
    same physical walk, even though the cursor never lands on it: the walk is
    defined over the children.
    """
    root = require_root(session, walk)
    if location_id is None:
        cursor = next_slot(session, walk, root=root)
        if cursor is None:
            raise ProvisioningError(f"nothing left to do in {root.name!r}", reason="walk_complete")
        return cursor

    target = session.get(Location, location_id)
    if target is None:
        raise ProvisioningError(f"no location with id {location_id}", reason="unknown_location")
    if target.id != root.id and target.parent_id != root.id:
        raise ProvisioningError(
            f"{target.name!r} is not part of the walk over {root.name!r}", reason="out_of_scope"
        )
    return target


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisionProgress:
    """Cabinet-wide, not session-wide. `bound` counts drawers with a tag however
    they got one, which is the number the person at the cabinet can see."""

    total_slots: int
    bound: int
    unbound: int
    skipped: int
    is_complete: bool


@dataclass(frozen=True)
class VerifyProgress:
    total_tagged: int
    checked: int
    remaining: int
    mismatches: int


def child_count(session: Session, root: Location) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(Location).where(Location.parent_id == root.id)
        ).scalar_one()
    )


def tagged_child_count(session: Session, root: Location) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(Location)
            .join(LocationTag, LocationTag.location_id == Location.id)
            .where(Location.parent_id == root.id)
        ).scalar_one()
    )


def provision_progress(session: Session, walk: ProvisioningSession) -> ProvisionProgress:
    root = require_root(session, walk)
    total = child_count(session, root)
    bound = tagged_child_count(session, root)
    return ProvisionProgress(
        total_slots=total,
        bound=bound,
        unbound=total - bound,
        skipped=len(_skipped_location_ids(session, walk)),
        is_complete=next_slot(session, walk, root=root) is None,
    )


def verify_progress(session: Session, walk: ProvisioningSession) -> VerifyProgress:
    root = require_root(session, walk)
    total = tagged_child_count(session, root)
    checked = len(_checked_location_ids(session, walk))
    return VerifyProgress(
        total_tagged=total,
        checked=checked,
        remaining=max(total - checked, 0),
        mismatches=len(mismatches(session, walk)),
    )


# ---------------------------------------------------------------------------
# Bind
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindOutcome:
    """What one tap did — or refused to do, without touching anything.

    `already_bound_elsewhere` and `slot_already_bound` are the two-button cases:
    the response carries the conflicting binding so the UI can say "Already
    bound to {label_path}" and offer Move here / Cancel with no second round
    trip. Nothing is written on either.
    """

    status: str
    location: Location
    tag: LocationTag | None
    conflict_tag: LocationTag | None = None
    conflict_location: Location | None = None


def tag_at(session: Session, location_id: int) -> LocationTag | None:
    return (
        session.execute(select(LocationTag).where(LocationTag.location_id == location_id))
        .scalars()
        .first()
    )


def tag_with_uid(session: Session, tag_uid: str) -> LocationTag | None:
    """The binding this physical tag currently has, anywhere in the tree.

    Deliberately unscoped: the tag in your hand may have been stuck on a drawer
    in a different room last month, and "already bound to {label_path}" is only
    useful if it can name that.
    """
    return (
        session.execute(
            select(LocationTag).where(LocationTag.tag_uid == tag_uid).order_by(LocationTag.id)
        )
        .scalars()
        .first()
    )


def bind(
    session: Session,
    walk: ProvisioningSession,
    *,
    tag_uid: str,
    location_id: int | None = None,
    move: bool = False,
) -> BindOutcome:
    """Bind one tag to one slot, then let the cursor advance by itself.

    `move=True` is the human having answered "Move here" to a conflict: it is
    required for anything that displaces an existing binding, because a silent
    rebind is how a drawer full of stock ends up answering to a tag that used to
    mean something else.
    """
    _require_kind(walk, ProvisioningKind.PROVISION)
    _require_open(walk)

    uid = normalize_tag_uid(tag_uid)
    target = resolve_target(session, walk, location_id)
    here = tag_at(session, target.id)
    elsewhere = tag_with_uid(session, uid)

    if elsewhere is not None and elsewhere.location_id == target.id:
        # The 400 ms debounce lost a race, or the same tag was tapped twice on
        # purpose. Either way the intended state already holds, so writing a
        # second identical binding would only add an undo step that undoes
        # nothing.
        return BindOutcome(status="already_bound_here", location=target, tag=elsewhere)

    if elsewhere is not None and here is not None:
        # Two displacements, one undo slot. Rather than record a half-reversible
        # action, refuse and let the walk unbind one side explicitly.
        raise ProvisioningError(
            f"{target.name!r} already has a tag and {uid} is bound elsewhere; "
            "unbind one of them first",
            reason="two_conflicts",
        )

    if elsewhere is not None and not move:
        return BindOutcome(
            status="already_bound_elsewhere",
            location=target,
            tag=None,
            conflict_tag=elsewhere,
            conflict_location=session.get(Location, elsewhere.location_id),
        )

    if here is not None and not move:
        return BindOutcome(
            status="slot_already_bound",
            location=target,
            tag=None,
            conflict_tag=here,
            conflict_location=target,
        )

    prior = elsewhere or here
    if prior is None:
        kind = ProvisioningActionKind.BIND
    elif prior.location_id == target.id:
        kind = ProvisioningActionKind.REBIND
    else:
        kind = ProvisioningActionKind.MOVE

    url = ndef_url_for(session, target)
    action = ProvisioningAction(
        session_id=walk.id,
        kind=kind,
        location_id=target.id,
        tag_uid=uid,
        ndef_url=url,
    )
    if prior is not None:
        action.prior_location_id = prior.location_id
        action.prior_tag_uid = prior.tag_uid
        action.prior_ndef_url = prior.ndef_url
        action.prior_bind_source = prior.bind_source
        action.prior_written_at = prior.written_at
        action.prior_is_read_only = prior.is_read_only
        session.delete(prior)
        session.flush()

    session.add(action)
    tag = LocationTag(
        location_id=target.id,
        tag_uid=uid,
        ndef_url=url,
        written_at=utcnow(),
        bind_source=walk.device_kind or ProvisioningDevice.MANUAL,
    )
    session.add(tag)
    walk.bound_count += 1
    session.flush()
    _sync_completion(session, walk)

    status = {
        ProvisioningActionKind.BIND: "bound",
        ProvisioningActionKind.MOVE: "moved",
        ProvisioningActionKind.REBIND: "rebound",
    }[kind]
    return BindOutcome(status=status, location=target, tag=tag)


# ---------------------------------------------------------------------------
# Skip
# ---------------------------------------------------------------------------


def skip(
    session: Session, walk: ProvisioningSession, *, location_id: int | None = None
) -> Location:
    """Leave a slot empty and advance. Session-scoped, and undoable."""
    _require_kind(walk, ProvisioningKind.PROVISION)
    _require_open(walk)

    target = resolve_target(session, walk, location_id)
    session.add(
        ProvisioningAction(
            session_id=walk.id,
            kind=ProvisioningActionKind.SKIP,
            location_id=target.id,
        )
    )
    walk.skipped_count += 1
    session.flush()
    _sync_completion(session, walk)
    return target


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def undoable_actions(session: Session, walk: ProvisioningSession) -> list[ProvisioningAction]:
    """The undo stack, newest first, at most `UNDO_DEPTH` deep.

    The window is the newest five actions *whatever their state*, filtered down
    to the ones still standing. Taking "the newest five not-undone" instead
    would refill the window from below on every undo, which is a bottomless
    stack wearing a five-deep label.
    """
    window = list(
        session.execute(
            select(ProvisioningAction)
            .where(ProvisioningAction.session_id == walk.id)
            .order_by(ProvisioningAction.id.desc())
            .limit(UNDO_DEPTH)
        ).scalars()
    )
    return [action for action in window if action.undone_at is None]


@dataclass(frozen=True)
class UndoOutcome:
    action_kind: str
    location: Location
    tag_uid: str | None
    #: The binding put back, when the undone action was a move or a rebind.
    restored_tag: LocationTag | None = None
    #: Set when a prior binding could not be put back because the slot it came
    #: from has been re-tagged since. Reported rather than forced: overwriting
    #: a binding somebody made after the fact would be a second silent rebind.
    not_restored_reason: str | None = None


def undo(session: Session, walk: ProvisioningSession) -> UndoOutcome:
    """Reverse the most recent still-standing action of this walk.

    Deliberately runs on a completed walk too, reopening it: binding the last
    drawer of a cabinet finishes the walk, and that is exactly the bind most
    likely to need taking back.
    """
    _require_kind(walk, ProvisioningKind.PROVISION)

    stack = undoable_actions(session, walk)
    if not stack:
        raise ProvisioningError(
            f"nothing left to undo in session {walk.id}", reason="nothing_to_undo"
        )
    action = stack[0]
    location = session.get(Location, action.location_id)
    if location is None:  # pragma: no cover - CASCADE removes the action with it
        raise ProvisioningError("the slot this action touched is gone", reason="unknown_location")

    restored: LocationTag | None = None
    not_restored: str | None = None

    if action.kind == ProvisioningActionKind.SKIP:
        walk.skipped_count = max(walk.skipped_count - 1, 0)
    else:
        current = tag_at(session, action.location_id)
        if current is not None and current.tag_uid == action.tag_uid:
            session.delete(current)
            session.flush()
        walk.bound_count = max(walk.bound_count - 1, 0)

        if action.prior_location_id is not None and action.prior_tag_uid is not None:
            if tag_at(session, action.prior_location_id) is not None:
                not_restored = "prior_slot_rebound"
            else:
                restored = LocationTag(
                    location_id=action.prior_location_id,
                    tag_uid=action.prior_tag_uid,
                    ndef_url=action.prior_ndef_url or "",
                    written_at=action.prior_written_at or utcnow(),
                    bind_source=action.prior_bind_source,
                    is_read_only=bool(action.prior_is_read_only),
                )
                session.add(restored)

    action.undone_at = utcnow()
    session.flush()
    _sync_completion(session, walk)

    return UndoOutcome(
        action_kind=str(action.kind),
        location=location,
        tag_uid=action.tag_uid,
        restored_tag=restored,
        not_restored_reason=not_restored,
    )


def undo_label(session: Session, walk: ProvisioningSession) -> str | None:
    """The last-acted slot's label, for the always-visible Undo button."""
    stack = undoable_actions(session, walk)
    if not stack:
        return None
    location = session.get(Location, stack[0].location_id)
    if location is None:  # pragma: no cover - CASCADE removes the action with it
        return None
    return location.slot_label or location.name


# ---------------------------------------------------------------------------
# The verification walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckOutcome:
    """One tag re-read. `mismatch` is a recorded finding, never a repair."""

    status: str
    location: Location
    expected_tag_uid: str | None
    scanned_tag_uid: str
    mismatch: VerificationMismatch | None = None
    #: Which slot the scanned tag actually belongs to. "You have swapped B2 and
    #: B3" is actionable in a way that "something is wrong" is not.
    scanned_belongs_to: Location | None = None


def mismatches(
    session: Session, walk: ProvisioningSession, *, unresolved_only: bool = False
) -> list[VerificationMismatch]:
    query = select(VerificationMismatch).where(VerificationMismatch.session_id == walk.id)
    if unresolved_only:
        query = query.where(VerificationMismatch.resolved_at.is_(None))
    return list(session.execute(query.order_by(VerificationMismatch.id)).scalars())


def is_stopped(session: Session, walk: ProvisioningSession) -> bool:
    """True while some slot's tag is unexplained.

    The walk is stopped in the only sense that matters: the cursor is still on
    the offending drawer, so it cannot step past it by itself. Checking another
    slot remains possible — a human choosing to look at the next drawer is not
    the software deciding the mismatch was fine.
    """
    return bool(mismatches(session, walk, unresolved_only=True))


def check(
    session: Session,
    walk: ProvisioningSession,
    *,
    tag_uid: str,
    location_id: int | None = None,
) -> CheckOutcome:
    """Re-read one tag and compare it to the expected UID.

    Writes no binding on either branch. `last_verified_at` moves, which is a
    record of the reading rather than a change to what the tag means — the
    binding's `location_id`, `tag_uid` and `ndef_url` are never touched here.
    """
    _require_kind(walk, ProvisioningKind.VERIFY)

    uid = normalize_tag_uid(tag_uid)
    target = resolve_target(session, walk, location_id)
    expected = tag_at(session, target.id)
    now = utcnow()

    if expected is not None and expected.tag_uid == uid:
        session.add(
            ProvisioningAction(
                session_id=walk.id,
                kind=ProvisioningActionKind.CHECK,
                location_id=target.id,
                tag_uid=uid,
            )
        )
        expected.last_verified_at = now
        target.last_verified_at = now
        # A slot that now reads correctly closes its own earlier finding,
        # whichever of the two repairs the human chose.
        for finding in mismatches(session, walk, unresolved_only=True):
            if finding.location_id == target.id:
                finding.resolved_at = now
        session.flush()
        _sync_completion(session, walk)
        return CheckOutcome(
            status="match",
            location=target,
            expected_tag_uid=expected.tag_uid,
            scanned_tag_uid=uid,
        )

    scanned_elsewhere = tag_with_uid(session, uid)
    belongs_to = (
        session.get(Location, scanned_elsewhere.location_id)
        if scanned_elsewhere is not None
        else None
    )
    finding = VerificationMismatch(
        session_id=walk.id,
        location_id=target.id,
        expected_tag_uid=expected.tag_uid if expected is not None else None,
        scanned_tag_uid=uid,
        scanned_resolved_location_id=(
            scanned_elsewhere.location_id if scanned_elsewhere is not None else None
        ),
    )
    session.add(finding)
    session.flush()
    return CheckOutcome(
        status="mismatch",
        location=target,
        expected_tag_uid=expected.tag_uid if expected is not None else None,
        scanned_tag_uid=uid,
        mismatch=finding,
        scanned_belongs_to=belongs_to,
    )


# ---------------------------------------------------------------------------
# Tag lookup and unbinding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagResolution:
    """NDEF first, UID second — and both reported when they disagree.

    NDEF wins because it is the payload the system authored: a UID is whatever
    the factory burned in, and a tag can be physically moved to another drawer
    without either of them changing. `disagreement` is worth surfacing rather
    than hiding behind the winner: it means the tag was written for one slot and
    is bound to another, which is the exact condition the verification walk
    exists to find.
    """

    status: str
    matched_by: str
    location: Location | None
    tag: LocationTag | None
    disagreement: bool


def resolve_tag(
    session: Session, *, tag_uid: str | None = None, ndef_url: str | None = None
) -> TagResolution:
    ndef_location: Location | None = None
    if ndef_url:
        short_id = parse_ndef_url(ndef_url)
        binding = shortid.resolve(session, short_id) if short_id else None
        if binding is not None and binding.entity_type == EntityType.LOCATION:
            ndef_location = session.get(Location, binding.entity_pk)

    uid_tag: LocationTag | None = None
    uid_location: Location | None = None
    if tag_uid:
        uid_tag = tag_with_uid(session, normalize_tag_uid(tag_uid))
        if uid_tag is not None:
            uid_location = session.get(Location, uid_tag.location_id)

    disagreement = (
        ndef_location is not None
        and uid_location is not None
        and ndef_location.id != uid_location.id
    )
    if ndef_location is not None:
        return TagResolution(
            status="resolved",
            matched_by="ndef",
            location=ndef_location,
            tag=uid_tag if uid_tag is not None else tag_at(session, ndef_location.id),
            disagreement=disagreement,
        )
    if uid_location is not None:
        return TagResolution(
            status="resolved",
            matched_by="uid",
            location=uid_location,
            tag=uid_tag,
            disagreement=False,
        )
    return TagResolution(
        status="unknown", matched_by="none", location=None, tag=None, disagreement=False
    )


def unbind(session: Session, tag: LocationTag) -> None:
    """Forget a binding. The tag itself stays perfectly resolvable.

    Only the location-to-tag link goes: the tag's payload is an opaque short id,
    so `/s/{short_id}` still resolves after this — which is why unbinding does
    not need the tag in hand, while *rewriting* one always would.
    """
    session.delete(tag)
    session.flush()


def label_paths(session: Session, location_ids: Sequence[int]) -> dict[int, str]:
    """`label_path` for a batch of ids, always derived and always fresh.

    Never read off a tag: a container that moves would make an encoded path a
    lie the moment the drawer changed cabinet.
    """
    if not location_ids:
        return {}
    return {
        row[0]: row[1]
        for row in session.execute(
            select(Location.id, Location.label_path).where(Location.id.in_(set(location_ids)))
        ).all()
    }
