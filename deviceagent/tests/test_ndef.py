"""Byte-level NDEF decoding: the part of "talk to a PN532" that is arithmetic.

Every case here is reachable with a real tag and none of it needs one, which is
the whole reason this logic is not inside the driver. The round-trip test is the
only assertion in this file that is not bytes typed out by the same person who
wrote the parser.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from idcodec.shortid import generate
from idcodec.tagpayload import parse_ndef_url

from agent import ndef


def pages(data: bytes) -> dict[int, bytes]:
    """Split TLV bytes into the 4-byte pages a reader hands back."""
    padded = data + bytes(-len(data) % 4)
    return {
        ndef.FIRST_USER_PAGE + index: padded[index * 4 : index * 4 + 4]
        for index in range(len(padded) // 4)
    }


def reader(mapping: dict[int, bytes]) -> Callable[[int], bytes | None]:
    return mapping.get


def test_a_tag_written_by_this_system_round_trips_to_the_same_short_id() -> None:
    """The end-to-end property: what the PWA writes over Web NFC is what the
    station reads back, and the short id survives with its check symbol intact."""
    code = generate()
    url = f"https://almagest.lan/s/{code}"
    memory = ndef.wrap_tlv(ndef.encode_uri_record(url))
    collected = ndef.collect_ndef_bytes(reader(pages(memory)))
    assert ndef.parse_uri_record(collected) == url
    assert parse_ndef_url(ndef.parse_uri_record(collected) or "") == code


@pytest.mark.parametrize(
    ("url", "code", "stored"),
    [
        ("https://almagest.lan/s/4K7T92M8", 0x04, b"almagest.lan/s/4K7T92M8"),
        ("http://almagest.lan/s/4K7T92M8", 0x03, b"almagest.lan/s/4K7T92M8"),
        ("https://www.example.com/x", 0x02, b"example.com/x"),
        ("weird://host/x", 0x00, b"weird://host/x"),
    ],
)
def test_the_abbreviation_code_is_the_prefix_that_was_dropped(
    url: str, code: int, stored: bytes
) -> None:
    """21 bytes instead of 29 on a 144-byte tag is not a micro-optimisation, and
    getting the code wrong silently corrupts the host, not the path."""
    record = ndef.encode_uri_record(url)
    payload = record[4:]
    assert payload[0] == code
    assert payload[1:] == stored
    assert ndef.parse_uri_record(ndef.wrap_tlv(record)) == url


def test_padding_and_lock_control_tlvs_are_skipped() -> None:
    """A tag formatted by another app can carry both before the NDEF block."""
    code = generate()
    url = f"https://almagest.lan/s/{code}"
    memory = (
        bytes([ndef.TLV_NULL, ndef.TLV_NULL])
        + bytes([ndef.TLV_LOCK_CONTROL, 3, 0xAA, 0xBB, 0xCC])
        + bytes([ndef.TLV_MEMORY_CONTROL, 3, 0x01, 0x02, 0x03])
        + ndef.wrap_tlv(ndef.encode_uri_record(url))
    )
    assert ndef.parse_uri_record(memory) == url


def test_a_three_byte_tlv_length_is_decoded() -> None:
    """A message over 254 bytes stores its length as `0xFF` plus two bytes. Bigger
    than an NTAG213 holds, so this is correctness rather than an expected case —
    but mis-reading the length shifts every following byte."""
    url = "https://almagest.lan/" + "a" * 300
    payload = bytes([0x04]) + url.removeprefix("https://").encode()
    record = bytes([0xC1, 1]) + len(payload).to_bytes(4, "big") + b"U" + payload
    memory = ndef.wrap_tlv(record)
    assert memory[1] == 0xFF
    assert ndef.parse_uri_record(memory) == url


def test_a_uri_too_long_for_a_short_record_is_refused_at_encode_time() -> None:
    with pytest.raises(ValueError, match="too long"):
        ndef.encode_uri_record("https://almagest.lan/" + "a" * 300)


def test_a_blank_tag_carries_nothing() -> None:
    """The normal state of a tag before provisioning. `None`, not an exception:
    the station's answer is "provision this container now"."""
    assert ndef.parse_uri_record(bytes(16)) is None


def test_a_terminator_before_any_ndef_block_is_nothing() -> None:
    assert ndef.parse_uri_record(bytes([ndef.TLV_TERMINATOR]) + bytes(8)) is None


def test_a_truncated_uri_is_refused_rather_than_partially_parsed() -> None:
    """A URI cut short is a different URI, and a different URI is a different
    container. The UID fallback is the correct answer here."""
    full = ndef.wrap_tlv(ndef.encode_uri_record("https://almagest.lan/s/4K7T92M8"))
    assert ndef.parse_uri_record(full[:12]) is None


def _two_record_message() -> bytes:
    """A URI record followed by a text record, in one NDEF message.

    Not something this system writes — a tag also formatted by another app.
    """
    uri = bytes([0x04]) + b"almagest.lan/s/4K7T92M8"
    text = b"\x02enhello"
    return (
        bytes([0x91, 1, len(uri)])  # MB | SR, not ME: another record follows
        + b"U"
        + uri
        + bytes([0x51, 1, len(text)])  # ME | SR
        + b"T"
        + text
    )


