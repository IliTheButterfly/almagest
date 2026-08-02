"""Workflow 5, driven off the fake reader and a fake API. No clocks, no sockets.

The headline case is `test_removing_the_container_before_commit_writes_nothing`,
and it is the reason the whole session lives in this process rather than in the
PWA: a half-finished session that commits is stock that moved without anyone
saying so. Every other test here exists to keep that one honest — that the abort
clears the pending action, that a confirm racing the lift is refused, and that a
loop back to `ACTION` neither re-identifies nor re-commits.

`FakeStationApi.commits` is the stand-in for the ledger at this level: an empty
list means nothing was written. `tests/test_session_ledger.py` proves the same
thing against the real routes and a real database, where "nothing was written" is
a row count.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent import events
from agent.api import ApiError, ApiUnavailable, LotView, TagResolution
from agent.events import Event
from agent.session import MAX_FRAME_BYTES, SessionState
from agent.tags import TagRead
from tests.conftest import Station
from tests.fake_api import CONTAINER, LOT, UNKNOWN, FakeStationApi

SHORT_ID = "4K7T92M8"
TAG = TagRead(uid="04:1A:2B:3C:4D:5E:6F", ndef_url=f"https://almagest.lan/s/{SHORT_ID}")
UID_ONLY = TagRead(uid="04AABBCCDDEE10", ndef_url=None)
UNREADABLE = TagRead(uid=None, ndef_url=None)


def types(emitted: list[Event]) -> list[str]:
    return [event.type for event in emitted]


def body(emitted: list[Event], kind: str) -> dict[str, object]:
    """The body of the one event of this type. Fails loudly on zero or two."""
    matches = [event.data for event in emitted if event.type == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


def take(station: Station, qty_milli: int = 5_000) -> list[Event]:
    return station.send(
        events.STATION_PROPOSE,
        action={"kind": "take", "lot_id": LOT.lot_id, "qty_milli": qty_milli},
    )


# ---------------------------------------------------------------------------
# The states, and the two that are gone
# ---------------------------------------------------------------------------


def test_the_states_are_the_seven_that_can_actually_be_entered() -> None:
    """ADR 0003 removed the load cell, so `WEIGHED` is gone rather than stubbed.

    Asserted as a literal set because the requirement is negative: a state that is
    always skipped is a lie in a diagram somebody will read later, and the way that
    creeps back in is somebody adding it "for symmetry".
    """
    assert {str(state) for state in SessionState} == {
        "idle",
        "identifying",
        "resolving",
        "ready",
        "proposed",
        "committing",
        "unidentified",
    }


def test_nothing_in_the_session_mentions_weight_or_mass() -> None:
    """The absence is the contract: no `weight.*` event, so no by-weight affordance.

    A `WEIGHED` state, a `mass_mg` field or a `weight` key would each be the first
    step towards a UI implying a precision that does not exist.
    """
    import agent.session as module

    names = {name.casefold() for name in dir(module)} | {str(state) for state in SessionState}
    assert not [name for name in names if "weigh" in name or "mass" in name]


# ---------------------------------------------------------------------------
# IDLE → IDENTIFYING → RESOLVING → READY
# ---------------------------------------------------------------------------


def test_a_container_landing_resolves_to_ready_with_the_balance(station: Station) -> None:
    """PLAN.md's READY: name, path, short id, ledger balance — and no count."""
    emitted = station.place(TAG)
    assert types(emitted) == [events.TAG_IDENTIFIED, events.STATION_READY]

    ready = body(emitted, events.STATION_READY)
    assert ready["state"] == "ready"
    assert ready["name"] == CONTAINER.name
    assert ready["label_path"] == CONTAINER.label_path
    assert ready["short_id"] == CONTAINER.short_id
    assert ready["total_qty_milli"] == LOT.qty_milli
    assert ready["lots"] == [LOT.as_data()]
    # The weight-derived count PLAN.md lists is simply absent.
    assert not [key for key in ready if "weight" in key or "mass" in key]
    assert station.session.state is SessionState.READY


