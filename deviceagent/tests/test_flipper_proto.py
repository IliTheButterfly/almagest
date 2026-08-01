"""The Flipper RPC codec, asserted against bytes computed by hand.

**This is the only check on `agent.flipper.proto` that does not require a
Flipper**, and it is deliberately written the awkward way: the expected bytes
below were worked out from the wire format and the upstream `.proto` field
numbers, *not* by running the encoder and pasting its output. A test that records
what the code already does would pass just as happily with a wrong field number,
and a wrong field number is invisible — the Flipper silently ignores a command it
cannot parse, so the failure looks like "the app never launched".

The same reasoning as `tests/fixtures/ecia/*.expected.json`, which are hand-
verified because no reference parser exists to diff against.
"""

from __future__ import annotations

import pytest

from agent.flipper import proto


class TestVarint:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, b"\x00"),
            (1, b"\x01"),
            (127, b"\x7f"),
            (128, b"\x80\x01"),
            (300, b"\xac\x02"),
            (522, b"\x8a\x04"),  # the tag for field 65, wire type 2
        ],
    )
    def test_encodes_base128_little_endian(self, value: int, expected: bytes) -> None:
        assert proto.encode_varint(value) == expected

    @pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 522, 2**32, 2**63 - 1])
    def test_round_trips(self, value: int) -> None:
        assert proto.decode_varint(proto.encode_varint(value), 0) == (
            value,
            len(proto.encode_varint(value)),
        )

    def test_a_negative_value_is_refused_rather_than_sign_extended(self) -> None:
        with pytest.raises(ValueError):
            proto.encode_varint(-1)

    def test_running_off_the_end_is_truncated_not_corrupt(self) -> None:
        """`Truncated` means "read more bytes"; the decoder must not treat a
        split varint as a broken stream, or every BLE frame would fail."""
        with pytest.raises(proto.Truncated):
            proto.decode_varint(b"\x80", 0)


class TestEncoding:
    """Byte-for-byte. Each expected value is annotated with its derivation."""

    def test_ping(self) -> None:
        # body = field 1 (command_id) varint 1  -> 08 01
        #      + field 5 (system_ping_request) len 0 -> 2a 00
        # length prefix = 4
        assert proto.ping(1) == b"\x04\x08\x01\x2a\x00"

    def test_start_app_is_the_auto_launch(self) -> None:
        """The message that launches Antlia into bridge mode without anyone
        touching the Flipper's screen."""
        name = proto.ANTLIA_FAP_PATH
        assert len(name) == 24

        content = (
            b"\x0a\x18" + name.encode()  # field 1 (name), len 24
            + b"\x12\x03RPC"  # field 2 (args), len 3
        )
        assert len(content) == 31

        expected = (
            b"\x24"  # length prefix: 36
            b"\x08\x02"  # command_id = 2
            b"\x82\x01\x1f"  # field 16 (app_start_request), wire 2, len 31
            + content
        )
        assert proto.start_app(2, name=name, args=proto.RPC_LAUNCH_ARGS) == expected

    def test_data_exchange_carries_the_line_protocol(self) -> None:
        # field 65 -> tag (65<<3)|2 = 522 -> 8a 04
        expected = (
            b"\x0c"  # length prefix: 12
            b"\x08\x03"  # command_id = 3
            b"\x8a\x04\x07"  # field 65 (app_data_exchange_request), len 7
            b"\x0a\x05READ\n"  # field 1 (data), len 5
        )
        assert proto.data_exchange(3, b"READ\n") == expected

    def test_exit_and_stop_session(self) -> None:
        # field 47 -> (47<<3)|2 = 378 -> fa 02 ;  field 19 -> (19<<3)|2 = 154 -> 9a 01
        # Both bodies are 5 bytes: command_id (2) + tag (2) + zero length (1).
        assert proto.exit_app(4) == b"\x05\x08\x04\xfa\x02\x00"
        assert proto.stop_session(5) == b"\x05\x08\x05\x9a\x01\x00"

    def test_proto3_omits_zero_scalars_and_empty_strings(self) -> None:
        """Launching with no arguments must produce the bytes the Flipper mobile
        app sends, which means omitting `args` rather than sending an empty one."""
        assert proto.string_field(proto.START_ARGS, "") == b""
        assert proto.varint_field(proto.MAIN_HAS_NEXT, 0) == b""
        # ...but an empty *message* is still emitted, or the oneof would be unset
        # and the Flipper would not know which command this is.
        assert proto.bytes_field(proto.CONTENT_EMPTY, b"") == b"\x22\x00"


