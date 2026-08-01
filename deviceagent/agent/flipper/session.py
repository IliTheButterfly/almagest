"""An RPC conversation with a Flipper, and the `TagSource` that rides on it.

Three layers meet here and it is worth naming them, because each is testable
without the one below:

    FlipperTagSource   poll() / write_uri()  — the TagSource the bridge sees
    FlipperRpc         request / exchange    — command ids, acks, app state
    FlipperLink        read / write bytes    — USB CDC or BLE

**The Flipper talks unprompted, and that is the interesting part.** An
RPC-launched app announces itself with an `app_state_response` nobody asked for,
sends its replies as `app_data_exchange_request` frames with no command id, and
announces its own exit the same way. So the reader here is a *pump* that
classifies every frame — reply, app message, state change — rather than a
request/response function that assumes the next frame is its answer. Getting this
wrong produces a session that works until the first time two things happen at
once, which at a bench is immediately.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Final

from agent import tags
from agent.flipper import antlia, proto
from agent.flipper.link import FlipperLink, FlipperLinkError
from agent.tags import (
    TagCapabilities,
    TagRead,
    TagSourceError,
    TagWrite,
    TagWriteRefused,
)

logger = logging.getLogger("almagest.deviceagent.flipper")

#: How long one RPC round trip may take. Generous next to the station's 300 ms
#: poll cadence because a Flipper doing an NFC read is doing real radio work, and
#: because the bridge polls a Flipper on its own schedule rather than the
#: station's.
DEFAULT_TIMEOUT_S: Final = 3.0

#: How long to wait for Antlia's `HELLO` after asking the loader to launch it.
#: A cold `.fap` load from the SD card is the slow part.
DEFAULT_LAUNCH_TIMEOUT_S: Final = 10.0

#: The CLI incantation that turns a text session into an RPC one. Sent with `\r`
#: because the Flipper's CLI is a terminal, not a line protocol.
START_RPC: Final = b"start_rpc_session\r"


class FlipperRpc:
    """Command ids, the frame pump, and the two requests the bridge makes.

    Not thread-safe by design: one link, one session, one caller. The bridge runs
    each device's polling in its own thread, so two threads sharing a Flipper
    would be a bug in the registry rather than something to lock around here.
    """

    def __init__(
        self,
        link: FlipperLink,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._link = link
        self._timeout_s = timeout_s
        self._clock = clock
        self._decoder = proto.FrameDecoder()
        self._command_id = 0
        #: Lines the app sent unprompted, oldest first. A deque rather than a
        #: single slot because Antlia may emit `HELLO` before the bridge has
        #: asked it anything, and dropping that would mean re-probing for
        #: capabilities the device already announced.
        self._inbox: deque[bytes] = deque()
        #: Acks waiting to be matched to a command id. Separate from `_inbox`
        #: because the two are consumed by different waits, and a single queue
        #: would let one wait swallow the other's frame.
        self._replies: deque[proto.Frame] = deque()
        self._app_running: bool | None = None
        self.closed = False

    @property
    def description(self) -> str:
        return self._link.description

    @property
    def app_running(self) -> bool | None:
        """True/False once the Flipper has said so, None before it ever has."""
        return self._app_running

    def _next_id(self) -> int:
        """Ids start at 1. **Zero is reserved for frames the Flipper initiates**,
        so using it for a request would make an app's unprompted message
        indistinguishable from an answer to that request."""
        self._command_id += 1
        return self._command_id

    # -- the pump ----------------------------------------------------------

    def _pump_once(self, deadline: float) -> bool:
        """Read one chunk and file whatever it decodes to. False once out of time.

        **Filing rather than returning is the whole point.** A Flipper interleaves
        three kinds of frame — the ack for the command in flight, a line the app
        sent unprompted, and a state change — and any of them can arrive while a
        caller is waiting for one of the others. So this classifies into three
        places and hands control straight back, and the two waiting loops below
        each check their own queue first.

        An earlier version returned only when it found a *reply*, which meant a
        caller waiting for an app line burned its entire timeout budget reading
        past the line it already had. That is the failure this shape exists to
        prevent, and `test_the_ack_is_not_mistaken_for_the_answer` pins it.
        """
        remaining = deadline - self._clock()
        if remaining <= 0:
            return False
        chunk = self._link.read(min(remaining, 0.25))
        for frame in self._decoder.feed(chunk):
            payload = frame.data_exchange_payload
            if payload is not None:
                self._inbox.append(payload)
                continue
            state = frame.app_state
            if state is not None:
                self._app_running = state == proto.APP_STARTED
                logger.debug("flipper app state: running=%s", self._app_running)
                continue
            self._replies.append(frame)
        return self._clock() < deadline

    def _await_reply(self, command_id: int, deadline: float) -> proto.Frame:
        """The ack for one command id. Frames for other ids are discarded.

        Discarded rather than kept: the bridge issues one request at a time, so a
        reply for a different id is a stale answer to a request that already
        timed out, and keeping it would only let it be mistaken for the next one.
        """
        while True:
            while self._replies:
                frame = self._replies.popleft()
                if frame.command_id == command_id:
                    return frame
                logger.debug("discarding a stale frame for command %s", frame.command_id)
            if not self._pump_once(deadline):
                raise TagSourceError(
                    f"{self.description} did not answer command {command_id} in "
                    f"{self._timeout_s:.0f}s"
                )

    def _deadline(self, timeout_s: float | None = None) -> float:
        return self._clock() + (self._timeout_s if timeout_s is None else timeout_s)

    def _send(self, raw: bytes) -> None:
        try:
            self._link.write(raw)
        except FlipperLinkError as error:
            raise TagSourceError(str(error)) from error

    # -- requests ----------------------------------------------------------

    def ping(self) -> None:
        """Proof the other end speaks RPC at all.

        The same role as reading the PN532's firmware version in its constructor:
        without it, a wrong serial node fails later and presents as "the Flipper
        never answers", which sends someone looking at the app instead of at the
        port.
        """
        command_id = self._next_id()
        self._send(proto.ping(command_id))
        frame = self._await_reply(command_id, self._deadline())
        if not frame.ok:
            raise TagSourceError(f"{self.description} refused a ping: status {frame.status}")

    def launch_antlia(
        self,
        *,
        path: str = proto.ANTLIA_FAP_PATH,
        launch_timeout_s: float = DEFAULT_LAUNCH_TIMEOUT_S,
    ) -> antlia.Hello:
        """Start Antlia in bridge mode and wait for its `HELLO`. The auto-launch.

        Nobody touches the Flipper. The loader is told a path and the argument
        `RPC`, which is what selects bridge mode over keyboard-wedge mode — see
        ADR 0013: in bridge mode Antlia must not claim USB HID, because claiming
        it would replace the CDC interface this very session is riding on.

        Two waits, and they are separate on purpose. The loader acks the *launch
        request* almost immediately; the app then has to be read off the SD card
        and start up, and only its `HELLO` proves it is actually listening. A
        bridge that treated the ack as readiness would send `READ` into a void.
        """
        command_id = self._next_id()
        self._send(proto.start_app(command_id, name=path, args=proto.RPC_LAUNCH_ARGS))
        frame = self._await_reply(command_id, self._deadline())
        if not frame.ok:
            raise TagSourceError(
                f"{self.description} could not start {path}: status {frame.status}. "
                "Is Antlia installed?"
            )

        deadline = self._deadline(launch_timeout_s)
        while True:
            line = self._next_line(deadline)
            if line is None:
                raise TagSourceError(
                    f"{path} started on {self.description} but never said HELLO. "
                    "Is it a build with bridge mode?"
                )
            reply = antlia.parse_reply(line)
            if isinstance(reply, antlia.Hello):
                if reply.version != antlia.PROTOCOL_VERSION:
                    raise TagSourceError(
                        f"{self.description} runs Antlia protocol {reply.version}, "
                        f"this bridge speaks {antlia.PROTOCOL_VERSION}"
                    )
                return reply
            logger.debug("ignoring %r while waiting for HELLO", line)

    def _next_line(self, deadline: float) -> str | None:
        """The next line the app sent, from the inbox or from the wire."""
        while True:
            if self._inbox:
                return self._inbox.popleft().decode("utf-8", errors="replace").strip()
            if not self._pump_once(deadline):
                return None

    def exchange(self, payload: bytes) -> str:
        """Send one line to Antlia and return the one it sends back.

        The Flipper acks the *delivery* of the data exchange with an empty frame
        carrying our command id, and Antlia's actual reply arrives separately and
        unprompted. Both are handled: the ack is awaited so a delivery failure is
        not mistaken for a slow app, and the reply is then taken from the inbox.
        """
        if self.closed:
            raise TagSourceError("exchange() after close()")
        command_id = self._next_id()
        deadline = self._deadline()
        self._send(proto.data_exchange(command_id, payload))
        ack = self._await_reply(command_id, deadline)
        if not ack.ok:
            raise TagSourceError(
                f"{self.description} rejected a data exchange: status {ack.status}"
            )
        line = self._next_line(deadline)
        if line is None:
            raise TagSourceError(
                f"Antlia on {self.description} did not answer {payload!r} in "
                f"{self._timeout_s:.0f}s"
            )
        return line

    def close(self) -> None:
        """Ask Antlia to exit, end the session, drop the link. Never raises.

        Asking the app to exit matters: a Flipper left sitting in bridge mode
        with no host looks, on the device, like a frozen app, and the only way
        out is the back button.
        """
        if self.closed:
            return
        self.closed = True
        for message in (proto.exit_app(self._next_id()), proto.stop_session(self._next_id())):
            try:
                self._link.write(message)
            except Exception:
                break
        self._link.close()


class FlipperTagSource:
    """A Flipper running Antlia, as a `TagSource`.

    Capabilities come from Antlia's `HELLO` rather than from a constant, because
    they genuinely differ: an Antlia build without the write path answers `r` and
    one with it answers `rw`, and the whole point of ADR 0013's capability set is
    that the client is told rather than guessing. `agent.tags.TagSource`'s
    contract that capabilities are constant for the object's lifetime holds — the
    `HELLO` is read once, at construction.
    """

    def __init__(self, rpc: FlipperRpc, hello: antlia.Hello) -> None:
        self._rpc = rpc
        self._capabilities = hello.capabilities
        self.protocol_version = hello.version

    @property
    def capabilities(self) -> TagCapabilities:
        return self._capabilities

    @property
    def description(self) -> str:
        return self._rpc.description

    def poll(self) -> TagRead | None:
        """One `READ`. `None` for an empty field, exactly as the PN532 reports it."""
        reply = antlia.parse_reply(self._rpc.exchange(antlia.command_read()))
        if isinstance(reply, antlia.Empty):
            return None
        if isinstance(reply, TagRead):
            return reply
        if isinstance(reply, antlia.Refused):
            # A refusal in answer to a read means the *reader* is unhappy — there
            # is nothing for a user to fix by re-seating a tag — so it is a
            # `TagSourceError`, which is the distinction `agent.tags` draws.
            raise TagSourceError(f"{self.description}: {reply.message}")
        raise TagSourceError(f"{self.description} answered a READ with {reply!r}")

    def write_uri(self, url: str, *, overwrite: bool = False) -> TagWrite:
        """One `WRITE`, and Antlia reads the tag back before answering.

        The read-back happens on the Flipper because that is the device holding
        the tag — the same rule as `Pn532TagSource.write_uri`, and the reason
        `TagWrite` carries a URI rather than a boolean (ADR 0012).
        """
        if not self._capabilities.writes_ndef:
            raise TagWriteRefused(
                f"{self.description} runs an Antlia build that cannot write",
                reason=tags.UNSUPPORTED,
            )
        reply = antlia.parse_reply(
            self._rpc.exchange(antlia.command_write(url, overwrite=overwrite))
        )
        if isinstance(reply, antlia.Wrote):
            if reply.read_back_url is None:
                raise TagWriteRefused(
                    "the tag did not read back after writing",
                    reason=tags.READ_BACK_FAILED,
                )
            return TagWrite(read_back_url=reply.read_back_url)
        if isinstance(reply, antlia.Refused):
            raise TagWriteRefused(reply.message, reason=reply.reason)
        raise TagSourceError(f"{self.description} answered a WRITE with {reply!r}")

    def close(self) -> None:
        self._rpc.close()


def open_serial(
    port: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    banner_quiet_s: float = 0.3,
) -> FlipperTagSource:
    """Open a USB Flipper, get it into RPC, launch Antlia. The whole USB path.

    **The CLI banner has to go first.** A fresh CDC session lands on the Flipper's
    text CLI, which greets it with a prompt and echoes what is typed. Those bytes
    are not protobuf, and feeding them to `FrameDecoder` would desynchronise it
    for the life of the session — a failure that looks like a Flipper that never
    answers rather than like a parsing problem. So: send the incantation, wait for
    the chatter to stop, and only then start framing.

    The `ping` immediately after is what proves the guess was right. If the
    banner drain ended early, the ping fails here — at open, with a clear
    message — instead of corrupting the first real command.
    """
    from agent.flipper.link import SerialFlipperLink

    link = SerialFlipperLink(port)
    try:
        link.write(b"\r")
        _drain(link, banner_quiet_s)
        link.write(START_RPC)
        _drain(link, banner_quiet_s)
        rpc = FlipperRpc(link, timeout_s=timeout_s)
        rpc.ping()
        hello = rpc.launch_antlia()
    except Exception:
        link.close()
        raise
    return FlipperTagSource(rpc, hello)


def _drain(link: FlipperLink, quiet_s: float, *, limit_s: float = 3.0) -> bytes:
    """Read until the link has been silent for `quiet_s`. Returns what was read.

    Silence rather than a pattern match, because the CLI banner differs between
    firmware forks — Momentum, Unleashed and official all print something
    different — and matching on any of them would break on the others. `limit_s`
    stops a Flipper that is chattering for its own reasons from hanging the open.
    """
    seen = bytearray()
    while limit_s > 0:
        chunk = link.read(quiet_s)
        if not chunk:
            return bytes(seen)
        seen.extend(chunk)
        limit_s -= quiet_s
    return bytes(seen)