def test_both_carriers_go_to_the_server_and_only_the_server_decides(station: Station) -> None:
    """Preferring one carrier locally would hide the mis-binding a verification
    walk exists to find, so `resolve` is given both, verbatim."""
    station.place(TAG)
    assert station.api.resolve_calls == [("041A2B3C4D5E6F", TAG.ndef_url)]


def test_an_unreadable_tag_reports_the_budget_then_falls_through(station: Station) -> None:
    """`UNIDENTIFIED` never dead-ends: manual search, or provision this container."""
    emitted = station.place(UNREADABLE, times=5)
    assert types(emitted)[-1] == events.STATION_UNIDENTIFIED

    unidentified = body(emitted, events.STATION_UNIDENTIFIED)
    assert unidentified["reason"] == "unreadable"
    assert unidentified["offers"] == ["manual_search", "provision"]
    assert station.session.state is SessionState.UNIDENTIFIED
    # Nothing was asked of the API: there was no carrier to ask about.
    assert station.api.resolve_calls == []


def test_a_tag_the_server_does_not_know_falls_through_with_its_carriers(
    station: Station,
) -> None:
    """A blank tag is the normal state of a container before it is provisioned, so
    the provisioning screen gets the tag that is physically on the platform."""
    station.api.resolution = UNKNOWN
    emitted = station.place(TAG)

    unidentified = body(emitted, events.STATION_UNIDENTIFIED)
    assert unidentified["reason"] == "unknown_tag"
    assert unidentified["tag_uid"] == "041A2B3C4D5E6F"
    assert unidentified["ndef_url"] == TAG.ndef_url
    assert unidentified["short_id"] == SHORT_ID
    assert unidentified["offers"] == ["manual_search", "provision"]


def test_provisioning_then_refreshing_reaches_ready_without_lifting_the_container(
    station: Station,
) -> None:
    """The fall-through completed: the user bound the tag in the PWA and asked the
    station to look again. `refresh` re-resolves — it never re-identifies."""
    station.api.resolution = UNKNOWN
    station.place(TAG)
    station.api.resolution = TagResolution(
        status="resolved", matched_by="uid", location_id=CONTAINER.location_id, disagreement=False
    )

    emitted = station.send(events.STATION_REFRESH)
    assert types(emitted) == [events.STATION_READY]
    assert station.session.state is SessionState.READY
    # Resolved twice, but the reader was never asked again.
    assert len(station.api.resolve_calls) == 2


def test_a_disagreement_is_surfaced_and_not_resolved(station: Station) -> None:
    """The tag's payload names one slot and its UID is bound to another. Only a
    human at the drawers can say which is right; the station says so and carries
    on, because stranding the user is not the safer failure."""
    station.api.resolution = TagResolution(
        status="resolved", matched_by="ndef", location_id=CONTAINER.location_id, disagreement=True
    )
    ready = body(station.place(TAG), events.STATION_READY)
    assert ready["disagreement"] is True
    assert ready["matched_by"] == "ndef"


def test_an_unreachable_api_stays_in_resolving_so_the_offer_is_not_made(
    station: Station,
) -> None:
    """ "The server is unreachable" and "this tag is bound to nothing" are different
    facts. Offering to provision a tag that may be perfectly bound invites a user
    to overwrite a good binding."""
    station.api.fail_resolve = ApiUnavailable("connection refused")
    emitted = station.place(TAG)

    failure = body(emitted, events.STATION_FAILED)
    assert failure["reason"] == "api_unavailable"
    assert station.session.state is SessionState.RESOLVING

    station.api.fail_resolve = None
    assert types(station.send(events.STATION_REFRESH)) == [events.STATION_READY]


# ---------------------------------------------------------------------------
# ACTION → CONFIRM → COMMIT
# ---------------------------------------------------------------------------


