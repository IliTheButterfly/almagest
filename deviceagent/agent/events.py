"""The wire protocol: one envelope, `<device>.<verb>` types, a JSON `data` body.

PLAN.md fixes the scale's half of this vocabulary — `weight.reading` /
`weight.stable` / `weight.timeout` / `weight.error` / `weight.zeroed` — so the
tag half is named to line up verb for verb. That is the whole design
requirement: adding the scale later must be new *types*, never a new protocol.

    weight.reading   ↔  tag.reading      a sample arrived; nothing decided yet
    weight.stable    ↔  tag.identified   the settling rule says this is the answer
    weight.timeout   ↔  tag.timeout      it never settled inside the budget
    weight.error     ↔  tag.error        the device faulted
    weight.zeroed    ↔  —                a tare has no tag analogue
    —                ↔  tag.removed      a scale never leaves the bench; a
                                         container does

**`station.hello` deliberately does not enumerate which devices exist.** It
would be the obvious place for `{"sources": ["tag"]}`, and it is exactly the
feature flag ADR 0003 says not to build. The rule is "scale absent → no
`weight.*` ever emitted → the PWA hides every by-weight affordance. No
special-casing": an affordance is drawn because a `weight.*` event arrived, not
because a capability list permitted it. A client that branches on a capability
list has two code paths to keep honest instead of one, and the one that never
runs on the developer's bench is the one that breaks.

**The `tag.*` half of the stream is one-directional**, and the `station.*` half is
not: workflow 5 has the user propose an action, confirm it and commit it, and the
process that must refuse a commit the instant the container is lifted is the one
holding the reader. So four **commands** exist — `station.propose`,
`station.confirm`, `station.cancel`, `station.refresh` — and they are the whole
inbound surface. Grammar keeps the two apart: **commands are imperative, events
are past tense.** `station.propose` is something you ask for; `station.proposed`
is something that happened.

Every command carries the `session_id` minted when the container was identified,
and nothing else about the world: a command cannot name a lot the agent did not
already announce, and a command carrying a stale session id is refused rather than
applied to whatever is on the platform now. That is what makes "removing the
container before COMMIT writes nothing" hold even when the tap and the lift race.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from agent.api import Action, ContainerView, LotView, TagResolution
from agent.identity import TagIdentity

#: Bumped only for a change a current client could not survive. Reported in
#: `station.hello`, which is the one message whose shape may never change.
#:
#: **2** adds the bridge half (ADR 0013): `device.*`, `tag.writing` and its two
#: outcomes, and the `tag.write` command. A version-1 client survives all of it
#: — unknown types are ignored by design — but it cannot write, and a UI that
#: offers a write it cannot perform is worse than one that does not offer it. So
#: the bump is not about compatibility, it is so a client can tell.
PROTOCOL_VERSION: Final = 2

TAG_READING: Final = "tag.reading"
TAG_IDENTIFIED: Final = "tag.identified"
TAG_TIMEOUT: Final = "tag.timeout"
TAG_ERROR: Final = "tag.error"
TAG_REMOVED: Final = "tag.removed"
STATION_HELLO: Final = "station.hello"

#: The session half — workflow 5 from `READY` on. `tag.*` says what the reader
#: saw; `station.*` says what the *session* is, which is the thing the kiosk
#: renders.
STATION_READY: Final = "station.ready"
STATION_PROPOSED: Final = "station.proposed"
STATION_UNIDENTIFIED: Final = "station.unidentified"
STATION_COMMITTED: Final = "station.committed"
STATION_ABORTED: Final = "station.aborted"
STATION_REJECTED: Final = "station.rejected"
STATION_FAILED: Final = "station.failed"

#: The bridge half — ADR 0013. `device.*` says what readers exist and what each
#: one can do; the `tag.write*` family says what happened to a write.
#:
#: **`device.*` is a capability announcement, and `station.hello` still is not.**
#: The rule ADR 0003 set — no feature flags, an affordance is drawn because an
#: event arrived — is about sensors whose absence is silence. A write is not
#: drawn from a stream: it is a command issued against a *named* device, and no
#: history of `tag.identified` distinguishes a PN532 that can write from a
#: Flipper that cannot, nor says which of two attached readers to hold the tag
#: against. That is the whole argument, and ADR 0013 makes it at length.
DEVICE_ATTACHED: Final = "device.attached"
DEVICE_DETACHED: Final = "device.detached"
DEVICE_ERROR: Final = "device.error"

#: `tag.writing` is `tag.reading`'s twin: something is happening and nothing is
#: decided. The two outcomes are split the same way `station.rejected` and
#: `station.failed` are, and for the same reason — the recovery differs. A
#: *refusal* is a fact about the tag (it is not blank, the field is empty) with a
#: user-facing answer; a *failure* is the reader breaking, and telling someone to
#: re-seat a drawer would be a lie.
#: A tap on a *bridge* reader — one debounced sighting, carriers as read.
#:
#: **Deliberately not `tag.identified`.** That one is the output of the station's
#: presence machine: an identify budget, a removal debounce, a settling rule, all
#: counted in `poll_forever`'s polls. A provisioning walk wants none of that. It
#: wants "a tag was held against this reader", which is what the browser's
#: `TagPresentation` already models and what `frontend/src/lib/tags/source.ts`
#: consumes. Overloading `tag.identified` would have forced one of the two to
#: pretend, and the station's vocabulary is load-bearing for workflow 5.
TAG_SEEN: Final = "tag.seen"

TAG_WRITING: Final = "tag.writing"
TAG_WRITTEN: Final = "tag.written"
TAG_WRITE_REFUSED: Final = "tag.write_refused"
TAG_WRITE_FAILED: Final = "tag.write_failed"

#: Commands, client → agent. Imperative where every event is past tense, so a
#: frame's direction is readable without a table.
STATION_PROPOSE: Final = "station.propose"
STATION_CONFIRM: Final = "station.confirm"
STATION_CANCEL: Final = "station.cancel"
STATION_REFRESH: Final = "station.refresh"

#: Put a URI on the tag in a named device's field. Imperative, like the four
#: above; `tag.written` is what comes back.
TAG_WRITE: Final = "tag.write"

#: The **entire** inbound vocabulary. Anything else a client sends is dropped
#: unread, which is what keeps this socket's command surface small enough to
#: reason about.
COMMAND_TYPES: Final = frozenset(
    {STATION_PROPOSE, STATION_CONFIRM, STATION_CANCEL, STATION_REFRESH, TAG_WRITE}
)

#: The events that describe a *state* rather than a moment, so a client that
#: connects (or a kiosk tab that reloads) mid-placement can be brought up to date
#: by replaying the last one instead of waiting for the user to lift the
#: container and put it back down. The per-poll `tag.reading` is never replayed
#: because it is stale by definition, and neither is `station.committed` — a
#: reconnecting client that re-rendered a commit banner from three hours ago
#: would be reporting a movement as if it had just happened. The `station.ready`
#: that follows every commit carries the balance it produced, which is the part
#: that is still true.
STICKY_TYPES: Final = frozenset(
    {
        TAG_IDENTIFIED,
        TAG_TIMEOUT,
        STATION_READY,
        STATION_PROPOSED,
        STATION_UNIDENTIFIED,
    }
)

#: Events that mean "there is no session any more", so the sticky slot must be
#: emptied rather than replayed. Both say the platform is clear: `tag.removed`
#: from the reader, `station.aborted` from the session that removal ended.
CLEARING_TYPES: Final = frozenset({TAG_REMOVED, STATION_ABORTED})

#: The device roster is replayed to a reconnecting client too, and it is **a set
#: rather than a slot** — which is why it cannot use `STICKY_TYPES`. A kiosk that
#: reloads with two readers attached needs to hear about both; the sticky
#: mechanism holds exactly one envelope and would tell it about whichever
#: attached last. `agent.hub.EventHub` keeps a dict keyed by `device_id`,
#: inserted on attached and removed on detached.
ROSTER_ADD: Final = DEVICE_ATTACHED
ROSTER_REMOVE: Final = DEVICE_DETACHED


@dataclass(frozen=True, slots=True)
class Event:
    """A type and its body. `seq` and `at` are added when it is published, so
    an event is comparable in a test without freezing a clock."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


