"""The station session — PLAN.md workflow 5, honestly adapted to having no scale.

PLAN.md specifies::

    IDLE → CONTAINER_DETECTED (weight jump > ~200 mg)
         → IDENTIFYING (NFC poll, ~5 tries / 1.5 s)
         → IDENTIFIED | UNIDENTIFIED
         → WEIGHED (stable, tare-subtracted)
         → READY (name, path, short_id, ledger balance, weight-derived count)
         → ACTION (take N / add N / recount / pour into the counting tray)
         → CONFIRM → COMMIT

looping back to ACTION while the tag and weight stay present, returning to IDLE on
removal. ADR 0003 deferred the load cell, so two of those states cannot happen and
are **gone rather than stubbed** — a state that is always skipped is a lie in a
diagram somebody will read later. What is built is::

    IDLE
     └─ a tag appears ──▶ IDENTIFYING       (PLAN.md: CONTAINER_DETECTED + IDENTIFYING)
          ├─ a carrier reads ──▶ RESOLVING                    (PLAN.md: IDENTIFIED)
          │    ├─ resolved ──▶ READY                 (PLAN.md: READY, no weight count)
          │    │                └─ propose ──▶ PROPOSED       (PLAN.md: ACTION + CONFIRM)
          │    │                     ├─ cancel ──▶ READY
          │    │                     └─ confirm ──▶ COMMITTING (PLAN.md: COMMIT)
          │    │                          ├─ committed ──▶ READY     ← the loop back to ACTION
          │    │                          └─ failed ──▶ PROPOSED     ← the same key retries
          │    └─ unknown tag ──▶ UNIDENTIFIED
          │         └─ provision it, then `station.refresh` ──▶ RESOLVING
          └─ budget spent (~5 tries) ──▶ UNIDENTIFIED
                 └─ manual search only. `station.refresh` is *refused* here

    from any state:  the container is removed ──▶ IDLE, `station.aborted`, nothing written
                     (PLAN.md: WEIGHED is gone — there is no scale)

**The two ways into `UNIDENTIFIED` are not the same state, and `refresh` is why.**
`refresh` re-asks the server about a carrier this session already read, so it needs
one in hand. The `unknown tag` branch has one — a tag that read perfectly and that
the server has no binding for — so provisioning it in the PWA and then refreshing
reaches `READY` without lifting the container. The `budget spent` branch has none:
`_unreadable` resets, holding no identity, so `refresh` there raises
`nothing_to_resolve` rather than re-asking about nothing. A container provisioned
after a timeout re-enters through the ordinary re-seat instead — set it down again,
`presence` takes its `UNIDENTIFIED → IDENTIFIED` edge with no teardown, and that is
a fresh placement with a fresh session id. Drawing one `refresh` edge under both
branches would be drawing a transition that cannot be taken, which is the same
mistake as keeping `CONTAINER_DETECTED`.

**What changed from PLAN.md, and why:**

* `CONTAINER_DETECTED` is **collapsed into `IDENTIFYING`**. It existed to name the
  weight jump; with polling there is no observable moment between "something is
  there" and "we are trying to read it", because the same poll does both. Keeping
  it would be keeping a state nothing can enter.
* `WEIGHED` is **removed**. There is no scale, so there is no stable
  tare-subtracted mass, and a state that is always skipped is worse than an absent
  one.
* `IDENTIFIED` is **renamed `RESOLVING`**, because what happens between a carrier
  being read and `READY` is one thing: asking `POST /api/location-tags/resolve`
  what the tag means. That call can fail or come back `unknown`, so the interval
  needs a name.
* `ACTION` and `CONFIRM` are **one state, `PROPOSED`**. The agent learns of an
  action only when it is complete, so "the user is choosing" and "the user is
  reviewing" are indistinguishable from here; a confirm screen is a render of the
  pending action, not a state this process can be in separately.
* `COMMIT` becomes `COMMITTING`, an interval rather than an instant, because that
  is exactly where the abort guarantee changes hands.

`READY` shows name, path, short id and the ledger balance. The weight-derived
count is simply absent — no `weight.*` event ever arrives, so the affordance is
never drawn, which is the contract the design already promised. No feature flag.

**Removing the container before COMMIT aborts and writes nothing**, and three
things enforce it rather than one, because this is the guarantee that matters:

1. the only path to a write is `station.confirm` → `_confirm`, which requires a
   pending action *and* `state is PROPOSED`. `tag.removed` clears both before
   anything else runs, so there is nothing left to commit;
2. every command carries the `session_id` minted when the container was
   identified. A confirm that races the lift arrives holding the *previous*
   session's id and is refused — so the user's last tap cannot land against the
   next container either;
3. one `asyncio.Lock` serialises presence and commands, so a removal cannot
   interleave with a commit; it either precedes it (nothing is written) or follows
   it (the write was already confirmed, and undo — not the tag — is what reverses
   it). There is no third case to reason about.

**The agent is not a second ledger writer.** Commits go over HTTP to the existing
`/api/stock/...` routes; `app/services/ledger.py` stays the only writer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Final

from agent import events
from agent.api import (
    QTY_MILLI_MAX,
    Action,
    ActionKind,
    ApiError,
    ApiUnavailable,
    ContainerView,
    LotView,
    StationApi,
    TagResolution,
    route_for,
)
from agent.events import Event
from agent.holdoff import DEFAULT_WINDOW_MS, HoldOff
from agent.identity import TagIdentity

logger = logging.getLogger("almagest.deviceagent.session")

#: Bigger than any command this vocabulary can express, and small enough that a
#: runaway local page cannot make the agent allocate. Same reasoning as
#: `PAYLOAD_MAX_LENGTH` on `/api/scan/resolve`: refuse before parsing, so a huge
#: frame cannot become a huge anything.
MAX_FRAME_BYTES: Final = 4096


class SessionState(StrEnum):
    """Workflow 5 as built. See the module docstring for what PLAN.md called these.

    A `StrEnum` off-database is safe: the no-`sa.Enum` rule exists because SQLite
    cannot alter a `CHECK` constraint, and nothing here touches a column.
    """

    IDLE = "idle"
    #: Something is on the platform and no carrier has read yet. PLAN.md's
    #: CONTAINER_DETECTED and IDENTIFYING, which polling makes one state.
    IDENTIFYING = "identifying"
    #: A carrier is in hand and the API is being asked what it means. PLAN.md's
    #: IDENTIFIED, renamed for the round trip it actually is.
    RESOLVING = "resolving"
    #: Container known, contents and balances loaded. The loop lives here.
    READY = "ready"
    #: An action exists and **nothing has been written**. PLAN.md's ACTION and
    #: CONFIRM.
    PROPOSED = "proposed"
    #: The write is in flight. The one interval where a removal can no longer
    #: prevent it.
    COMMITTING = "committing"
    #: No tag, an unreadable one, or one the server has no binding for. Falls
    #: through to manual search or "provision this container now" — never a dead
    #: end.
    UNIDENTIFIED = "unidentified"


class _BadCommand(Exception):
    """A command that was well-formed JSON and still cannot be honoured.

    Raised rather than returned so the twelve refusal sites read as one line each,
    and converted to exactly one `station.rejected` in `_dispatch`.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _new_id() -> str:
    return str(uuid.uuid4())