def test_a_proposal_writes_nothing_and_previews_the_balance(station: Station) -> None:
    station.place(TAG)
    proposed = body(take(station), events.STATION_PROPOSED)

    assert proposed["state"] == "proposed"
    assert proposed["action"] == {"kind": "take", "lot_id": LOT.lot_id, "qty_milli": 5_000}
    assert proposed["projected_qty_milli"] == LOT.qty_milli - 5_000
    assert station.api.commits == []


def test_the_idempotency_key_is_minted_at_identify_time_not_at_commit(
    station: Station,
) -> None:
    """The scan path's discipline: the key is attached when the container is
    identified, so a retried commit cannot double-move stock. Asserted by reading
    it off the *ready* frame, which is published before the user can have acted."""
    ready = body(station.place(TAG), events.STATION_READY)
    key = ready["client_op_id"]
    assert isinstance(key, str) and key

    proposed = body(take(station), events.STATION_PROPOSED)
    assert proposed["client_op_id"] == key

    committed = body(station.send(events.STATION_CONFIRM), events.STATION_COMMITTED)
    assert committed["client_op_id"] == key
    assert station.api.commits == [
        {"kind": "take", "lot_id": LOT.lot_id, "qty_milli": 5_000, "client_op_id": key}
    ]


def test_a_commit_loops_back_to_ready_with_the_new_balance(station: Station) -> None:
    station.place(TAG)
    take(station)
    emitted = station.send(events.STATION_CONFIRM)

    assert types(emitted) == [events.STATION_COMMITTED, events.STATION_READY]
    committed = body(emitted, events.STATION_COMMITTED)
    assert committed["seqs"] == [101]
    assert committed["replayed"] is False
    assert body(emitted, events.STATION_READY)["total_qty_milli"] == LOT.qty_milli - 5_000
    assert station.session.state is SessionState.READY
    assert station.session.pending is None
    # The new balance came from the movement response, not a second GET.
    assert station.api.read_calls == [CONTAINER.location_id]


def test_the_loop_back_to_action_neither_re_identifies_nor_re_commits(
    station: Station,
) -> None:
    """Two takes in one placement: one identification, two ledger operations under
    two different keys — a second commit under the first key would replay the first
    movement and silently move nothing."""
    station.place(TAG)
    take(station, 5_000)
    station.send(events.STATION_CONFIRM)
    station.advance(500)
    take(station, 3_000)
    station.send(events.STATION_CONFIRM)

    assert len(station.api.resolve_calls) == 1
    assert [commit["qty_milli"] for commit in station.api.commits] == [5_000, 3_000]
    keys = {commit["client_op_id"] for commit in station.api.commits}
    assert len(keys) == 2


def test_a_settled_container_produces_no_further_session_events(station: Station) -> None:
    """The station loops in `ACTION` while the tag stays put. Silence is the
    feature: an event per poll would let a client re-fire an action 3×/second."""
    station.place(TAG)
    assert station.place(TAG, times=20) == []


@pytest.mark.parametrize(
    ("kind", "qty", "expected"),
    [
        ("take", 5_000, LOT.qty_milli - 5_000),
        ("add", 5_000, LOT.qty_milli + 5_000),
        ("recount", 7_000, 7_000),
        ("recount", 0, 0),
    ],
)
def test_every_action_maps_to_an_existing_route(
    station: Station, kind: str, qty: int, expected: int
) -> None:
    """Three actions because three stock routes exist. `recount` admits zero — "I
    counted it and the bin is empty" is a real answer — where a take does not."""
    station.place(TAG)
    proposed = body(
        station.send(
            events.STATION_PROPOSE,
            action={"kind": kind, "lot_id": LOT.lot_id, "qty_milli": qty},
        ),
        events.STATION_PROPOSED,
    )
    assert proposed["projected_qty_milli"] == expected

    committed = body(station.send(events.STATION_CONFIRM), events.STATION_COMMITTED)
    assert committed["lot"]["qty_milli"] == expected
    assert station.api.commits[0]["kind"] == kind


