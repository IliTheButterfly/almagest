"""A Flipper in software: real RPC frames, the real Antlia grammar, a real tag.

`FakeTagSource` replays a script and `FakeWritableTagSource` models a tag. This
models the **device in between** — the thing that receives protobuf, decides what
Antlia would say, and answers. It is what lets the entire USB path be exercised
with no Flipper on the desk: `FlipperRpc` and `FlipperTagSource` run unmodified
against it, so what is under test is the production code and not a mock of it.

**Why it is worth this much machinery.** The three things most likely to be wrong
about driving a Flipper are not the byte encoding — that is pinned in
`tests/test_flipper_proto.py` — but the *conversation*: that a data exchange is
acknowledged and then answered separately, that the app announces itself
unprompted, and that a reply can arrive while the bridge is waiting for something
else. None of those can be tested by encoding a message and reading it back. They
need something on the other end that behaves like a Flipper, including the
inconvenient parts.

What it deliberately does **not** simulate: BLE MTU fragmentation (the transport's
problem, and `FrameDecoder` is tested against every split independently) or
timing. `DEFAULT_BANNER` exists only so `session.open_serial`'s drain has
something to drain, and is opt-in.
"""

from __future__ import annotations

from collections import deque
from typing import Final

from agent import tags
from agent.fake_tags import FakeWritableTagSource
from agent.flipper import antlia, proto
from agent.flipper.link import FlipperLinkError

#: What a Momentum build prints when a CDC session opens. Only the shape matters
#: — `session._drain` waits for silence rather than matching text, precisely so
#: that firmware forks with different banners all work.
#:
#: **Not the default.** A fake constructed bare is already in RPC mode, because
#: that is what almost every test wants; only the drain test asks for a banner,
#: and it asks explicitly. A default banner would make every other test carry a
#: setup step that has nothing to do with what it is asserting.
DEFAULT_BANNER: Final = b"\r\n\r\n              _.-------.._\r\n>: "


