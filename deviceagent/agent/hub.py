"""Fan-out: one event stream, several subscribers, no per-client bookkeeping.

Kept transport-agnostic on purpose. Everything here — sequence numbers, the
replay a fresh client gets, what happens when a subscriber dies — is a decision,
and none of it should need a socket to test. `agent.ws` is the twenty lines that
bolt a WebSocket to a `Sink`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from agent import __version__, events
from agent.events import Event, envelope, to_json


@runtime_checkable
class Sink(Protocol):
    """Somewhere to put a JSON message. A WebSocket connection, or a list."""

    async def send(self, message: str) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EventHub:
    """Publishes envelopes to every attached sink.

    Deliberately *not* buffering the stream for reconnection. The interesting
    history of a bench station is in the ledger, not here, and a client that
    reconnects needs to know one thing — what is on the platform *now* — which
    `attach` answers with a single replayed event. A ring buffer of past polls
    would be a second, weaker source of truth about what happened at the bench.
    """

    def __init__(
        self,
        *,
        agent_version: str = __version__,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._agent_version = agent_version
        self._clock = clock
        self._sinks: list[Sink] = []
        self._seq = 0
        #: The last state-defining envelope, kept with its **original** `seq`.
        self._sticky: dict[str, Any] | None = None

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def sticky(self) -> dict[str, Any] | None:
        return self._sticky

    async def publish(self, event: Event) -> dict[str, Any]:
        """Stamp, remember if it is state, send to everyone. Returns the envelope."""
        self._seq += 1
        message = envelope(event, seq=self._seq, at=self._clock())

        if event.type in events.STICKY_TYPES:
            self._sticky = message
        elif event.type in events.CLEARING_TYPES:
            # The platform is empty, so there is no state for a new client to
            # catch up on. Not clearing here is how a kiosk reload three hours
            # later would render a container that is back in its cabinet.
            self._sticky = None

        await self._broadcast(to_json(message))
        return message

    async def attach(self, sink: Sink) -> None:
        """Register a sink, greet it, and bring it up to date.

        The replayed sticky event **keeps the seq it was first published with**.
        That is the whole dedupe mechanism: a kiosk that reconnects after a
        dropped Wi-Fi frame and is handed seq 87 again, having already processed
        87, ignores it; a browser tab opened for the first time has seen nothing
        and renders it. Neither needs the agent to remember which client is which.
        """
        self._sinks.append(sink)
        greeting = envelope(
            events.station_hello(agent_version=self._agent_version, last_seq=self._seq),
            # Hello is per-connection rather than part of the ordered stream, so
            # it takes seq 0 instead of consuming a number the stream would then
            # be missing from every *other* client's point of view.
            seq=0,
            at=self._clock(),
        )
        if not await self._send(sink, to_json(greeting)):
            return
        if self._sticky is not None:
            await self._send(sink, to_json(self._sticky))

    def detach(self, sink: Sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    @property
    def subscriber_count(self) -> int:
        return len(self._sinks)

    async def _broadcast(self, message: str) -> None:
        # Snapshot, because a failing send detaches its sink mid-iteration.
        for sink in list(self._sinks):
            await self._send(sink, message)

    async def _send(self, sink: Sink, message: str) -> bool:
        """Send, dropping the sink if it fails. True if it is still attached.

        **One dead subscriber must not stop the bench station.** A kiosk tab that
        was closed mid-session is the ordinary case, and an exception escaping
        here would propagate into the poll loop and take the reader down with it.
        `CancelledError` is re-raised: that is the agent being shut down, not a
        subscriber misbehaving, and swallowing it would make shutdown hang.
        """
        try:
            await sink.send(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.detach(sink)
            return False
        return True