# ---------------------------------------------------------------------------
# The guarantee: removal before COMMIT writes nothing
# ---------------------------------------------------------------------------


def test_removing_the_container_before_commit_writes_nothing(station: Station) -> None:
    """**The one that matters.** A half-finished session that commits is stock that
    moved without anyone saying so."""
    station.place(TAG)
    take(station)
    assert station.session.pending is not None

    emitted = station.lift()
    assert types(emitted) == [events.TAG_REMOVED, events.STATION_ABORTED]

    aborted = body(emitted, events.STATION_ABORTED)
    assert aborted["reason"] == "removed"
    assert aborted["discarded"] == {"kind": "take", "lot_id": LOT.lot_id, "qty_milli": 5_000}
    assert station.api.commits == []
    assert station.session.state is SessionState.IDLE
    assert station.session.pending is None
    assert station.session.client_op_id is None


def test_a_confirm_that_races_the_lift_is_refused(station: Station) -> None:
    """The tap and the lift happen together: the frame arrives holding the id of a
    session that has ended. Refusing it is what stops the user's last tap landing
    against whatever container is on the platform *now*."""
    station.place(TAG)
    take(station)
    stale = station.session.session_id
    station.lift()

    # Refused as `no_session` while the platform is empty and as `stale_session`
    # once the next container has landed. Two messages, one guarantee: the id the
    # client is holding is never the id of a session that could commit.
    emitted = station.send(events.STATION_CONFIRM, session_id=stale)
    assert body(emitted, events.STATION_REJECTED)["reason"] == "no_session"
    assert station.api.commits == []

    station.place(TAG)
    late = station.send(events.STATION_CONFIRM, session_id=stale)
    assert body(late, events.STATION_REJECTED)["reason"] == "stale_session"
    assert station.api.commits == []


def test_a_swap_aborts_the_first_session_without_writing(station: Station) -> None:
    """Two containers with no empty poll between them. Carrying a pending action
    across the change is a ledger row against the wrong bin."""
    station.place(TAG)
    take(station)
    emitted = station.place(UID_ONLY)

    assert types(emitted) == [
        events.TAG_REMOVED,
        events.STATION_ABORTED,
        events.TAG_IDENTIFIED,
        events.STATION_READY,
    ]
    assert body(emitted, events.STATION_ABORTED)["reason"] == "swapped"
    assert station.api.commits == []


def test_a_dropped_read_does_not_abort_the_session(station: Station) -> None:
    """The debounce is what makes the abort guarantee usable: a PN532 misses reads,
    and a flicker that discarded the user's work would teach them to distrust the
    bench."""
    station.place(TAG)
    take(station)
    assert station.lift(polls=2) == []
    assert station.session.pending is not None
    assert station.session.state is SessionState.PROPOSED


def test_a_reader_fault_does_not_abort_the_session(station: Station) -> None:
    """A container has not moved because a UART did."""
    station.place(TAG)
    take(station)
    assert types(station.fault()) == [events.TAG_ERROR]
    assert station.session.state is SessionState.PROPOSED
    assert station.api.commits == []


def test_cancel_discards_the_action_and_keeps_the_session(station: Station) -> None:
    station.place(TAG)
    take(station)
    emitted = station.send(events.STATION_CANCEL)

    assert types(emitted) == [events.STATION_READY]
    assert station.session.state is SessionState.READY
    assert station.api.commits == []
    # Idempotent: a second Cancel is not an error worth a screen.
    assert station.send(events.STATION_CANCEL) == []


# ---------------------------------------------------------------------------
# Retries, and the key
# ---------------------------------------------------------------------------