class FakeFlipperLink:
    """The other end of a `FlipperLink`, behaving like a Flipper running Antlia.

    Attributes are public and inspectable on purpose: a test asserts what was
    *launched* and what was *written to the tag*, not just what came back.
    """

    def __init__(
        self,
        tag: FakeWritableTagSource | None = None,
        *,
        antlia_installed: bool = True,
        can_write: bool = True,
        protocol_version: int = antlia.PROTOCOL_VERSION,
        banner: bytes = b"",
        answers_hello: bool = True,
        description: str = "Fake Flipper",
    ) -> None:
        self.tag = tag if tag is not None else FakeWritableTagSource()
        self.antlia_installed = antlia_installed
        self.can_write = can_write
        self.protocol_version = protocol_version
        self.answers_hello = answers_hello
        self._description = description

        self._decoder = proto.FrameDecoder()
        self._outbox: deque[bytes] = deque([banner] if banner else [])
        self.closed = False

        #: Every `(name, args)` the host asked the loader to start. The
        #: auto-launch assertion reads this.
        self.launched: list[tuple[str, str]] = []
        #: Every line the host sent Antlia, decoded. The protocol assertion.
        self.commands: list[str] = []
        self.app_running = False
        self.exited = False
        #: Accept every byte and answer nothing — a Flipper whose app crashed, or
        #: a cable half out. Distinct from `close()`, which raises: a mute device
        #: is exactly the one that must be caught by a *timeout* rather than by
        #: an error, and those are different code paths.
        self.mute = False

    @property
    def description(self) -> str:
        return self._description

    # -- FlipperLink -------------------------------------------------------

    def read(self, timeout_s: float) -> bytes:  # noqa: ARG002 — a fake has no latency
        if self.closed:
            raise FlipperLinkError("read after close")
        if not self._outbox:
            return b""
        return self._outbox.popleft()

    def write(self, data: bytes) -> None:
        if self.closed:
            raise FlipperLinkError("write after close")
        if self.mute:
            return
        # The CLI phase: anything before RPC starts is text, and a real Flipper
        # echoes it. Swallowed rather than decoded, which is exactly what the
        # banner drain in `session.open_serial` is there to cope with.
        if data.endswith(b"\r") and not data.startswith(b"\x00"):
            text = data.strip()
            if text in (b"", proto.encode_varint(0)):
                return
            if text == b"start_rpc_session":
                self._outbox.append(b"start_rpc_session\r\n")
                return
            if not self._looks_like_a_frame(data):
                self._outbox.append(data)
                return
        for frame in self._decoder.feed(data):
            self._handle(frame)

    def close(self) -> None:
        self.closed = True

    # -- behaviour ---------------------------------------------------------

    @staticmethod
    def _looks_like_a_frame(data: bytes) -> bool:
        """A length prefix that matches the buffer means protobuf, not a command.

        Crude, and only needed because this fake serves both the CLI phase and
        the RPC phase over one method. A real Flipper switches transports
        internally and never has to guess.
        """
        try:
            length, start = proto.decode_varint(data, 0)
        except (proto.Truncated, proto.ProtocolError):
            return False
        return start + length <= len(data)

    def _reply(self, command_id: int, field: int, content: bytes = b"", *, status: int = 0) -> None:
        self._outbox.append(proto.main(command_id, field, content, status=status))

    def _push_line(self, line: str) -> None:
        """An app-initiated data exchange: command id 0, unprompted.

        This is the part that makes the fake worth having. Antlia's answer is not
        the reply to the host's frame — the *ack* is — so a bridge that read the
        ack as the answer would pass against a simpler mock and fail against a
        Flipper.
        """
        self._outbox.append(
            proto.data_exchange(0, line.encode("utf-8") + b"\n")
        )

    def _handle(self, frame: proto.Frame) -> None:
        if frame.content_field == proto.CONTENT_SYSTEM_PING_REQUEST:
            self._reply(frame.command_id, proto.CONTENT_SYSTEM_PING_RESPONSE)
            return

        if frame.content_field == proto.CONTENT_APP_START_REQUEST:
            name = _string(frame.content, proto.START_NAME)
            args = _string(frame.content, proto.START_ARGS)
            self.launched.append((name, args))
            if not self.antlia_installed:
                # A real loader answers ERROR_APP_CANT_START here. The number is
                # not pinned (see `proto.STATUS_OK`), so any non-zero status
                # exercises the same branch.
                self._reply(frame.command_id, proto.CONTENT_EMPTY, status=6)
                return
            self._reply(frame.command_id, proto.CONTENT_EMPTY)
            self.app_running = True
            self._outbox.append(
                proto.main(
                    0,
                    proto.CONTENT_APP_STATE_RESPONSE,
                    proto.varint_field(proto.APP_STATE_STATE, proto.APP_STARTED),
                )
            )
            if self.answers_hello:
                caps = antlia.CAPS_READ_WRITE if self.can_write else antlia.CAPS_READ
                self._push_line(f"{antlia.HELLO} {self.protocol_version} {caps}")
            return

        if frame.content_field == proto.CONTENT_APP_DATA_EXCHANGE_REQUEST:
            payload = frame.data_exchange_payload or b""
            self._reply(frame.command_id, proto.CONTENT_EMPTY)
            self._run_antlia(payload.decode("utf-8", errors="replace").strip())
            return

        if frame.content_field == proto.CONTENT_APP_EXIT_REQUEST:
            self._reply(frame.command_id, proto.CONTENT_EMPTY)
            self.app_running = False
            self.exited = True
            self._outbox.append(
                proto.main(0, proto.CONTENT_APP_STATE_RESPONSE, b"")
            )
            return

        if frame.content_field == proto.CONTENT_STOP_SESSION:
            self._reply(frame.command_id, proto.CONTENT_EMPTY)
            return

        self._reply(frame.command_id, proto.CONTENT_EMPTY, status=2)

    def _run_antlia(self, line: str) -> None:
        """Antlia's side of the grammar, over a real simulated tag.

        Every branch here has a counterpart in `antlia/src/antlia_rpc.c`. Keeping
        them in step is a human job — see `agent.flipper.antlia`'s docstring for
        why the grammar is small enough for that to be realistic.
        """
        self.commands.append(line)
        verb, _, rest = line.partition(" ")

        if verb == antlia.PING:
            self._push_line(antlia.PONG)
            return

        if verb == antlia.READ:
            read = self.tag.poll()
            if read is None:
                self._push_line(antlia.NONE)
            else:
                uid = read.uid or antlia.ABSENT
                url = read.ndef_url or antlia.ABSENT
                self._push_line(f"{antlia.TAG} {uid} {url}")
            return

        if verb in (antlia.WRITE, antlia.WRITE_OVERWRITE):
            if not self.can_write:
                self._push_line(f"{antlia.ERR} {tags.UNSUPPORTED} this build cannot write")
                return
            try:
                written = self.tag.write_uri(rest, overwrite=verb == antlia.WRITE_OVERWRITE)
            except tags.TagWriteRefused as refusal:
                self._push_line(f"{antlia.ERR} {refusal.reason} {refusal}")
            except ValueError:
                self._push_line(f"{antlia.ERR} {tags.TOO_LONG} the payload does not fit")
            else:
                self._push_line(f"{antlia.WROTE} {written.read_back_url or antlia.ABSENT}")
            return

        self._push_line(f"{antlia.ERR} {tags.UNSUPPORTED} unknown verb {verb}")


def _string(content: bytes, number: int) -> str:
    for found, wire, value in proto._fields(content):
        if found == number and wire == proto.WIRE_BYTES:
            assert isinstance(value, bytes)
            return value.decode("utf-8")
    return ""
