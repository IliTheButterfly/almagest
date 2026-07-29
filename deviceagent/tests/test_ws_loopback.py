"""End-to-end over a real socket: fake reader → poll loop → WebSocket → commands.

This is not a hardware test — it binds 127.0.0.1 and talks to itself, which any
CI runner can do — but it is the only place the wiring is exercised: that
`serve_events` really serves, that a client really receives the envelopes, that
`poll_forever` really folds a script into them, and that a command frame really
reaches the session and comes back as an event to *every* subscriber. Everything
above it is unit tested; this checks the seams between those units.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

from websockets.asyncio.client import connect

from agent import events
from agent.config import AgentSettings
from agent.fake_tags import FakeTagSource, ScriptedPoll, load_script
from agent.hub import EventHub
from agent.main import poll_forever, run
from agent.presence import TagPresence
from agent.session import StationSession
from agent.tags import TagRead
from agent.ws import serve_events
from tests.fake_api import LOT, FakeStationApi

#: Generous, because a hung test is worse than a slow one, and the loop under
#: test polls with a near-zero interval.
TIMEOUT_S = 5.0


async def collect(port: int, *, count: int) -> list[dict[str, Any]]:
    async with connect(f"ws://127.0.0.1:{port}") as client:
        return [json.loads(await client.recv()) for _ in range(count)]


def test_a_client_receives_the_stream_a_scripted_session_produces() -> None:
    async def scenario() -> list[dict[str, Any]]:
        hub = EventHub()
        presence = TagPresence(identify_polls=5, absent_polls=3)
        session = StationSession(FakeStationApi())
        source = FakeTagSource(load_script())
        # An exit stack rather than nested `async with`, because the client's URL
        # is only known once the server has bound its port.
        async with AsyncExitStack() as stack:
            port = await stack.enter_async_context(serve_events(hub, host="127.0.0.1", port=0))
            client = await stack.enter_async_context(connect(f"ws://127.0.0.1:{port}"))
            hello = json.loads(await client.recv())
            # Interval 0: the script's timing is expressed in polls, not seconds,
            # so there is nothing to wait for.
            await poll_forever(
                source, presence, session, hub, interval_s=0, max_polls=len(load_script())
            )
            received = [hello]
            while True:
                try:
                    received.append(json.loads(await asyncio.wait_for(client.recv(), 0.2)))
                except TimeoutError:
                    return received

        raise AssertionError("unreachable")

    received = asyncio.run(asyncio.wait_for(scenario(), TIMEOUT_S))
    assert received[0]["type"] == events.STATION_HELLO
    kinds = [message["type"] for message in received[1:]]
    # The presence stream `test_presence.py` asserts event-for-event, having
    # survived the envelope, JSON and a socket — now interleaved with the session
    # events each placement causes, in that order: the kiosk renders the local
    # parse first and the resolved container when the round trip returns.
    assert kinds[0] == events.TAG_IDENTIFIED
    assert kinds[1] == events.STATION_READY
    assert events.TAG_TIMEOUT in kinds
    assert events.STATION_ABORTED in kinds
    assert kinds[-1] == events.STATION_ABORTED
    assert [message["seq"] for message in received[1:]] == list(range(1, len(kinds) + 1))


def test_two_clients_see_the_same_events() -> None:
    """The kiosk and a laptop debugging it, at once."""

    async def scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        hub = EventHub()
        async with AsyncExitStack() as stack:
            port = await stack.enter_async_context(serve_events(hub, host="127.0.0.1", port=0))
            first = await stack.enter_async_context(connect(f"ws://127.0.0.1:{port}"))
            second = await stack.enter_async_context(connect(f"ws://127.0.0.1:{port}"))
            for message in (json.loads(await first.recv()), json.loads(await second.recv())):
                assert message["type"] == events.STATION_HELLO
            await hub.publish(events.tag_timeout(polls=5))
            return (
                [json.loads(await first.recv())],
                [json.loads(await second.recv())],
            )
        raise AssertionError("unreachable")

    first, second = asyncio.run(asyncio.wait_for(scenario(), TIMEOUT_S))
    assert first == second
    assert first[0]["type"] == events.TAG_TIMEOUT


def test_a_socket_with_no_handler_wired_drops_everything_a_client_sends() -> None:
    """The default is still read-and-discard, and that is a wiring decision.

    `serve_events` grows a command surface only when `agent.main` hands it one, so
    a socket with no handler cannot be talked into anything — and a client that
    sends must neither be obeyed nor disconnect the others."""

    async def scenario() -> str:
        hub = EventHub()
        async with AsyncExitStack() as stack:
            port = await stack.enter_async_context(serve_events(hub, host="127.0.0.1", port=0))
            client = await stack.enter_async_context(connect(f"ws://127.0.0.1:{port}"))
            await client.recv()
            await client.send('{"type":"tag.identified","data":{"short_id":"MADEUP01"}}')
            await client.send("not json at all")
            await hub.publish(events.tag_removed(missed_polls=3))
            return str(json.loads(await client.recv())["type"])
        raise AssertionError("unreachable")

    assert asyncio.run(asyncio.wait_for(scenario(), TIMEOUT_S)) == events.TAG_REMOVED


def test_a_disconnected_client_is_detached() -> None:
    async def scenario() -> int:
        hub = EventHub()
        async with serve_events(hub, host="127.0.0.1", port=0) as port:
            async with connect(f"ws://127.0.0.1:{port}") as client:
                await client.recv()
                assert hub.subscriber_count == 1
            # Closing is asynchronous at the server end; give the handler's
            # `finally` a chance to run rather than sleeping a fixed time.
            for _ in range(100):
                if hub.subscriber_count == 0:
                    break
                await asyncio.sleep(0.01)
            return hub.subscriber_count

    assert asyncio.run(asyncio.wait_for(scenario(), TIMEOUT_S)) == 0


def test_a_command_frame_reaches_the_session_and_answers_every_subscriber() -> None:
    """The whole inbound path, over a socket: propose → `station.proposed`, seen by
    both clients.

    Answered to everyone rather than privately to the sender, because two kiosk tabs
    looking at one bench must not disagree about what is pending.
    """

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        hub = EventHub()
        presence = TagPresence(identify_polls=5, absent_polls=3)
        session = StationSession(FakeStationApi())

        async def on_frame(raw: str) -> None:
            for event in await session.handle_frame(raw):
                await hub.publish(event)

        async with AsyncExitStack() as stack:
            port = await stack.enter_async_context(
                serve_events(hub, host="127.0.0.1", port=0, on_frame=on_frame)
            )
            first = await stack.enter_async_context(connect(f"ws://127.0.0.1:{port}"))
            second = await stack.enter_async_context(connect(f"ws://127.0.0.1:{port}"))
            await first.recv()
            await second.recv()

            source = FakeTagSource([ScriptedPoll(read=TagRead(uid="041A2B3C", ndef_url=None))])
            await poll_forever(source, presence, session, hub, interval_s=0, max_polls=1)
            for _ in range(2):  # tag.identified, station.ready
                await first.recv()
                await second.recv()

            await first.send(
                json.dumps(
                    {
                        "type": events.STATION_PROPOSE,
                        "session_id": session.session_id,
                        "action": {"kind": "take", "lot_id": LOT.lot_id, "qty_milli": 5_000},
                    }
                )
            )
            return (
                json.loads(await asyncio.wait_for(first.recv(), 2.0)),
                json.loads(await asyncio.wait_for(second.recv(), 2.0)),
            )
        raise AssertionError("unreachable")

    first, second = asyncio.run(asyncio.wait_for(scenario(), TIMEOUT_S))
    assert first == second
    assert first["type"] == events.STATION_PROPOSED
    assert first["data"]["action"]["qty_milli"] == 5_000


def test_run_closes_the_reader_on_the_way_out() -> None:
    """`run` owns the source's lifetime, and a serial port left open is a port the
    next start cannot have."""
    source = FakeTagSource(load_script())
    settings = AgentSettings(ws_port=0, poll_interval_ms=20)
    asyncio.run(
        asyncio.wait_for(run(source, settings, api=FakeStationApi(), max_polls=3), TIMEOUT_S)
    )
    assert source.closed