def test_a_failed_commit_keeps_the_action_and_its_key_for_a_retry(station: Station) -> None:
    """The response was lost, not the intent. Retrying the same key is safe by
    construction: if the request did land, the API replays its stored answer."""
    station.place(TAG)
    key = body(take(station), events.STATION_PROPOSED)["client_op_id"]
    station.api.fail_commit = ApiUnavailable("timed out")

    failure = body(station.send(events.STATION_CONFIRM), events.STATION_FAILED)
    assert failure["reason"] == "api_unavailable"
    assert failure["action"] == {"kind": "take", "lot_id": LOT.lot_id, "qty_milli": 5_000}
    assert station.session.state is SessionState.PROPOSED

    station.api.fail_commit = None
    station.advance(500)
    committed = body(station.send(events.STATION_CONFIRM), events.STATION_COMMITTED)
    assert committed["client_op_id"] == key
    assert len(station.api.commits) == 1


def test_editing_a_proposal_after_a_failed_commit_mints_a_new_key(station: Station) -> None:
    """A key reused for a different body is a 409 `request_mismatch` by design, so
    a changed proposal is a new operation and needs a new key."""
    station.place(TAG)
    first = body(take(station, 5_000), events.STATION_PROPOSED)["client_op_id"]
    station.api.fail_commit = ApiError("boom", status=500, reason="server_error")
    station.send(events.STATION_CONFIRM)
    station.api.fail_commit = None

    station.advance(500)
    second = body(take(station, 9_000), events.STATION_PROPOSED)["client_op_id"]
    assert second != first

    station.advance(500)
    committed = body(station.send(events.STATION_CONFIRM), events.STATION_COMMITTED)
    assert committed["client_op_id"] == second
    assert station.api.commits[0]["qty_milli"] == 9_000


def test_a_server_refusal_is_reported_with_its_own_reason(station: Station) -> None:
    """`reason` comes from the API's error body, so the station never invents a
    vocabulary for a refusal it did not make."""
    station.place(TAG)
    take(station)
    station.api.fail_commit = ApiError("lot is closed", status=409, reason="lot_closed")

    failure = body(station.send(events.STATION_CONFIRM), events.STATION_FAILED)
    assert failure["reason"] == "lot_closed"
    assert failure["message"] == "lot is closed"


# ---------------------------------------------------------------------------
# The 400 ms hold-off
# ---------------------------------------------------------------------------


def test_a_double_tapped_commit_is_one_commit(station: Station) -> None:
    """Two taps on Commit. The second finds the action already committed.

    The state guard carries this one, not the hold-off: confirming clears the
    pending action, so a second tap has nothing to confirm. Worth pinning anyway —
    it is the physical gesture most likely to duplicate a ledger row, and the
    rejection is what proves it cannot.
    """
    station.place(TAG)
    take(station)
    assert types(station.send(events.STATION_CONFIRM)) == [
        events.STATION_COMMITTED,
        events.STATION_READY,
    ]
    station.advance(100)
    second = station.send(events.STATION_CONFIRM)
    assert body(second, events.STATION_REJECTED)["reason"] == "nothing_pending"
    assert len(station.api.commits) == 1


def test_impatient_taps_against_a_failing_api_do_not_become_a_retry_storm(
    station: Station,
) -> None:
    """Where the confirm hold-off actually bites. A failed commit *keeps* the
    pending action so a retry is possible, which means the Commit button stays live
    — and a user who taps it four times while the API is down should send one
    request, not four. PLAN.md: duplicates inside the window are dropped "by the
    same debounce as the decoder"."""
    station.place(TAG)
    take(station)
    station.api.fail_commit = ApiUnavailable("timed out")

    assert types(station.send(events.STATION_CONFIRM)) == [events.STATION_FAILED]
    for _ in range(3):
        assert station.send(events.STATION_CONFIRM) == []
    assert station.api.commit_attempts == 1

    station.advance(401)
    assert types(station.send(events.STATION_CONFIRM)) == [events.STATION_FAILED]
    assert station.api.commit_attempts == 2