class TestDecoding:
    def _framed(self, content_field: int, content: bytes, *, command_id: int = 0) -> bytes:
        return proto.main(command_id, content_field, content)

    def test_reads_back_what_it_writes(self) -> None:
        raw = proto.data_exchange(7, b"OK 4K7T92M8")
        [frame] = proto.FrameDecoder().feed(raw)
        assert frame.command_id == 7
        assert frame.ok
        assert frame.data_exchange_payload == b"OK 4K7T92M8"

    def test_app_state_started_is_how_the_bridge_learns_antlia_is_ready(self) -> None:
        raw = self._framed(
            proto.CONTENT_APP_STATE_RESPONSE,
            proto.varint_field(proto.APP_STATE_STATE, proto.APP_STARTED),
        )
        [frame] = proto.FrameDecoder().feed(raw)
        assert frame.app_state == proto.APP_STARTED
        assert frame.command_id == 0, "unprompted, so the Flipper sends id 0"

    def test_an_omitted_state_field_is_app_closed_not_missing(self) -> None:
        """Proto3 omits a zero, and `APP_CLOSED` is zero. Reading the absence as
        `None` would make the exit notification invisible."""
        raw = self._framed(proto.CONTENT_APP_STATE_RESPONSE, b"")
        [frame] = proto.FrameDecoder().feed(raw)
        assert frame.app_state == proto.APP_CLOSED

    def test_a_nonzero_status_is_reported_by_number(self) -> None:
        body = proto.varint_field(proto.MAIN_COMMAND_ID, 9) + proto.varint_field(
            proto.MAIN_COMMAND_STATUS, 14
        )
        frame = proto.parse_main(body)
        assert not frame.ok
        assert frame.status == 14

    def test_unknown_content_fields_survive_as_bytes(self) -> None:
        """The bridge models six messages out of ~75. The other 69 must decode as
        an opaque frame rather than raising, or a Flipper mentioning its battery
        would kill the session."""
        raw = self._framed(33, b"\x08\x01")  # system_device_info_response
        [frame] = proto.FrameDecoder().feed(raw)
        assert frame.content_field == 33
        assert frame.data_exchange_payload is None
        assert frame.app_state is None


class TestFraming:
    """The decoder must tolerate any split. USB hands back endpoint-sized reads
    and BLE has a ~20-244 byte MTU, so a frame split mid-length-prefix is the
    ordinary case, not an edge one."""

    def test_one_byte_at_a_time(self) -> None:
        raw = proto.start_app(1, name=proto.ANTLIA_FAP_PATH, args=proto.RPC_LAUNCH_ARGS)
        decoder = proto.FrameDecoder()
        seen = [f for byte in raw for f in decoder.feed(bytes([byte]))]
        assert len(seen) == 1
        assert seen[0].command_id == 1
        assert not decoder.buffer, "a fully consumed frame must leave nothing behind"

    def test_several_frames_in_one_read(self) -> None:
        raw = proto.ping(1) + proto.ping(2) + proto.ping(3)
        frames = proto.FrameDecoder().feed(raw)
        assert [f.command_id for f in frames] == [1, 2, 3]

    def test_a_trailing_partial_frame_is_held_not_dropped(self) -> None:
        whole = proto.ping(1)
        partial = proto.ping(2)
        decoder = proto.FrameDecoder()
        frames = decoder.feed(whole + partial[:2])
        assert [f.command_id for f in frames] == [1]
        assert decoder.buffer == bytearray(partial[:2])
        assert [f.command_id for f in decoder.feed(partial[2:])] == [2]

    def test_a_payload_large_enough_to_need_a_two_byte_length(self) -> None:
        """A URL is short, but the length prefix crossing 127 is exactly the
        boundary a hand-rolled framer gets wrong."""
        payload = b"X" * 200
        raw = proto.data_exchange(1, payload)
        [frame] = proto.FrameDecoder().feed(raw)
        assert frame.data_exchange_payload == payload

    def test_an_unsupported_wire_type_is_fatal_rather_than_skipped(self) -> None:
        """Wire types 3 and 4 (deprecated groups) carry no length, so the decoder
        cannot find the next field. Waiting would never help."""
        with pytest.raises(proto.ProtocolError):
            proto.parse_main(b"\x0b")  # field 1, wire type 3