def envelope(event: Event, *, seq: int, at: datetime) -> dict[str, Any]:
    """Wrap an event for the wire.

    `seq` is monotonic across the agent's lifetime, which is what lets a
    reconnecting client tell "I have seen this" from "I missed something"
    without the agent tracking per-client state. `at` is ISO-8601 UTC with a `Z`,
    matching every timestamp the API emits — a station log correlated against the
    ledger by hand is a thing that will happen.
    """
    return {
        "type": event.type,
        "seq": seq,
        "at": at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "data": event.data,
    }


def to_json(message: dict[str, Any]) -> str:
    """Compact, key order as inserted. `separators` because at 3 polls/second
    the whitespace is the majority of the bytes on a slow reconnect replay."""
    return json.dumps(message, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Constructors — the only places these payload shapes are written
# ---------------------------------------------------------------------------


def station_hello(*, agent_version: str, last_seq: int) -> Event:
    """Sent to each client on connect, before anything is replayed.

    `last_seq` is the highest `seq` published so far. A client that reconnects
    holding seq 41 and is told `last_seq: 87` knows it missed events; one told
    `last_seq: 41` knows it did not. Hello itself carries `seq: 0` because it is
    per-connection rather than part of the ordered stream.
    """
    return Event(
        STATION_HELLO,
        {
            "protocol": PROTOCOL_VERSION,
            "agent": f"almagest-deviceagent/{agent_version}",
            "last_seq": last_seq,
        },
    )


def tag_reading(*, poll: int, of: int) -> Event:
    """A tag is in the field and has not resolved yet — `weight.reading`'s twin.

    Emitted only while resolution is outstanding, so it is bounded by the retry
    budget rather than repeating for as long as a container sits there. A
    settled tag produces silence; that silence is what keeps the PWA from
    re-firing an action on every poll.
    """
    return Event(TAG_READING, {"poll": poll, "of": of})


def tag_identified(identity: TagIdentity) -> Event:
    """The settled answer for this placement — `weight.stable`'s twin.

    Carries **both carriers**, verbatim UID-normalised and NDEF as read, because
    the PWA posts both to `POST /api/location-tags/resolve` and only the server,
    seeing both, can report that they disagree. `via` says which carrier gave us
    the short id; it is not a claim that anything was resolved.
    """
    return Event(
        TAG_IDENTIFIED,
        {
            "short_id": identity.short_id,
            "tag_uid": identity.tag_uid,
            "ndef_url": identity.ndef_url,
            "via": identity.via,
        },
    )


def tag_timeout(*, polls: int) -> Event:
    """Something is on the platform and the budget is spent — `weight.timeout`'s
    twin, and PLAN.md's `UNIDENTIFIED` state.

    Not an error: an unprovisioned container is the normal state of a container
    before it is provisioned, and the UI's answer is "provision this now" or
    manual search, never a dead end.
    """
    return Event(TAG_TIMEOUT, {"polls": polls})


def tag_error(*, message: str) -> Event:
    """The reader faulted — `weight.error`'s twin.

    `message` is operator-facing diagnostic text and is never parsed. Emitted
    once per run of consecutive faults: an unplugged reader must not become
    thousands of identical messages, and no client can act differently on the
    ten-thousandth.
    """
    return Event(TAG_ERROR, {"message": message})


def tag_removed(*, missed_polls: int) -> Event:
    """The field is confirmed empty; the session is over.

    `missed_polls` is the debounce that had to elapse first, exposed because it
    is the number to look at when removals feel sluggish or spurious.
    """
    return Event(TAG_REMOVED, {"missed_polls": missed_polls})


# ---------------------------------------------------------------------------
# The bridge — which readers exist, and what happened to a write
# ---------------------------------------------------------------------------


def device_attached(
    *, device_id: str, kind: str, label: str, capabilities: dict[str, bool]
) -> Event:
    """A reader is present and ready to be named in a command.

    `device_id` is stable across a detach and reattach of the same physical
    thing — derived from a port or a Bluetooth address, never from a counter — so
    a PWA that had chosen a reader does not lose it when a cable is jiggled.

    `kind` is one of `ProvisioningDevice`'s values, so it can be forwarded to the
    API verbatim when the walk records who bound a tag. `label` is prose for a
    status line and is never parsed.
    """
    return Event(
        DEVICE_ATTACHED,
        {"device_id": device_id, "kind": kind, "label": label, "capabilities": capabilities},
    )


def device_detached(*, device_id: str, reason: str) -> Event:
    """The reader is gone. `reason` is `unplugged` or `failed`.

    Split because they mean different things to a user mid-walk: a cable pulled
    is something they did, and a reader that faulted is not.
    """
    return Event(DEVICE_DETACHED, {"device_id": device_id, "reason": reason})


def device_error(*, device_id: str, message: str) -> Event:
    """A reader could not be opened, or faulted while attached.

    Emitted once per run of consecutive failures, the same shape as `tag.error`:
    a Flipper that is plugged in but has no Antlia installed would otherwise
    produce one of these on every discovery sweep, for ever.
    """
    return Event(DEVICE_ERROR, {"device_id": device_id, "message": message})


def tag_seen(*, device_id: str, identity: TagIdentity) -> Event:
    """One debounced tap on a bridge reader. The walk's whole input.

    Carries **both carriers plus the short id**, because the three are not
    interchangeable and the walk needs different ones at different steps: binding
    a tag is a claim about a specific piece of silicon and needs the UID, while
    confirming which container is in your hand works from any of them. Which ones
    are populated is a property of the reader, which is what
    `device.attached`'s capability set describes.

    `carries_ndef` says whether user memory was looked at *at all*, so a `None`
    URL from a reader that cannot read NDEF never gets mistaken for a blank tag.
    The server enforces the same distinction from the other side
    (`CheckRequest.carries_ndef`); this is where the honest answer is produced.
    """
    return Event(
        TAG_SEEN,
        {
            "device_id": device_id,
            "short_id": identity.short_id,
            "tag_uid": identity.tag_uid,
            "ndef_url": identity.ndef_url,
            "via": identity.via,
        },
    )


def tag_writing(*, request_id: str, device_id: str, url: str) -> Event:
    """A write has started. `tag.reading`'s twin: nothing is decided yet.

    Published *before* the write so a client can disable its own button from an
    event rather than from optimism, and so a write that never returns is
    visible in the stream rather than being an absence.
    """
    return Event(TAG_WRITING, {"request_id": request_id, "device_id": device_id, "url": url})


def tag_written(*, request_id: str, device_id: str, url: str, read_back_url: str | None) -> Event:
    """The write completed, and this is what the tag read back.

    **`read_back_url` is the payload, and there is deliberately no `verified`
    boolean.** ADR 0012 refuses a client-computed one and makes
    `POST /api/location-tags/{id}/write-result` take the read-back URI, compared
    server-side by short id rather than by string. The bridge is a client, so it
    reports what it saw; the PWA forwards it, because the PWA is the thing
    holding the provisioning session and therefore the only thing that knows the
    `tag_id`.

    `url` is echoed alongside so a client that lost track of its own request can
    still tell what was intended from what arrived.
    """
    return Event(
        TAG_WRITTEN,
        {
            "request_id": request_id,
            "device_id": device_id,
            "url": url,
            "read_back_url": read_back_url,
        },
    )


def tag_write_refused(*, request_id: str, device_id: str, reason: str, message: str) -> Event:
    """The write did not happen and the reader is fine. **Nothing was written.**

    `reason` is from the closed vocabulary in `agent.tags` — `no_tag`,
    `not_blank`, `too_long`, `unsupported`, `read_back_failed` — which a PN532
    and a Flipper both draw from, so the PWA has one table and not one per
    reader. `message` is prose and is never parsed.
    """
    return Event(
        TAG_WRITE_REFUSED,
        {
            "request_id": request_id,
            "device_id": device_id,
            "reason": reason,
            "message": message,
        },
    )


def tag_write_failed(*, request_id: str, device_id: str, message: str) -> Event:
    """The *reader* broke. Kept apart from a refusal because the recovery differs.

    A refusal has a user-facing answer — re-seat the tag, tick overwrite, pick a
    different drawer. A failure does not, and telling someone to re-seat a drawer
    when the reader is unplugged is a lie that costs them a minute.

    Whether anything was written is **unknown** after this, which is exactly
    ADR 0012's `degraded`: the UID lives in factory-locked pages 0-2, so the tag
    still identifies itself and the honest next step is to read it back, not to
    assume either way.
    """
    return Event(
        TAG_WRITE_FAILED,
        {"request_id": request_id, "device_id": device_id, "message": message},
    )


# ---------------------------------------------------------------------------
# The session — workflow 5. Every one of these carries `state`.
# ---------------------------------------------------------------------------
#
# `state` is repeated on all of them on purpose. A kiosk renders off the last
# frame it received, whatever that frame was, and a client that had to map seven
# event types onto seven states would be keeping a second copy of this machine.


def station_ready(
    *,
    state: str,
    session_id: str,
    client_op_id: str,
    container: ContainerView,
    resolution: TagResolution,
) -> Event:
    """PLAN.md's `READY`: name, derived path, short id, and the ledger balance.

    Re-emitted on the loop back to `ACTION` after every commit, and after a
    cancel, because "the ready screen, with nothing pending" is exactly what is
    true in both cases — and because it keeps the replayable slot holding a state
    rather than a banner.

    **No weight-derived count.** ADR 0003 deferred the load cell, so that number
    is absent rather than zero or null-with-a-flag: no `weight.*` event ever
    arrives, so the by-weight affordance is never drawn.

    `disagreement` is forwarded, never acted on. The tag's payload names one slot
    and its UID is bound to another; only a human at the drawers can say which is
    right, and the station's job is to say so loudly, not to pick.
    """
    return Event(
        STATION_READY,
        {
            "state": state,
            "session_id": session_id,
            "client_op_id": client_op_id,
            "matched_by": resolution.matched_by,
            "disagreement": resolution.disagreement,
            **container.as_data(),
        },
    )


def station_proposed(
    *,
    state: str,
    session_id: str,
    client_op_id: str,
    action: Action,
    projected_qty_milli: int,
) -> Event:
    """PLAN.md's `ACTION` and `CONFIRM`, which are one state here.

    An action exists and **nothing has been written**. `projected_qty_milli` is
    what the balance will read if it is confirmed — computed from the cached
    balance rather than fetched, because it is a preview and the commit response
    is the authority.

    `client_op_id` is the key this action will commit under, published before the
    user confirms so that a client which loses the response can retry the same
    key and get the same row.
    """
    return Event(
        STATION_PROPOSED,
        {
            "state": state,
            "session_id": session_id,
            "client_op_id": client_op_id,
            "action": action.as_data(),
            "projected_qty_milli": projected_qty_milli,
        },
    )


def station_unidentified(
    *,
    state: str,
    session_id: str,
    reason: str,
    identity: TagIdentity | None,
) -> Event:
    """PLAN.md's `UNIDENTIFIED`. **Never a dead end.**

    Two reasons reach it, and the client's answer to both is the same pair of
    offers: `unreadable` (the identify budget was spent — no tag, or a tag that
    would not answer) and `unknown_tag` (a tag answered and the server has no
    binding for it, which is the normal state of a container before it is
    provisioned).

    `offers` is a hint, not a permission: both flows are the PWA calling the API
    directly, and the carriers are forwarded so a provisioning screen can be
    pre-filled with the tag that is physically on the platform right now.
    """
    return Event(
        STATION_UNIDENTIFIED,
        {
            "state": state,
            "session_id": session_id,
            "reason": reason,
            "short_id": None if identity is None else identity.short_id,
            "tag_uid": None if identity is None else identity.tag_uid,
            "ndef_url": None if identity is None else identity.ndef_url,
            "offers": ["manual_search", "provision"],
        },
    )


def station_committed(
    *,
    state: str,
    session_id: str,
    client_op_id: str,
    action: Action,
    seqs: tuple[int, ...],
    lot: LotView,
    replayed: bool,
) -> Event:
    """The movement is in the ledger. `seqs` are the rows it appended.

    `replayed: true` means the API recognised the key and handed back the answer
    it had already stored — a retry that correctly moved nothing. Rendered as
    success, because it is one.

    The rows are named so an operator can correlate the bench with the ledger,
    and so the PWA's eight-second undo has something to reverse without holding
    state of its own.
    """
    return Event(
        STATION_COMMITTED,
        {
            "state": state,
            "session_id": session_id,
            "client_op_id": client_op_id,
            "action": action.as_data(),
            "seqs": list(seqs),
            "lot": lot.as_data(),
            "replayed": replayed,
        },
    )


def station_aborted(*, state: str, session_id: str, reason: str, discarded: Action | None) -> Event:
    """The session ended without committing, and **nothing was written.**

    `reason` is `removed` (the container was lifted) or `swapped` (another
    container arrived with no empty poll in between). `discarded` is the action
    that was pending, if any — reported so the kiosk can say "your take of 5 was
    discarded" rather than silently clearing the screen, which is how a user comes
    to believe a movement happened when it did not.
    """
    return Event(
        STATION_ABORTED,
        {
            "state": state,
            "session_id": session_id,
            "reason": reason,
            "discarded": None if discarded is None else discarded.as_data(),
        },
    )


def station_rejected(*, state: str, session_id: str | None, reason: str, message: str) -> Event:
    """A command was well-formed and refused. The *station* said no.

    The one that matters is `stale_session`: a confirm that arrives after the
    container left carries the previous session's id, and refusing it is what
    stops the user's last tap landing against the next container.
    """
    return Event(
        STATION_REJECTED,
        {"state": state, "session_id": session_id, "reason": reason, "message": message},
    )


def station_failed(
    *, state: str, session_id: str, reason: str, message: str, action: Action | None
) -> Event:
    """The API said no, or said nothing. The *server* (or the network) refused.

    Kept distinct from `station.rejected` because the recovery differs: a rejected
    command was never going to work, while a failure leaves the pending action —
    and its idempotency key — exactly where they were, so the same confirm can be
    retried safely. `reason` is the API's own code (`insufficient_stock`,
    `api_unavailable`, …) rather than a vocabulary invented here.
    """
    return Event(
        STATION_FAILED,
        {
            "state": state,
            "session_id": session_id,
            "reason": reason,
            "message": message,
            "action": None if action is None else action.as_data(),
        },
    )