def test_a_double_tapped_stepper_is_one_proposal(station: Station) -> None:
    """Quantities are absolute, never deltas, which is what makes an identical
    repeat recognisable as a duplicate rather than a second increment."""
    station.place(TAG)
    assert types(take(station)) == [events.STATION_PROPOSED]
    assert take(station) == []
    station.advance(401)
    assert types(take(station)) == [events.STATION_PROPOSED]


def test_a_deliberate_repeat_after_the_window_is_honoured(station: Station) -> None:
    """Take 5, commit, take 5 again — the same action twice on purpose. The window
    is keyed on the idempotency key as well as the action, so the second one is a
    different operation and is admitted."""
    station.place(TAG)
    take(station)
    station.send(events.STATION_CONFIRM)
    station.advance(401)
    take(station)
    assert types(station.send(events.STATION_CONFIRM)) == [
        events.STATION_COMMITTED,
        events.STATION_READY,
    ]
    assert len(station.api.commits) == 2


# ---------------------------------------------------------------------------
# The command surface
# ---------------------------------------------------------------------------


def test_a_command_cannot_name_a_lot_the_agent_never_announced(station: Station) -> None:
    """The vocabulary is "the lot you told me about", not "lot 4173". That is what
    keeps a loopback socket from being a way to move any stock in the system."""
    station.place(TAG)
    emitted = station.send(
        events.STATION_PROPOSE, action={"kind": "take", "lot_id": 4173, "qty_milli": 1}
    )
    assert body(emitted, events.STATION_REJECTED)["reason"] == "unknown_lot"
    assert station.api.commits == []


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ({"kind": "take", "lot_id": LOT.lot_id, "qty_milli": 0}, "bad_quantity"),
        ({"kind": "take", "lot_id": LOT.lot_id, "qty_milli": -5}, "bad_quantity"),
        ({"kind": "take", "lot_id": LOT.lot_id, "qty_milli": 10**13}, "bad_quantity"),
        ({"kind": "recount", "lot_id": LOT.lot_id, "qty_milli": -1}, "bad_quantity"),
        ({"kind": "empty", "lot_id": LOT.lot_id, "qty_milli": 1}, "unknown_action"),
        ({"kind": "take", "lot_id": 0, "qty_milli": 1}, "bad_action"),
        ({"kind": "take", "lot_id": "41", "qty_milli": 1}, "bad_action"),
        ({"kind": "take", "lot_id": LOT.lot_id, "qty_milli": True}, "bad_action"),
        ("take five", "bad_action"),
    ],
)
def test_a_nonsense_action_is_refused_before_the_network(
    station: Station, action: object, reason: str
) -> None:
    """The API's own bounds (`app.api.limits`), enforced at the bench so a
    fat-fingered keypad entry costs no round trip and never reaches the ledger's
    accumulating cache."""
    station.place(TAG)
    emitted = station.send(events.STATION_PROPOSE, action=action)
    assert body(emitted, events.STATION_REJECTED)["reason"] == reason
    assert station.api.commits == []


def test_commands_are_refused_when_nothing_is_on_the_platform(station: Station) -> None:
    assert (
        body(station.send(events.STATION_CONFIRM, session_id="whatever"), events.STATION_REJECTED)[
            "reason"
        ]
        == "no_session"
    )


def test_a_command_without_a_session_id_is_refused(station: Station) -> None:
    station.place(TAG)
    emitted = station.raw(json.dumps({"type": events.STATION_CONFIRM}))
    assert body(emitted, events.STATION_REJECTED)["reason"] == "missing_session_id"


def test_a_confirm_with_nothing_pending_is_refused(station: Station) -> None:
    station.place(TAG)
    emitted = station.send(events.STATION_CONFIRM)
    assert body(emitted, events.STATION_REJECTED)["reason"] == "nothing_pending"
    assert station.api.commits == []


def test_an_action_cannot_be_proposed_before_the_container_is_known(station: Station) -> None:
    station.api.fail_resolve = ApiUnavailable("down")
    station.place(TAG)
    emitted = take(station)
    assert body(emitted, events.STATION_REJECTED)["reason"] == "not_ready"


