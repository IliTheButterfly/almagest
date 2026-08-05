"""The wire shape, and the naming parity that keeps the scale additive.

PLAN.md fixes `weight.reading` / `weight.stable` / `weight.timeout` /
`weight.error` / `weight.zeroed`. If the tag half of the vocabulary drifts from
that, adding the load cell later (ADR 0003 calls the deferral reversible for
~$25) becomes a protocol change and every client has to learn two grammars. This
file is what stops that drift being a code review's job.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agent import events
from agent.api import Action, ActionKind
from agent.events import Event, envelope, to_json
from agent.identity import identify
from agent.tags import TagRead
from tests.fake_api import CONTAINER, LOT, RESOLVED

#: PLAN.md's scale vocabulary, verbatim.
WEIGHT_TYPES = (
    "weight.reading",
    "weight.stable",
    "weight.timeout",
    "weight.error",
    "weight.zeroed",
)

TAG_TYPES = (
    events.TAG_READING,
    events.TAG_IDENTIFIED,
    events.TAG_TIMEOUT,
    events.TAG_ERROR,
    events.TAG_REMOVED,
)

#: The session half. Held as a tuple so the `<device>.<verb>` grammar covers them
#: too — `station.propose` and friends are checked in `COMMAND_TYPES`.
STATION_TYPES = (
    events.STATION_HELLO,
    events.STATION_READY,
    events.STATION_PROPOSED,
    events.STATION_UNIDENTIFIED,
    events.STATION_COMMITTED,
    events.STATION_ABORTED,
    events.STATION_REJECTED,
    events.STATION_FAILED,
)

AT = datetime(2026, 7, 29, 1, 2, 3, 456000, tzinfo=UTC)

ACTION = Action(kind=ActionKind.TAKE, lot_id=LOT.lot_id, qty_milli=5_000)


@pytest.fixture
def station_events() -> list[Event]:
    """One of each session event, built through the constructors.

    Listed by hand rather than discovered, so adding an eighth event means adding
    it here — which is the moment to decide whether it is sticky, whether it
    clears, and whether it carries `state`.
    """
    return [
        events.station_ready(
            state="ready",
            session_id="s",
            client_op_id="k",
            container=CONTAINER,
            resolution=RESOLVED,
        ),
        events.station_proposed(
            state="proposed",
            session_id="s",
            client_op_id="k",
            action=ACTION,
            projected_qty_milli=1,
        ),
        events.station_unidentified(
            state="unidentified", session_id="s", reason="unreadable", identity=None
        ),
        events.station_committed(
            state="ready",
            session_id="s",
            client_op_id="k",
            action=ACTION,
            seqs=(7,),
            lot=LOT,
            replayed=False,
        ),
        events.station_aborted(state="idle", session_id="s", reason="removed", discarded=ACTION),
        events.station_rejected(
            state="idle", session_id=None, reason="no_session", message="nothing is there"
        ),
        events.station_failed(
            state="proposed",
            session_id="s",
            reason="api_unavailable",
            message="timed out",
            action=ACTION,
        ),
    ]


@pytest.mark.parametrize("event_type", (*TAG_TYPES, *STATION_TYPES, *events.COMMAND_TYPES))
def test_every_type_is_a_device_and_a_verb(event_type: str) -> None:
    device, _, verb = event_type.partition(".")
    assert device in {"tag", "station"}
    assert verb and "." not in verb


def test_the_tag_verbs_are_the_scales_verbs() -> None:
    """`reading`/`timeout`/`error` are shared outright; `identified` is `stable`'s
    twin under a name that means something for an identifier; `removed` has no
    scale analogue because a scale never leaves the bench."""
    weight_verbs = {name.split(".", 1)[1] for name in WEIGHT_TYPES}
    tag_verbs = {name.split(".", 1)[1] for name in TAG_TYPES}
    assert {"reading", "timeout", "error"} <= weight_verbs & tag_verbs
    assert tag_verbs - weight_verbs == {"identified", "removed"}
    assert weight_verbs - tag_verbs == {"stable", "zeroed"}


def test_no_weight_event_is_defined_here() -> None:
    """ADR 0003: there is no scale, so no `weight.*` is ever emitted, and the PWA
    hides every by-weight affordance because none arrived. Defining the constants
    "ready for later" would be the first step towards emitting a placeholder."""
    assert not [name for name in dir(events) if name.startswith("WEIGHT")]


def test_the_envelope_carries_type_seq_time_and_body() -> None:
    message = envelope(events.tag_timeout(polls=5), seq=7, at=AT)
    assert message == {
        "type": "tag.timeout",
        "seq": 7,
        "at": "2026-07-29T01:02:03.456000Z",
        "data": {"polls": 5},
    }


def test_the_timestamp_is_utc_with_a_z_like_every_other_timestamp_in_the_system() -> None:
    """A station log correlated by hand against the ledger is a thing that will
    happen, and two timestamp formats is what makes that an afternoon."""
    local = datetime(2026, 7, 29, 3, 2, 3, tzinfo=UTC).astimezone()
    assert envelope(Event("tag.error"), seq=1, at=local)["at"].endswith("Z")
    assert "+" not in str(envelope(Event("tag.error"), seq=1, at=local)["at"])


def test_hello_reports_the_protocol_and_the_highest_seq_so_far() -> None:
    hello = events.station_hello(agent_version="9.9.9", last_seq=41)
    assert hello.data["protocol"] == events.PROTOCOL_VERSION
    assert hello.data["agent"] == "almagest-deviceagent/9.9.9"
    assert hello.data["last_seq"] == 41


def test_hello_does_not_enumerate_which_devices_exist() -> None:
    """The obvious place for `{"sources": ["tag"]}`, and exactly the feature flag
    ADR 0003 says not to build: an affordance is drawn because an event arrived,
    not because a capability list permitted it."""
    hello = events.station_hello(agent_version="1", last_seq=0)
    assert set(hello.data) == {"protocol", "agent", "last_seq"}


def test_identified_carries_both_carriers_verbatim() -> None:
    """The PWA posts both to `/api/location-tags/resolve`, and only the server,
    seeing both, can report that a tag's payload and its binding disagree."""
    url = "https://almagest.aether.lan/s/4K7T92M8"
    event = events.tag_identified(identify(TagRead(uid="04:1A:2B", ndef_url=url)))
    assert event.data == {
        "short_id": "4K7T92M8",
        "tag_uid": "041A2B",
        "ndef_url": url,
        "via": "ndef",
    }