def _projected(balance: int, action: Action) -> int:
    """The balance a confirmed action would leave, for the confirm screen.

    A preview computed from the cached balance, never a second source of truth:
    the movement response is the authority, and it is what `station.committed`
    carries.
    """
    if action.kind is ActionKind.TAKE:
        return balance - action.qty_milli
    if action.kind is ActionKind.ADD:
        return balance + action.qty_milli
    return action.qty_milli


def _action_of(raw: object) -> Action:
    """Parse a proposal's `action` object. Every refusal is a `_BadCommand`.

    **Quantities are absolute, never deltas.** That is what makes a repeated
    proposal recognisable as a duplicate rather than as a second increment, which
    is what lets the 400 ms hold-off drop a double-tapped stepper safely.
    """
    if not isinstance(raw, dict):
        raise _BadCommand("bad_action", "action must be an object")
    kind_raw = raw.get("kind")
    if not isinstance(kind_raw, str) or kind_raw not in set(ActionKind):
        raise _BadCommand(
            "unknown_action",
            f"action.kind must be one of {sorted(str(kind) for kind in ActionKind)}",
        )
    kind = ActionKind(kind_raw)
    lot_id = raw.get("lot_id")
    if not isinstance(lot_id, int) or isinstance(lot_id, bool) or lot_id < 1:
        raise _BadCommand("bad_action", "action.lot_id must be a positive integer")
    qty = raw.get("qty_milli")
    if not isinstance(qty, int) or isinstance(qty, bool):
        raise _BadCommand("bad_action", "action.qty_milli must be an integer")
    minimum = route_for(kind).minimum
    if not minimum <= qty <= QTY_MILLI_MAX:
        # The API's own bounds (`app.api.limits`), enforced here so a nonsense
        # quantity is refused at the bench instead of costing a round trip to be
        # told 422 — and so a fat-fingered keypad entry never reaches the ledger's
        # accumulating cache.
        raise _BadCommand(
            "bad_quantity",
            f"{kind} needs a quantity between {minimum} and {QTY_MILLI_MAX} milli-units",
        )
    return Action(kind=kind, lot_id=lot_id, qty_milli=qty)


