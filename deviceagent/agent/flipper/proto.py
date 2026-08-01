"""The six Flipper RPC messages this bridge needs, hand-encoded. Standard library.

**Why not the `protobuf` runtime.** `deviceagent/pyproject.toml` is explicit that
this process runs on a Raspberry Pi next to kiosk Chromium and every dependency
is one more thing to cross-compile or apt-pin. The full Flipper RPC surface is
~75 message types across eight `.proto` files; the bridge uses **six**, and every
Almagest-specific concept travels as text inside one of them
(`app_data_exchange_request`). Generating and vendoring a protobuf runtime plus
75 message classes to send `WRITE <url>` is not a trade worth making, and the
alternative — a varint writer and a field table — is the smaller thing to keep
correct.

**Field numbers are ground truth, not recollection.** Every constant below is
copied from `flipperzero-protobuf` (`flipper.proto`, `application.proto`) and is
the single place they appear. `tests/test_flipper_proto.py` asserts the encoding
**byte for byte** against hand-written expected bytes, so a typo in a field
number fails in CI rather than as a Flipper that ignores every command. That test
is the only check available without hardware and it is worth reading before
trusting anything here.

**Framing.** Each `PB_Main` on the wire is preceded by a varint of its own
length — nanopb's `PB_ENCODE_DELIMITED`. The stream is otherwise unstructured, so
`FrameDecoder` has to tolerate a length varint split across two USB reads, which
is the ordinary case at 64-byte packets.

What is *not* modelled, deliberately: storage, GUI, GPIO, desktop, property,
update, and every request whose reply the bridge would have to interpret. If a
future need appears, the honest move is to add the one field number to the table
below, not to reach for a runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

# ---------------------------------------------------------------------------
# Wire primitives
# ---------------------------------------------------------------------------

WIRE_VARINT: Final = 0
WIRE_BYTES: Final = 2


def encode_varint(value: int) -> bytes:
    """Base-128, little-endian, high bit as continuation. Non-negative only.

    Every varint this codec writes is a field number, a length, or a small
    unsigned scalar, so the negative/zigzag cases are absent rather than
    unimplemented — an `int32` field with a negative value would need ten bytes
    of sign extension, and nothing here has one.
    """
    if value < 0:
        raise ValueError(f"varint cannot encode {value}")
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Read one varint at `offset`. Returns `(value, next_offset)`.

    Raises `Truncated` when the buffer ends mid-varint, which `FrameDecoder`
    treats as "wait for more bytes" rather than as corruption.
    """
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise Truncated("varint ran off the end of the buffer")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise ProtocolError("varint is longer than 64 bits")


class ProtocolError(Exception):
    """The bytes are not a message this codec can read. Never recoverable by
    waiting — the session is resynchronised by closing it."""


class Truncated(Exception):
    """The buffer ends mid-message. Recoverable by reading more bytes."""


def _tag(number: int, wire: int) -> bytes:
    return encode_varint((number << 3) | wire)


def varint_field(number: int, value: int) -> bytes:
    """A scalar field. Proto3 omits zero-valued scalars, and so does this: an
    encoder that emitted `has_next: false` explicitly would produce bytes no
    Flipper has ever seen, for no gain."""
    if value == 0:
        return b""
    return _tag(number, WIRE_VARINT) + encode_varint(value)


def bytes_field(number: int, payload: bytes) -> bytes:
    """A length-delimited field. Emitted even when empty, unlike a scalar —
    `empty {}` and an absent oneof member are different facts, and the oneof is
    how the receiver knows which command this is."""
    return _tag(number, WIRE_BYTES) + encode_varint(len(payload)) + payload