def test_only_state_events_are_replayable() -> None:
    """`tag.reading` is stale the moment the next poll happens, so replaying it to
    a reconnecting client would show a spinner that never resolves — and neither is
    `station.committed`, which a reconnecting client would re-render as a movement
    that had just happened. The `station.ready` following every commit carries the
    balance it produced, which is the part still true an hour later."""
    assert set(events.STICKY_TYPES) == {
        events.TAG_IDENTIFIED,
        events.TAG_TIMEOUT,
        events.STATION_READY,
        events.STATION_PROPOSED,
        events.STATION_UNIDENTIFIED,
    }


def test_the_events_that_end_a_session_clear_the_replayable_slot() -> None:
    """Both say the platform is clear: one from the reader, one from the session that
    the removal ended. A sticky frame left behind is a kiosk reload rendering a
    container that is back in its cabinet."""
    assert set(events.CLEARING_TYPES) == {events.TAG_REMOVED, events.STATION_ABORTED}
    assert not events.CLEARING_TYPES & events.STICKY_TYPES


def test_commands_are_imperative_and_events_are_not() -> None:
    """The grammar that makes a frame's direction readable without a table. It is
    also what stops an event echoed back by a confused client being mistaken for a
    command: the two vocabularies are disjoint."""
    assert {
        "station.propose",
        "station.confirm",
        "station.cancel",
        "station.refresh",
        # ADR 0014's one addition. `tag.write` is imperative and `tag.written` is
        # past tense, which is the same rule the four above follow.
        "tag.write",
    } == events.COMMAND_TYPES
    session_events = {
        events.STATION_READY,
        events.STATION_PROPOSED,
        events.STATION_UNIDENTIFIED,
        events.STATION_COMMITTED,
        events.STATION_ABORTED,
        events.STATION_REJECTED,
        events.STATION_FAILED,
        events.STATION_HELLO,
    }
    bridge_events = {
        events.DEVICE_ATTACHED,
        events.DEVICE_DETACHED,
        events.DEVICE_ERROR,
        events.TAG_SEEN,
        events.TAG_WRITING,
        events.TAG_WRITTEN,
        events.TAG_WRITE_REFUSED,
        events.TAG_WRITE_FAILED,
    }
    assert not events.COMMAND_TYPES & (session_events | bridge_events)
    # The near-miss worth guarding: `tag.write` the command and `tag.writing` the
    # event differ by three letters, and a client that sent the latter would
    # otherwise be silently ignored rather than told.
    assert events.TAG_WRITE not in bridge_events


def test_every_session_event_carries_the_state(station_events: list[Event]) -> None:
    """A kiosk renders off the last frame it received, whatever that frame was. A
    client that had to map seven event types onto seven states would be keeping a
    second copy of the state machine."""
    for event in station_events:
        assert event.type.startswith("station.")
        assert isinstance(event.data.get("state"), str), event.type


def test_the_wire_form_is_compact_json() -> None:
    message = envelope(events.tag_reading(poll=1, of=5), seq=1, at=AT)
    text = to_json(message)
    assert ", " not in text and ": " not in text
    assert json.loads(text) == message