def test_refresh_is_refused_while_an_action_is_pending(station: Station) -> None:
    """A proposal reviewed against one balance and committed against another is
    exactly the confusion the confirm step exists to prevent."""
    station.place(TAG)
    take(station)
    emitted = station.send(events.STATION_REFRESH)
    assert body(emitted, events.STATION_REJECTED)["reason"] == "action_pending"


def test_a_spent_identify_budget_is_not_rescued_by_refresh_only_by_a_re_seat(
    station: Station,
) -> None:
    """**The defect:** the as-built diagram drew `refresh ──▶ RESOLVING` under the
    `budget spent` branch, which is the one branch that cannot take it.

    `_unreadable` calls `_reset()`, so this session holds no carrier, so `_refresh`
    falls past the `identity.is_identified` arm to `nothing_to_resolve`. The edge
    belongs to the *other* route into `UNIDENTIFIED` — a tag that read cleanly and
    that the server has no binding for, which
    `test_provisioning_then_refreshing_reaches_ready_without_lifting_the_container`
    covers.

    The shape is why it matters: a drawn-but-untriggerable transition is exactly what
    deleting `CONTAINER_DETECTED` avoided, and it is worse in a diagram labelled
    "as built" than in PLAN.md. So both halves are asserted here — the refusal, and
    the documented way back, which is only honest advice if it works.
    """
    station.place(UNREADABLE, times=5)
    stranded = station.session.session_id
    assert station.session.state is SessionState.UNIDENTIFIED
    # Nothing to re-ask the server about, which is the whole reason refresh fails.
    assert station.session.identity is None

    refused = body(station.send(events.STATION_REFRESH), events.STATION_REJECTED)
    assert refused["reason"] == "nothing_to_resolve"
    assert station.session.state is SessionState.UNIDENTIFIED
    assert station.api.resolve_calls == []

    # The way back: provision the tag, set the container down again. Presence takes
    # its UNIDENTIFIED → IDENTIFIED re-seat edge with no teardown, and this is a
    # fresh placement with a fresh session id — not the stranded one resurrected.
    emitted = station.place(TAG)
    assert types(emitted) == [events.TAG_IDENTIFIED, events.STATION_READY]
    assert station.session.state is SessionState.READY
    assert station.session.session_id != stranded


def test_refresh_picks_up_a_balance_somebody_else_changed(station: Station) -> None:
    station.place(TAG)
    station.api.container = CONTAINER.with_lot(
        LotView(
            lot_id=LOT.lot_id,
            part_id=LOT.part_id,
            qty_milli=1_000,
            qty_reserved_milli=0,
            status="active",
            batch_code=LOT.batch_code,
        )
    )
    ready = body(station.send(events.STATION_REFRESH), events.STATION_READY)
    assert ready["total_qty_milli"] == 1_000


@pytest.mark.parametrize(
    "frame",
    [
        "not json at all",
        "[]",
        '"a string"',
        '{"no_type": 1}',
        '{"type": "station.ready", "session_id": "x"}',
        '{"type": 42}',
        '{"type": "tag.identified"}',
    ],
)
def test_a_frame_that_is_not_a_command_produces_no_event(station: Station, frame: str) -> None:
    """Junk is logged, not broadcast. A refused *command* is operational truth the
    user needs on screen; a malformed frame is a programmer error, and letting one
    paint the bench display hands any page on the loopback a way to shout at the
    kiosk. Note `station.ready` — an event echoed back by a confused client — is
    dropped for the same reason."""
    station.place(TAG)
    assert station.raw(frame) == []
    assert station.api.commits == []


def test_an_oversized_frame_is_dropped_before_it_is_parsed(station: Station) -> None:
    station.place(TAG)
    assert station.raw("x" * (MAX_FRAME_BYTES + 1)) == []