def test_only_the_first_record_of_a_message_is_read() -> None:
    """Documented behaviour, not an accident: hunting a multi-record message for a
    URI would mean trusting a tag someone else wrote more than one we wrote."""
    assert (
        ndef.parse_uri_record(ndef.wrap_tlv(_two_record_message()))
        == "https://almagest.lan/s/4K7T92M8"
    )


def test_a_message_shorter_than_its_tlv_promised_is_refused_even_if_a_record_parses() -> None:
    """The subtle half of truncation, and the reason the length check is not
    redundant.

    The first record can be complete and self-consistent inside a buffer the TLV
    header says should be longer — a read that stopped early on a marginal tag.
    Returning that URI would be reporting a successful read of a tag that was not
    fully read; the UID is in factory-locked pages and is the honest answer.
    """
    message = _two_record_message()
    memory = ndef.wrap_tlv(message)
    cut = 2 + len(message) - 6  # inside the second record, past the first
    assert ndef.parse_uri_record(memory[:cut]) is None


def test_a_text_record_is_not_a_uri_record() -> None:
    """TNF 1 type 'T'. Something else's tag, and guessing at its payload is how a
    hotel key card would resolve to a drawer."""
    payload = b"\x02enhello"
    record = bytes([0xD1, 1, len(payload)]) + b"T" + payload
    assert ndef.parse_uri_record(ndef.wrap_tlv(record)) is None


def test_a_non_well_known_tnf_is_refused() -> None:
    payload = b"\x04almagest.lan/s/4K7T92M8"
    record = bytes([0xD2, 1, len(payload)]) + b"U" + payload  # TNF 2 = MIME media
    assert ndef.parse_uri_record(ndef.wrap_tlv(record)) is None


def test_a_reserved_abbreviation_code_is_refused() -> None:
    """Codes past the table are reserved. Falling off the end of the prefix list
    must not be an IndexError, and must not be a bare path either."""
    payload = bytes([0xFE]) + b"almagest.lan/s/4K7T92M8"
    record = bytes([0xD1, 1, len(payload)]) + b"U" + payload
    assert ndef.parse_uri_record(ndef.wrap_tlv(record)) is None


def test_an_id_field_is_stepped_over() -> None:
    """The IL flag is rare but legal; mis-handling it shifts the payload by one
    byte, which turns `https://` into garbage rather than failing loudly."""
    url = "https://almagest.lan/s/4K7T92M8"
    payload = bytes([0x04]) + url.removeprefix("https://").encode()
    record = bytes([0xD9, 1, len(payload), 2]) + b"U" + b"id" + payload
    assert ndef.parse_uri_record(ndef.wrap_tlv(record)) == url


def test_a_long_form_payload_length_is_decoded() -> None:
    """No SR flag: a four-byte payload length."""
    url = "https://almagest.lan/s/4K7T92M8"
    payload = bytes([0x04]) + url.removeprefix("https://").encode()
    record = bytes([0xC1, 1]) + len(payload).to_bytes(4, "big") + b"U" + payload
    assert ndef.parse_uri_record(ndef.wrap_tlv(record)) == url


def test_invalid_utf8_in_a_uri_is_refused() -> None:
    """A half-written tag. The UID is still in factory-locked pages, so falling
    back is a correct answer where a mojibake URL would not be."""
    payload = bytes([0x04]) + b"almagest.lan/s/\xff\xfe"
    record = bytes([0xD1, 1, len(payload)]) + b"U" + payload
    assert ndef.parse_uri_record(ndef.wrap_tlv(record)) is None


# ---------------------------------------------------------------------------
# collect_ndef_bytes
# ---------------------------------------------------------------------------


def test_collection_stops_at_the_terminator_rather_than_reading_the_whole_tag() -> None:
    """36 pages is 36 UART round trips. A container is polled several times a
    second, so reading only as far as the terminator is the difference between a
    responsive station and one that feels stuck."""
    memory = pages(ndef.wrap_tlv(ndef.encode_uri_record("https://almagest.lan/s/4K7T92M8")))
    seen: list[int] = []

    def read_page(page: int) -> bytes | None:
        seen.append(page)
        return memory.get(page)

    ndef.collect_ndef_bytes(read_page)
    assert len(seen) == len(memory)


def test_collection_stops_at_the_first_failed_page() -> None:
    """Reading past the end of a smaller tag lands here, and it is the ordinary
    way a read of an NTAG213 ends rather than an error."""

    def read_page(page: int) -> bytes | None:
        return b"\x03\x02\xd0\x00" if page == ndef.FIRST_USER_PAGE else None

    assert ndef.collect_ndef_bytes(read_page) == b"\x03\x02\xd0\x00"


def test_collection_is_bounded_even_by_a_reader_that_never_stops() -> None:
    """A wedged reader that answers every page with data forever must not hang the
    poll loop; the bound is a safety stop, not the expected exit."""
    calls = 0

    def read_page(page: int) -> bytes | None:
        nonlocal calls
        calls += 1
        return b"\x00\x00\x00\x00"

    ndef.collect_ndef_bytes(read_page, max_pages=8)
    assert calls == 8