def string_field(number: int, text: str) -> bytes:
    """Proto3 omits empty strings. `StartRequest.args` relies on this: launching
    without arguments must produce the same bytes the Flipper mobile app sends."""
    if not text:
        return b""
    return bytes_field(number, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Field numbers — copied from flipperzero-protobuf. The only copy.
# ---------------------------------------------------------------------------

#: `PB.Main`, the outer envelope.
MAIN_COMMAND_ID: Final = 1
MAIN_COMMAND_STATUS: Final = 2
MAIN_HAS_NEXT: Final = 3

#: `PB.Main.content`, the oneof. Only what the bridge sends or reads.
CONTENT_EMPTY: Final = 4
CONTENT_SYSTEM_PING_REQUEST: Final = 5
CONTENT_SYSTEM_PING_RESPONSE: Final = 6
CONTENT_APP_START_REQUEST: Final = 16
CONTENT_STOP_SESSION: Final = 19
CONTENT_APP_EXIT_REQUEST: Final = 47
CONTENT_APP_STATE_RESPONSE: Final = 58
CONTENT_APP_GET_ERROR_REQUEST: Final = 63
CONTENT_APP_GET_ERROR_RESPONSE: Final = 64
CONTENT_APP_DATA_EXCHANGE_REQUEST: Final = 65

#: `PB_App.StartRequest`
START_NAME: Final = 1
START_ARGS: Final = 2

#: `PB_App.DataExchangeRequest` — the byte array both directions ride in.
DATA_EXCHANGE_DATA: Final = 1

#: `PB_App.AppStateResponse` / `PB_App.AppState`
APP_STATE_STATE: Final = 1
APP_CLOSED: Final = 0
APP_STARTED: Final = 1

#: `PB_App.GetErrorResponse`
GET_ERROR_CODE: Final = 1
GET_ERROR_TEXT: Final = 2

#: `PB.CommandStatus.OK`. **Only zero is pinned.** Proto3 requires the first
#: enumerator to be 0 and it is `OK`, which is certain; the other 21 statuses are
#: reported by number rather than by a name this codec might have wrong. A wrong
#: status *name* in an operator-facing error is worse than a number, because it
#: sends someone looking at the wrong subsystem.
STATUS_OK: Final = 0

#: The path Antlia installs to, and the argument that puts it in bridge mode
#: rather than keyboard-wedge mode. See ADR 0013: claiming USB HID would sever
#: the very CDC interface this session is riding on, so the two modes are
#: disjoint and the launch argument is what selects between them.
ANTLIA_FAP_PATH: Final = "/ext/apps/NFC/antlia.fap"
RPC_LAUNCH_ARGS: Final = "RPC"


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def main(
    command_id: int, content_field: int, content: bytes = b"", *, status: int = STATUS_OK
) -> bytes:
    """One `PB.Main`, length-delimited, ready for the wire.

    `command_id` correlates a reply with its request; the Flipper echoes it.
    Zero is reserved for messages the *Flipper* initiates unprompted, so callers
    start at 1 — see `FlipperRpc._next_id`.

    `status` is only ever non-zero on a *reply*, which the bridge does not send;
    it is here so `agent.flipper.fake` can simulate a refusing Flipper with the
    same encoder the real one is parsed by, rather than with hand-built bytes
    that could be wrong in the same direction as the parser.
    """
    body = (
        varint_field(MAIN_COMMAND_ID, command_id)
        + varint_field(MAIN_COMMAND_STATUS, status)
        + bytes_field(content_field, content)
    )
    return encode_varint(len(body)) + body


def ping(command_id: int) -> bytes:
    """The cheapest proof something on the other end speaks RPC at all.

    Sent before anything else, for the same reason `Pn532TagSource.__init__`
    reads the firmware version: without it a wrong serial port fails later and
    looks like "the Flipper never answers", which is a much worse diagnostic than
    "nothing on /dev/ttyACM0 replied to a ping".
    """
    return main(command_id, CONTENT_SYSTEM_PING_REQUEST)


def start_app(command_id: int, *, name: str, args: str) -> bytes:
    """`app_start_request` — launch a FAP by path. This is the auto-launch.

    Nobody touches the Flipper's screen: the bridge names the `.fap` and the
    argument that selects its mode, and the loader does the rest.
    """
    content = string_field(START_NAME, name) + string_field(START_ARGS, args)
    return main(command_id, CONTENT_APP_START_REQUEST, content)


def data_exchange(command_id: int, payload: bytes) -> bytes:
    """`app_data_exchange_request` — arbitrary bytes to the running app.

    The firmware's own words for this message (`rpc_app.h`): *"bi-directional
    exchange of arbitrary raw data. Useful for implementing higher-level
    protocols while using the RPC as a transport layer."* Everything
    Almagest-specific is a line of text in here, which is why the rest of this
    module is six messages instead of seventy-five.
    """
    content = bytes_field(DATA_EXCHANGE_DATA, payload)
    return main(command_id, CONTENT_APP_DATA_EXCHANGE_REQUEST, content)


def exit_app(command_id: int) -> bytes:
    """Ask the running app to exit. Sent on close so a Flipper is not left
    sitting in bridge mode with no host — which looks, on the device, like a
    frozen app."""
    return main(command_id, CONTENT_APP_EXIT_REQUEST)


def stop_session(command_id: int) -> bytes:
    """End the RPC session itself, returning the Flipper to its normal CLI."""
    return main(command_id, CONTENT_STOP_SESSION)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded `PB.Main`.

    `content` is kept as raw bytes and interpreted by whoever cares. The bridge
    only ever looks inside three of them, and decoding the rest eagerly would
    mean modelling messages nothing reads.
    """

    command_id: int
    status: int
    has_next: bool
    content_field: int | None
    content: bytes = b""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def data_exchange_payload(self) -> bytes | None:
        """The bytes the *app* sent us, or None if this frame is something else."""
        if self.content_field != CONTENT_APP_DATA_EXCHANGE_REQUEST:
            return None
        return _sub_bytes(self.content, DATA_EXCHANGE_DATA) or b""

    @property
    def app_state(self) -> int | None:
        """`APP_STARTED` / `APP_CLOSED`, or None if this frame is something else.

        The Flipper sends this unprompted with `command_id: 0` when an
        RPC-launched app starts and again when it exits, which is how the bridge
        learns Antlia is ready without polling for it.
        """
        if self.content_field != CONTENT_APP_STATE_RESPONSE:
            return None
        value = _sub_varint(self.content, APP_STATE_STATE)
        # Proto3 omits a zero, so an absent field *is* APP_CLOSED.
        return APP_CLOSED if value is None else value

    @property
    def error_text(self) -> str | None:
        if self.content_field != CONTENT_APP_GET_ERROR_RESPONSE:
            return None
        raw = _sub_bytes(self.content, GET_ERROR_TEXT)
        return "" if raw is None else raw.decode("utf-8", errors="replace")


def _fields(data: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """Walk a message, yielding `(number, wire_type, value)`.

    Unknown fields are yielded rather than skipped so a caller can ignore them
    explicitly; unknown *wire types* are fatal, because a length this codec
    cannot measure means it has lost its place in the buffer and everything after
    is garbage.
    """
    offset = 0
    while offset < len(data):
        key, offset = decode_varint(data, offset)
        number, wire = key >> 3, key & 0x07
        if wire == WIRE_VARINT:
            value, offset = decode_varint(data, offset)
            yield number, wire, value
        elif wire == WIRE_BYTES:
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise Truncated("length-delimited field ran off the end")
            yield number, wire, data[offset:end]
            offset = end
        elif wire == 5:  # fixed32
            offset += 4
        elif wire == 1:  # fixed64
            offset += 8
        else:
            raise ProtocolError(f"unsupported wire type {wire} for field {number}")


def _sub_bytes(data: bytes, number: int) -> bytes | None:
    for found, wire, value in _fields(data):
        if found == number and wire == WIRE_BYTES:
            assert isinstance(value, bytes)
            return value
    return None


def _sub_varint(data: bytes, number: int) -> int | None:
    for found, wire, value in _fields(data):
        if found == number and wire == WIRE_VARINT:
            assert isinstance(value, int)
            return value
    return None


#: Envelope fields that are *not* the oneof. Anything else in a `Main` is the
#: content, whether or not this codec knows the message inside.
_ENVELOPE: Final = frozenset({MAIN_COMMAND_ID, MAIN_COMMAND_STATUS, MAIN_HAS_NEXT})


def parse_main(body: bytes) -> Frame:
    """One `PB.Main` body (no length prefix) into a `Frame`."""
    command_id = 0
    status = STATUS_OK
    has_next = False
    content_field: int | None = None
    content = b""

    for number, wire, value in _fields(body):
        if number == MAIN_COMMAND_ID and wire == WIRE_VARINT:
            assert isinstance(value, int)
            command_id = value
        elif number == MAIN_COMMAND_STATUS and wire == WIRE_VARINT:
            assert isinstance(value, int)
            status = value
        elif number == MAIN_HAS_NEXT and wire == WIRE_VARINT:
            has_next = bool(value)
        elif number not in _ENVELOPE and wire == WIRE_BYTES and content_field is None:
            assert isinstance(value, bytes)
            content_field, content = number, value

    return Frame(
        command_id=command_id,
        status=status,
        has_next=has_next,
        content_field=content_field,
        content=content,
    )


@dataclass
class FrameDecoder:
    """Bytes in, whole `Frame`s out. Tolerates any split.

    A USB CDC read hands back whatever happened to be in the endpoint buffer, so
    a length prefix arriving in one read and its body in the next is the ordinary
    case rather than an edge one — and over BLE, where the MTU is ~20-244 bytes,
    every frame of any size is split. Holding the remainder here is what keeps
    that out of both transports.
    """

    buffer: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> list[Frame]:
        """Append and return every complete frame now available.

        A `Truncated` from either the length or the body means "not yet" and the
        bytes stay buffered. Only `ProtocolError` — a wire type that cannot be
        skipped — is raised, because that one cannot be fixed by waiting.
        """
        self.buffer.extend(chunk)
        frames: list[Frame] = []
        while True:
            try:
                length, start = decode_varint(bytes(self.buffer), 0)
            except Truncated:
                return frames
            end = start + length
            if end > len(self.buffer):
                return frames
            body = bytes(self.buffer[start:end])
            del self.buffer[:end]
            try:
                frames.append(parse_main(body))
            except Truncated as error:
                # The body is *complete* by construction — its length came off
                # the wire and the buffer held that many bytes. A field inside
                # it running off the end therefore means the bytes are not a
                # `Main` at all, which no amount of further reading will fix.
                # The realistic cause is the decoder having been fed something
                # that was never protobuf: the Flipper's CLI banner, before
                # `start_rpc_session` took effect. Raising `Truncated` here
                # would tell the caller to wait for more of a message that does
                # not exist.
                raise ProtocolError(
                    f"a {len(body)}-byte frame is not a well-formed Main: {error}"
                ) from error
