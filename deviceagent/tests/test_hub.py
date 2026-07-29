"""Fan-out, sequence numbers, and what a client that connects late is told.

No socket in this file. Everything worth deciding about the stream is in the hub,
so it is tested against a list; `tests/test_ws_loopback.py` checks the twenty
lines that put it on a WebSocket.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from agent import events
from agent.events import Event
from agent.hub import EventHub, Sink

AT = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)


class Recorder:
    """A `Sink` that keeps what it was sent."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict[str, object]] = []
        self.fail = fail

    async def send(self, message: str) -> None:
        if self.fail:
            raise ConnectionResetError("the kiosk tab was closed")
        self.sent.append(json.loads(message))

    def types(self) -> list[str]:
        return [str(message["type"]) for message in self.sent]


def hub() -> EventHub:
    return EventHub(agent_version="1.2.3", clock=lambda: AT)


def test_a_sink_is_greeted_on_attach() -> None:
    async def scenario() -> Recorder:
        subject, sink = hub(), Recorder()
        await subject.attach(sink)
        return sink

    sink = asyncio.run(scenario())
    assert sink.types() == [events.STATION_HELLO]
    assert sink.sent[0]["seq"] == 0


def test_seq_is_monotonic_across_the_stream() -> None:
    async def scenario() -> Recorder:
        subject, sink = hub(), Recorder()
        await subject.attach(sink)
        for index in range(3):
            await subject.publish(events.tag_reading(poll=index + 1, of=5))
        return sink

    sink = asyncio.run(scenario())
    assert [message["seq"] for message in sink.sent] == [0, 1, 2, 3]


def test_every_attached_sink_gets_every_event() -> None:
    async def scenario() -> tuple[Recorder, Recorder]:
        subject, first, second = hub(), Recorder(), Recorder()
        await subject.attach(first)
        await subject.attach(second)
        await subject.publish(events.tag_removed(missed_polls=3))
        return first, second

    first, second = asyncio.run(scenario())
    assert first.types() == second.types() == [events.STATION_HELLO, events.TAG_REMOVED]


def test_a_client_that_connects_mid_placement_is_told_what_is_on_the_platform() -> None:
    """A kiosk reload must not need the user to lift the container and put it back
    down. The replay keeps the event's ORIGINAL seq, which is the whole dedupe
    mechanism: a client that already processed seq 1 ignores it, a fresh one
    renders it."""

    async def scenario() -> tuple[Recorder, dict[str, object]]:
        subject = hub()
        first = Recorder()
        await subject.attach(first)
        published = await subject.publish(Event(events.TAG_IDENTIFIED, {"short_id": "4K7T92M8"}))
        late = Recorder()
        await subject.attach(late)
        return late, published

    late, published = asyncio.run(scenario())
    assert late.types() == [events.STATION_HELLO, events.TAG_IDENTIFIED]
    assert late.sent[1]["seq"] == published["seq"]
    assert late.sent[0]["data"] == {
        "protocol": events.PROTOCOL_VERSION,
        "agent": "almagest-deviceagent/1.2.3",
        "last_seq": published["seq"],
    }


def test_a_removal_clears_the_replay() -> None:
    """Otherwise a browser opened three hours later renders a container that is
    back in its cabinet."""

    async def scenario() -> Recorder:
        subject = hub()
        await subject.publish(Event(events.TAG_IDENTIFIED, {"short_id": "4K7T92M8"}))
        await subject.publish(events.tag_removed(missed_polls=3))
        late = Recorder()
        await subject.attach(late)
        return late

    assert asyncio.run(scenario()).types() == [events.STATION_HELLO]


def test_an_unidentified_container_is_also_replayed() -> None:
    """`tag.timeout` is a state — something *is* on the platform — so a client that
    connects during it must see the "provision this container" prompt too."""

    async def scenario() -> Recorder:
        subject = hub()
        await subject.publish(events.tag_timeout(polls=5))
        late = Recorder()
        await subject.attach(late)
        return late

    assert asyncio.run(scenario()).types() == [events.STATION_HELLO, events.TAG_TIMEOUT]


def test_a_transient_event_is_never_replayed() -> None:
    async def scenario() -> Recorder:
        subject = hub()
        await subject.publish(events.tag_reading(poll=2, of=5))
        late = Recorder()
        await subject.attach(late)
        return late

    assert asyncio.run(scenario()).types() == [events.STATION_HELLO]


def test_one_dead_subscriber_does_not_stop_the_station() -> None:
    """A closed kiosk tab is the ordinary case; an exception escaping the fan-out
    would propagate into the poll loop and take the reader down with it."""

    async def scenario() -> tuple[EventHub, Recorder]:
        subject = hub()
        broken, healthy = Recorder(fail=True), Recorder()
        await subject.attach(broken)
        await subject.attach(healthy)
        await subject.publish(events.tag_removed(missed_polls=3))
        return subject, healthy

    subject, healthy = asyncio.run(scenario())
    assert healthy.types() == [events.STATION_HELLO, events.TAG_REMOVED]
    assert subject.subscriber_count == 1


def test_a_sink_that_fails_its_greeting_is_never_attached() -> None:
    async def scenario() -> EventHub:
        subject = hub()
        await subject.attach(Recorder(fail=True))
        return subject

    assert asyncio.run(scenario()).subscriber_count == 0


def test_detach_is_idempotent() -> None:
    """Called from a `finally` on a path that may already have dropped the sink."""

    async def scenario() -> int:
        subject, sink = hub(), Recorder()
        await subject.attach(sink)
        subject.detach(sink)
        subject.detach(sink)
        return subject.subscriber_count

    assert asyncio.run(scenario()) == 0


def test_a_recorder_is_a_sink() -> None:
    """Keeps the test double honest against the protocol it stands in for."""
    assert isinstance(Recorder(), Sink)