def parse_frame(raw: str) -> dict[str, Any] | None:
    """A client frame, or `None` for anything that is not a command.

    **Junk produces no event, only a log line.** A refused *command* is
    operational truth the user needs on screen ("your tap did nothing, the
    container left"); a malformed frame is a programmer error, and letting one
    paint the bench display would hand any page on the loopback a way to shout at
    the kiosk. Unknown types are dropped for the same reason, which is also what
    keeps `station.*` events echoed back by a confused client from meaning
    anything.
    """
    if len(raw) > MAX_FRAME_BYTES:
        logger.warning("dropped a %d-byte frame; the limit is %d", len(raw), MAX_FRAME_BYTES)
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("dropped a frame that is not JSON")
        return None
    if not isinstance(parsed, dict):
        logger.warning("dropped a frame that is not an object")
        return None
    kind = parsed.get("type")
    if not isinstance(kind, str) or kind not in events.COMMAND_TYPES:
        logger.warning("dropped a frame with type %r", kind)
        return None
    return parsed


class StationSession:
    """One bench station's session. One instance per agent process.

    Fed presence events from `agent.presence.TagPresence` and command frames from
    `agent.ws`; every method returns the events it caused, and the caller
    publishes them. Nothing here writes to a socket, so the whole of workflow 5 is
    testable against a fake reader and a fake API.
    """

    def __init__(
        self,
        api: StationApi,
        *,
        debounce_ms: int = DEFAULT_WINDOW_MS,
        mint: Callable[[], str] = _new_id,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        #: `mint` is injected so a test can assert *which* key was committed
        #: under; production mints uuid4, which is what `client_operations`
        #: expects and what the 36-character column holds exactly.
        self._mint = mint
        self._holdoff = HoldOff(debounce_ms, clock=clock)
        #: Presence advances from the poll loop and commands arrive from the
        #: WebSocket handler; both mutate this machine. The lock is also load
        #: bearing for the abort guarantee — see the module docstring, point 3.
        self._lock = asyncio.Lock()

        self.state = SessionState.IDLE
        self.session_id: str | None = None
        self.client_op_id: str | None = None
        self.identity: TagIdentity | None = None
        self.resolution: TagResolution | None = None
        self.container: ContainerView | None = None
        self.pending: Action | None = None
        #: The action a commit was last *attempted* with, kept because the API
        #: refuses a key reused for a different body: editing a proposal after a
        #: failed commit must therefore mint a new key, while retrying the same
        #: one must not.
        self._attempted: Action | None = None

    # -- presence ----------------------------------------------------------

    async def on_presence(self, event: Event) -> list[Event]:
        """React to one `tag.*` event. Returns the `station.*` events it caused.

        Reads the identity back out of the **published** event body rather than
        taking a privileged side channel from the presence machine. That is
        deliberate: anything this session can see, a client can see too, so there
        is no state the protocol cannot express.
        """
        async with self._lock:
            if event.type == events.TAG_READING:
                if self.state is SessionState.IDLE:
                    self.state = SessionState.IDENTIFYING
                return []
            if event.type == events.TAG_IDENTIFIED:
                return await self._begin(_identity_of(event))
            if event.type == events.TAG_TIMEOUT:
                return self._unreadable()
            if event.type == events.TAG_REMOVED:
                return self._removed(_missed_polls_of(event))
            # `tag.error` deliberately changes nothing. A container has not moved
            # because a UART did, and aborting a session on a transport blip
            # would discard the user's work for no reason. If the tag really is
            # gone, the first good poll afterwards produces the removal through
            # the ordinary debounce.
            return []

    async def _begin(self, identity: TagIdentity) -> list[Event]:
        """A placement was identified. **This is where the keys are minted.**

        Both ids exist before the user can possibly have acted, which is the same
        discipline the scan path uses ("a client `uuid4` idempotency key is
        attached at scan"): a retried commit cannot double-move stock, because the
        key it retries with was fixed before the first attempt.
        """
        self._reset()
        self.session_id = self._mint()
        self.client_op_id = self._mint()
        self.identity = identity
        self.state = SessionState.RESOLVING
        return await self._resolve()

    def _unreadable(self) -> list[Event]:
        """`tag.timeout`: something is there and the identify budget is spent.

        A session id is minted so the client can correlate, and **no idempotency
        key is** — there is no lot to move, and a key for an operation that cannot
        exist would be a promise about a write that has no target. Manual search
        and provisioning are both the PWA calling the API with keys of its own.

        The `_reset()` is what makes this state different from the other route into
        `UNIDENTIFIED`: no carrier was read, so nothing is held, so `station.refresh`
        cannot help and says so (`nothing_to_resolve`). A container provisioned after
        a timeout comes back by being set down again, not by refreshing — see the
        module docstring.
        """
        self._reset()
        self.session_id = self._mint()
        self.state = SessionState.UNIDENTIFIED
        return [
            events.station_unidentified(
                state=self.state,
                session_id=self.session_id,
                reason="unreadable",
                identity=None,
            )
        ]

    def _removed(self, missed_polls: int) -> list[Event]:
        """The container is gone. **Abort, and write nothing.**

        Everything is cleared before this returns, so a confirm that was already
        in flight from the client finds no pending action and a stale session id.
        `swapped` is distinguished from `removed` because it is worth saying which
        happened, and `tag.removed`'s `missed_polls: 0` is how presence marks a
        swap.
        """
        if self.state is SessionState.IDLE:
            return []
        session_id = self.session_id
        discarded = self.pending
        reason = "swapped" if missed_polls == 0 else "removed"
        self._reset()
        if session_id is None:  # pragma: no cover - a non-idle state always has one
            return []
        return [
            events.station_aborted(
                state=self.state,
                session_id=session_id,
                reason=reason,
                discarded=discarded,
            )
        ]

    # -- commands ----------------------------------------------------------

    async def handle_frame(self, raw: str) -> list[Event]:
        """Handle one client frame. The entire inbound surface of this process."""
        frame = parse_frame(raw)
        if frame is None:
            return []
        async with self._lock:
            return await self._dispatch(frame)

    async def _dispatch(self, frame: dict[str, Any]) -> list[Event]:
        try:
            self._require_session(frame.get("session_id"))
            kind = frame["type"]
            if kind == events.STATION_PROPOSE:
                return self._propose(frame.get("action"))
            if kind == events.STATION_CONFIRM:
                return await self._confirm()
            if kind == events.STATION_CANCEL:
                return self._cancel()
            return await self._refresh()
        except _BadCommand as refusal:
            return [
                events.station_rejected(
                    state=self.state,
                    session_id=self.session_id,
                    reason=refusal.reason,
                    message=refusal.message,
                )
            ]

    def _require_session(self, given: object) -> None:
        """Every command names the session it belongs to. **The abort race lives here.**

        A confirm sent as the user lifts the drawer arrives holding the id of the
        session that just ended, and is refused. Without this check it would be
        applied to whatever is on the platform now — which is how a take lands
        against the wrong bin.
        """
        if not isinstance(given, str) or not given:
            raise _BadCommand("missing_session_id", "every command must carry session_id")
        if self.session_id is None:
            raise _BadCommand("no_session", "nothing is on the platform")
        if given != self.session_id:
            raise _BadCommand(
                "stale_session",
                "that session has ended: the container was removed or swapped",
            )

    def _propose(self, raw_action: object) -> list[Event]:
        """PLAN.md's ACTION, entered. Nothing is written and nothing is reserved."""
        container = self.container
        if container is None or self.state not in {SessionState.READY, SessionState.PROPOSED}:
            raise _BadCommand("not_ready", f"the station is {self.state}, not ready for an action")
        op_id = self.client_op_id
        if op_id is None:  # pragma: no cover - READY always has one
            raise _BadCommand("not_ready", "this placement has no idempotency key")

        action = _action_of(raw_action)
        lot = container.lot(action.lot_id)
        if lot is None:
            # A command can only name a lot the agent itself announced. That is
            # what keeps this socket from being a way to move any stock in the
            # system: the vocabulary is "the lot you told me about", not "lot 4173".
            raise _BadCommand(
                "unknown_lot", f"lot {action.lot_id} is not in {container.label_path}"
            )

        if not self._holdoff.admit(f"propose:{op_id}:{action.fingerprint}"):
            # A double-tapped stepper. Silence rather than a rejection: nothing
            # went wrong, and the state already says what the user wanted.
            return []

        if self._attempted is not None and self._attempted != action:
            # The last commit attempt failed and the user changed their mind. The
            # old key may or may not be recorded server-side, and reusing it with
            # a different body is a 409 `request_mismatch` by design — so this is
            # a new operation and it gets a new key.
            self._rotate_key()
            self._attempted = None
            op_id = self.client_op_id or op_id

        self.pending = action
        self.state = SessionState.PROPOSED
        return [
            events.station_proposed(
                state=self.state,
                session_id=self.session_id or "",
                client_op_id=op_id,
                action=action,
                projected_qty_milli=_projected(lot.qty_milli, action),
            )
        ]

    async def _confirm(self) -> list[Event]:
        """PLAN.md's CONFIRM → COMMIT. The only path in this process to a write."""
        action = self.pending
        op_id = self.client_op_id
        if action is None or op_id is None or self.state is not SessionState.PROPOSED:
            raise _BadCommand("nothing_pending", "there is no action awaiting confirmation")

        if not self._holdoff.admit(f"confirm:{op_id}:{action.fingerprint}"):
            # A double-tapped Commit, or a client retrying inside the window.
            # PLAN.md's take/return screen drops duplicates "within ~2 s or during
            # an in-flight commit by the same debounce as the decoder"; the
            # in-flight half is covered by `state is not PROPOSED` above.
            return []

        self.state = SessionState.COMMITTING
        self._attempted = action
        try:
            movement = await self._api.commit(
                kind=action.kind,
                lot_id=action.lot_id,
                qty_milli=action.qty_milli,
                client_op_id=op_id,
            )
        except ApiUnavailable as error:
            # The pending action and its key are left exactly as they were, so the
            # same confirm can be retried: if the request did land, the API
            # replays the stored response instead of moving stock twice.
            self.state = SessionState.PROPOSED
            return [self._failed("api_unavailable", str(error))]
        except ApiError as error:
            self.state = SessionState.PROPOSED
            return [self._failed(error.reason, str(error))]

        return self._committed(action, op_id, movement.seqs, movement.lot, movement.replayed)

    def _committed(
        self,
        action: Action,
        op_id: str,
        seqs: tuple[int, ...],
        lot: LotView,
        replayed: bool,
    ) -> list[Event]:
        """Land the result and loop back to `READY` with the new balance.

        The lot is patched from the movement response rather than re-read: that
        response is authoritative for the lot that moved, and an extra round trip
        on every commit is how a bench station comes to feel slow. `refresh` is
        there for anything that changed elsewhere.

        A fresh key is minted here, not at the next proposal, so the invariant
        "there is always a key in hand before the user acts" holds through the
        whole loop and not just at identify time.
        """
        if self.container is not None:
            self.container = self.container.with_lot(lot)
        self.pending = None
        self._attempted = None
        self.state = SessionState.READY
        self._rotate_key()
        committed = events.station_committed(
            state=self.state,
            session_id=self.session_id or "",
            client_op_id=op_id,
            action=action,
            seqs=seqs,
            lot=lot,
            replayed=replayed,
        )
        return [committed, *self._ready_events()]

    def _cancel(self) -> list[Event]:
        """The Cancel button. Discards the proposal, keeps the session.

        Idempotent and silent with nothing pending: a second tap on Cancel is not
        an error worth a screen. Re-emitting `station.ready` rather than inventing
        a "cancelled" event keeps the replayable slot holding a *state* — "the
        ready screen, nothing pending" is exactly what is now true.
        """
        if self.pending is None:
            return []
        self.pending = None
        if self._attempted is not None:
            # A commit was attempted and failed, so that key may be recorded
            # server-side. Retiring it means the next action cannot collide with it.
            self._rotate_key()
            self._attempted = None
        self.state = SessionState.READY
        return self._ready_events()

    async def _refresh(self) -> list[Event]:
        """Re-ask the server. **Never re-identifies and never writes.**

        Two jobs: pick up balances changed by somebody else, and re-ask about a tag
        that **read** but that the server had no binding for a moment ago and has
        just been provisioned — the fall-through that stops *that* route into
        `UNIDENTIFIED` being a dead end without lifting the container.

        It cannot rescue the other route. A spent identify budget read no carrier at
        all, so there is nothing to re-ask about and the last branch here refuses;
        that container comes back by being set down again. See the module docstring.

        Refused while an action is pending: refreshing would move the client off
        its confirm screen, and a proposal reviewed against one balance and
        committed against another is exactly the confusion the confirm step exists
        to prevent.
        """
        if self.pending is not None:
            raise _BadCommand("action_pending", "cancel the pending action before refreshing")
        key = f"refresh:{self.session_id}"
        if not self._holdoff.admit(key):
            return []

        container = self.container
        if container is not None:
            try:
                self.container = await self._api.read_container(container.location_id)
            except ApiUnavailable as error:
                # The window is given back before returning. Every other silent
                # hold-off here is justified by "the state already says what the
                # user wanted" — that is false when the last thing on screen is
                # an error. Without this the second tap of Refresh produces *no
                # event at all*: no retry, no rejection, nothing, for 400 ms, with
                # a failure showing. `_propose` gets the ordering right by
                # admitting after its validation; this admitted before the work.
                self._holdoff.forget(key)
                return [self._failed("api_unavailable", str(error))]
            except ApiError as error:
                self._holdoff.forget(key)
                return [self._failed(error.reason, str(error))]
            self.state = SessionState.READY
            return self._ready_events()

        if self.identity is not None and self.identity.is_identified:
            self.state = SessionState.RESOLVING
            return await self._resolve()

        # Same reason: a refusal the user can act on must not also cost them the
        # next tap.
        self._holdoff.forget(key)
        raise _BadCommand(
            "nothing_to_resolve",
            "this placement produced no readable carrier; lift the container and set it down again",
        )

    # -- the API round trip ------------------------------------------------

    async def _resolve(self) -> list[Event]:
        """Tag → container → contents. Two calls, both reads.

        On failure the state stays `RESOLVING` rather than falling to
        `UNIDENTIFIED`: "the server is unreachable" and "this tag is bound to
        nothing" are different facts, and offering to provision a tag that may be
        perfectly bound would invite a user to overwrite a good binding.
        """
        identity = self.identity
        session_id = self.session_id
        if identity is None or session_id is None:  # pragma: no cover - set by the callers
            logger.error("resolve with no placement: state=%s", self.state)
            return []
        try:
            resolution = await self._api.resolve_tag(
                tag_uid=identity.tag_uid, ndef_url=identity.ndef_url
            )
            self.resolution = resolution
            if not resolution.is_resolved or resolution.location_id is None:
                self.state = SessionState.UNIDENTIFIED
                return [
                    events.station_unidentified(
                        state=self.state,
                        session_id=session_id,
                        reason="unknown_tag",
                        identity=identity,
                    )
                ]
            self.container = await self._api.read_container(resolution.location_id)
        except ApiUnavailable as error:
            return [self._failed("api_unavailable", str(error))]
        except ApiError as error:
            return [self._failed(error.reason, str(error))]

        self.state = SessionState.READY
        return self._ready_events()

    # -- helpers -----------------------------------------------------------

    def _ready_events(self) -> list[Event]:
        """The `station.ready` frame, or nothing if there is no placement to describe.

        A list rather than an `Event` so the impossible case needs no exception: a
        caller reaching here without a resolved container is a bug in this module,
        and the right shape for that is a loud log line and a missing frame, not a
        traceback that takes the poll loop — and the reader — down with it.
        """
        if (
            self.container is None
            or self.resolution is None
            or self.session_id is None
            or self.client_op_id is None
        ):  # pragma: no cover - guarded by every caller
            logger.error("station.ready with no resolved placement: state=%s", self.state)
            return []
        return [
            events.station_ready(
                state=self.state,
                session_id=self.session_id,
                client_op_id=self.client_op_id,
                container=self.container,
                resolution=self.resolution,
            )
        ]

    def _failed(self, reason: str, message: str) -> Event:
        return events.station_failed(
            state=self.state,
            session_id=self.session_id or "",
            reason=reason,
            message=message,
            action=self.pending,
        )

    def _rotate_key(self) -> None:
        self.client_op_id = self._mint()

    def _reset(self) -> None:
        """Back to `IDLE`, holding nothing. The abort, and the start of a placement.

        The hold-off map is cleared here too: its keys carry the session's
        idempotency keys, so they are dead the moment the session is, and clearing
        them is what stops a bench that runs for weeks accumulating them.
        """
        self.state = SessionState.IDLE
        self.session_id = None
        self.client_op_id = None
        self.identity = None
        self.resolution = None
        self.container = None
        self.pending = None
        self._attempted = None
        self._holdoff.clear()


# ---------------------------------------------------------------------------
# Reading a presence event's body
# ---------------------------------------------------------------------------


def _text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _identity_of(event: Event) -> TagIdentity:
    """Rebuild the identity `tag.identified` published.

    Reconstructed from the wire body on purpose — see `on_presence`. Missing or
    wrongly-typed fields fold to `None`, which the resolve call then refuses as
    `no_carrier` rather than sending a half-formed request.
    """
    return TagIdentity(
        short_id=_text(event.data, "short_id"),
        tag_uid=_text(event.data, "tag_uid"),
        ndef_url=_text(event.data, "ndef_url"),
        via=_text(event.data, "via"),
    )


def _missed_polls_of(event: Event) -> int:
    value = event.data.get("missed_polls")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