def test_the_whole_placement_emits_no_weight_event(station: Station) -> None:
    """End to end: land, take, commit, lift. Not one `weight.*`, so the PWA hides
    every by-weight affordance without a feature flag anywhere."""
    emitted = station.place(TAG)
    take(station)
    emitted += station.send(events.STATION_CONFIRM)
    emitted += station.lift()
    assert not [event for event in emitted if event.type.startswith("weight")]


def test_the_fake_api_is_a_station_api() -> None:
    """Structural check, so the double cannot drift from the protocol the real
    client implements — the same arrangement `FakeTagSource` has with `TagSource`."""
    from agent.api import StationApi

    fake: StationApi = FakeStationApi()
    assert fake is not None


# ---------------------------------------------------------------------------
# The three things that make an abort an abort
# ---------------------------------------------------------------------------


def test_lifting_the_container_mid_commit_does_not_race_the_commit(
    station: Station, api: FakeStationApi
) -> None:
    """The `asyncio.Lock`, which nothing else in this suite can see.

    Every other test drives one `run_until_complete` at a time, so two coroutines
    never overlap and the lock is indistinguishable from no lock: replacing both
    `async with self._lock:` blocks with `if True:` leaves the whole suite green.

    Here a commit is held open at the API while a **`tag.removed`** arrives — the
    container lifted off the reader with a movement in flight, which is the
    sequence the lock exists for. Serialised, the commit finishes and the teardown
    runs after it. Unserialised, the teardown reaches session state the commit is
    still using, and the movement's own bookkeeping lands on a session that has
    already been cleared.
    """
    station.place(TAG)
    api.gate = asyncio.Event()
    take(station)

    async def confirm_slowly() -> list[Event]:
        return await station.session.handle_frame(
            json.dumps({"type": events.STATION_CONFIRM, "session_id": station.session.session_id})
        )

    async def lift_it_mid_flight() -> list[Event]:
        # Let the confirm get as far as the blocked API call, then tear the
        # session down underneath it and release the commit.
        await asyncio.sleep(0)
        removed = asyncio.ensure_future(
            station.session.on_presence(Event(type=events.TAG_REMOVED, data={"missed_polls": 0}))
        )
        await asyncio.sleep(0)
        api.gate.set()
        return await removed

    confirmed, lifted = station.gather(confirm_slowly(), lift_it_mid_flight())

    assert len(api.commits) == 1, api.commits
    # **The confirm finishes its own sequence.** Serialised it emits the movement
    # and then the screen to stand on; with the lock removed the teardown lands
    # between the two, `station.ready` is dropped, and the session logs
    # "station.ready with no resolved placement" — so the bench is left looking
    # at a committed movement with no ready state behind it, on a session that
    # has already been cleared.
    assert types(confirmed) == [events.STATION_COMMITTED, events.STATION_READY]
    assert types(lifted) == [events.STATION_ABORTED]


def test_cancelling_after_a_lost_response_does_not_replay_the_first_movement(
    station: Station, api: FakeStationApi
) -> None:
    """The worst case in the file, and it had no test.

    The commit **lands** and the reply is lost. The user cancels, takes five
    more, and confirms an identical action. Without `_cancel` rotating the
    idempotency key, the second confirm carries the key the server already has —
    so the server replays the first movement, the agent reports success, and the
    second five units are never recorded. Deleting the rotation branch leaves the
    whole suite green.

    `lose_response` is what makes this expressible: `fail_commit` raises *before*
    the row is recorded, which is the harmless half of the same shape.
    """
    station.place(TAG)
    api.lose_response = True
    take(station)
    station.send(events.STATION_CONFIRM)
    assert len(api.commits) == 1
    first_key = api.commits[0]["client_op_id"]

    station.send(events.STATION_CANCEL)
    api.lose_response = False
    station.advance(500)
    take(station)
    station.send(events.STATION_CONFIRM)

    assert len(api.commits) == 2, "the retry replayed the first movement instead of recording one"
    assert api.commits[1]["client_op_id"] != first_key
