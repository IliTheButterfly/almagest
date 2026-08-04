"""The loopback WebSocket the kiosk PWA subscribes to.

Thin by design — every decision is in `agent.hub` and `agent.session`. What is
here is the socket and two policies:

**Loopback only.** This socket has no authentication and no origin check, and it
narrates every container the bench touches. It is bound to 127.0.0.1 because the
only client is Chromium on the same Pi; `agent.config` refuses any other host
rather than leaving that to a deployment note nobody reads.

**Read and discard, unless a handler is wired.** This socket started
one-directional, and workflow 5 is why it is not any more: the user proposes an
action, confirms it and commits it, and the process that must refuse a commit the
instant the container is lifted is the one holding the reader. A PWA that owned
the pending action could not be stopped by a removal it has not heard about yet.

The reversal is kept as narrow as it can be:

* `on_frame` is optional. With no handler this module still drains and discards,
  which is what the protocol tests exercise — so "has a command surface" is a
  wiring decision made in one place, `agent.main`, and not a property of the
  socket;
* the vocabulary is four imperative verbs (`agent.events.COMMAND_TYPES`) and
  everything else is dropped unread;
* a command cannot name anything the agent did not already announce, and must
  carry the current `session_id`. See `agent.session` for why that is what makes
  the abort-before-commit guarantee hold under a race;
* draining is still required regardless — an unread receive buffer eventually
  stalls the connection.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from agent.hub import EventHub

logger = logging.getLogger("almagest.deviceagent.ws")

#: Handles one inbound text frame. Returns nothing: whatever a command causes is
#: published to *every* subscriber through the hub, not answered privately to the
#: sender, because two kiosk tabs looking at one bench must not disagree about
#: what is pending.
FrameHandler = Callable[[str], Awaitable[None]]


@dataclass(eq=False)
class ConnectionSink:
    """Adapts one WebSocket connection to `hub.Sink`.

    `eq=False` so identity comparison is used: the hub finds a sink in its list
    by identity, and two connections are never interchangeable.
    """

    connection: ServerConnection

    async def send(self, message: str) -> None:
        await self.connection.send(message)


def _add_headers(
    headers: dict[str, str],
) -> Callable[[ServerConnection, Request, Response], Response]:
    """Stamp `headers` onto the handshake response, leaving it otherwise alone.

    `process_response` rather than `process_request` on purpose: returning a
    response from `process_request` would *reject* the upgrade. This runs after
    the handshake has been accepted and only adds headers.
    """

    def process(connection: ServerConnection, request: Request, response: Response) -> Response:
        del connection, request
        for name, value in headers.items():
            response.headers[name] = value
        return response

    return process


def private_network_headers(origin: str) -> dict[str, str]:
    """Headers that let an `https://` page open this `ws://` loopback socket.

    The PWA is served from `https://almagest.aether.lan` (ADR 0001) and this socket is
    `ws://127.0.0.1:8765`. Loopback is "potentially trustworthy" per the
    secure-context spec, so the connection is *specified* to be allowed — but
    Chrome's Private Network Access work adds a preflight for requests from a
    public origin to a local one, and browsers have differed about mixed-content
    WebSockets before. Answering the preflight explicitly costs three headers.
    Getting it wrong costs a bridge that is running, answers `curl`, and is
    invisible to the page — which is close to the worst diagnostic in this
    codebase, because everything looks correct from the terminal.

    **This is not authentication and does not pretend to be.** The socket's
    security is that it is bound to the loopback and refuses to be bound
    anywhere else (`agent.config._refuse_a_non_loopback_bind`). An `Origin`
    header is set by the browser and trivially absent from anything that is not
    one, so it is a compatibility shim, not a gate.
    """
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Private-Network": "true",
        "Vary": "Origin",
    }


@asynccontextmanager
async def serve_events(
    hub: EventHub,
    *,
    host: str,
    port: int,
    on_frame: FrameHandler | None = None,
    allowed_origin: str | None = None,
) -> AsyncIterator[int]:
    """Run the event server for the duration of the context. Yields the port.

    The port is yielded rather than assumed because a test binds port 0 and asks
    the OS for a free one; production passes the configured port and gets it back.
    """

    async def handler(connection: ServerConnection) -> None:
        sink = ConnectionSink(connection)
        await hub.attach(sink)
        try:
            async for frame in connection:
                if on_frame is None:
                    continue
                # Binary frames are not part of this protocol. Ignored rather
                # than decoded, so a client cannot smuggle a command past the
                # size guard by sending bytes.
                if not isinstance(frame, str):
                    logger.warning("dropped a binary frame")
                    continue
                # A command that raises must not kill the connection, and must
                # certainly not kill the reader: a kiosk that stops receiving
                # events is the failure this whole daemon exists to avoid. The
                # session's own refusals are events, so anything reaching here is
                # a bug worth a traceback in the log and nothing more.
                try:
                    await on_frame(frame)
                except Exception:
                    logger.exception("command handler failed")
        except ConnectionClosed:
            # **The normal way a bench client leaves.** A kiosk reload, a closed
            # lid, a tab killed by a restart: none of them send a close frame, so
            # `websockets` raises out of the iterator and logged a full traceback
            # for each one. The `finally` below already says this is expected;
            # the log did not, and a stack trace per reload is how a genuine
            # fault goes unnoticed on a machine nobody reads the log of daily.
            logger.debug("client went away without a close frame")
        finally:
            # In a `finally` because a client that vanishes without a close frame
            # is the normal case at a bench — a kiosk reload, a closed lid — and a
            # sink left attached would be sent to on every event for ever.
            hub.detach(sink)

    extra_headers = private_network_headers(allowed_origin) if allowed_origin else {}

    async with serve(handler, host, port, process_response=_add_headers(extra_headers)) as server:
        bound = next(iter(server.sockets)).getsockname()[1]
        yield int(bound)
