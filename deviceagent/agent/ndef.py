"""Getting a URI out of an NTAG2xx's user memory. Pure, byte-level, testable.

This is the part of talking to a PN532 that is a *decision* rather than a wire
transaction, so it lives here rather than in `agent.nfc_pn532`: given a way to
read one 4-byte page, everything from there to a URL string is arithmetic, and
arithmetic can be tested without a reader on the desk.

Two layers of format, both from the NFC Forum specs:

* **Type 2 Tag TLV blocks** fill user memory from page 4. Each is
  `tag [length] [value]`, where `0x03` is the NDEF message, `0x00` is padding,
  `0xFE` terminates, and a length of `0xFF` means the real length is the next two
  bytes big-endian. Lock- and memory-control TLVs (`0x01`, `0x02`) carry three
  bytes and are skipped.
* An **NDEF URI record**, whose payload is a one-byte abbreviation code followed
  by the rest of the URI. The code is why a tag holding
  `https://almagest.aether.lan/s/4K7T92M8` stores 21 bytes and not 29.

**Anything unexpected returns `None`.** Never a partial or guessed URL: the
caller falls back to the UID, which is a correct answer, whereas a
half-reassembled URL would be a wrong one that looks right. The same reasoning as
refusing an OCR'd part number.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

#: NFC Forum URI Record Type Definition, abbreviation code → prefix. Codes above
#: this table's range are reserved; a tag using one is not a tag this system
#: wrote, so it resolves to `None` rather than to a mangled string.
URI_PREFIXES: Final[tuple[str, ...]] = (
    "",  # 0x00 — no abbreviation, the URI is verbatim
    "http://www.",
    "https://www.",
    "http://",
    "https://",
    "tel:",
    "mailto:",
    "ftp://anonymous:anonymous@",
    "ftp://ftp.",
    "ftps://",
    "sftp://",
    "smb://",
    "nfs://",
    "ftp://",
    "dav://",
    "news:",
    "telnet://",
    "imap:",
    "rtsp://",
    "urn:",
    "pop:",
    "sip:",
    "sips:",
    "tftp:",
    "btspp://",
    "btl2cap://",
    "btgoep://",
    "tcpobex://",
    "irdaobex://",
    "file://",
    "urn:epc:id:",
    "urn:epc:tag:",
    "urn:epc:pat:",
    "urn:epc:raw:",
    "urn:epc:",
    "urn:nfc:",
)

TLV_NULL: Final = 0x00
TLV_LOCK_CONTROL: Final = 0x01
TLV_MEMORY_CONTROL: Final = 0x02
TLV_NDEF: Final = 0x03
TLV_TERMINATOR: Final = 0xFE

#: TNF 0x01, "NFC Forum well-known type" — the only TNF a URI record uses.
TNF_WELL_KNOWN: Final = 0x01
RECORD_TYPE_URI: Final = b"U"

#: NTAG213's user memory is pages 4-39: 36 pages, 144 bytes. Reading past the end
#: of a smaller tag is a failed read, which `collect_ndef_bytes` treats as the end
#: of the data rather than as an error — so this bound is a safety stop for a
#: reader that keeps answering, not the expected exit.
FIRST_USER_PAGE: Final = 4
DEFAULT_MAX_PAGES: Final = 36


def collect_ndef_bytes(
    read_page: Callable[[int], bytes | None],
    *,
    first_page: int = FIRST_USER_PAGE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> bytes:
    """Read user memory page by page, stopping as early as the data allows.

    Stops at the TLV terminator, at the first page that fails to read, or at
    `max_pages`. Stopping early is the point: an NTAG213 read one page at a time
    over UART is ~36 round trips, and a container sitting on the platform is
    polled several times a second, so reading only as far as the terminator is
    the difference between a responsive station and one that feels stuck.

    Returns whatever was gathered, including nothing. Deciding whether it means
    anything is `parse_uri_record`'s job.
    """
    gathered = bytearray()
    for offset in range(max_pages):
        page = read_page(first_page + offset)
        if not page:
            break
        gathered.extend(page)
        if TLV_TERMINATOR in page:
            break
    return bytes(gathered)


def _ndef_message(data: bytes) -> bytes | None:
    """Walk the TLV blocks and return the NDEF message's value bytes."""
    index = 0
    while index < len(data):
        tag = data[index]
        if tag == TLV_TERMINATOR:
            return None
        if tag == TLV_NULL:
            # Padding, no length byte. Skipping one byte at a time is correct and
            # is also how a blank (all-zero) tag terminates this loop.
            index += 1
            continue

        # Every remaining TLV carries a length. One byte, unless it is 0xFF, in
        # which case the length is the next two bytes big-endian.
        if index + 1 >= len(data):
            return None
        length = data[index + 1]
        value_start = index + 2
        if length == 0xFF:
            if index + 3 >= len(data):
                return None
            length = (data[index + 2] << 8) | data[index + 3]
            value_start = index + 4

        value_end = value_start + length
        if tag == TLV_NDEF:
            # A message truncated by the read budget is refused rather than
            # parsed as far as it goes: a URI cut short is a different URI.
            if value_end > len(data):
                return None
            return data[value_start:value_end]
        if tag in (TLV_LOCK_CONTROL, TLV_MEMORY_CONTROL):
            index = value_end
            continue
        # An unknown TLV type. Its length field is still trustworthy per the
        # spec, so skip it rather than giving up — but do not read its value.
        index = value_end
    return None


def parse_uri_record(data: bytes) -> str | None:
    """The URI carried by the first NDEF URI record in `data`, or `None`.

    Only the **first** record is considered. A multi-record message is not
    something this system writes (`{base_url}/s/{short_id}` and nothing else), so
    hunting through one for a URI would mean trusting a tag somebody else wrote
    more than the tag we wrote.
    """
    message = _ndef_message(data)
    if not message:
        return None

    header = message[0]
    if header & 0x07 != TNF_WELL_KNOWN:
        return None
    short_record = bool(header & 0x10)
    id_length_present = bool(header & 0x08)

    cursor = 1
    if cursor >= len(message):
        return None
    type_length = message[cursor]
    cursor += 1

    # SR (short record) means a single-byte payload length. A 4-byte length is
    # legal but means a payload larger than any NTAG21x holds, so it is decoded
    # for completeness rather than because it is expected.
    if short_record:
        if cursor >= len(message):
            return None
        payload_length = message[cursor]
        cursor += 1
    else:
        if cursor + 4 > len(message):
            return None
        payload_length = int.from_bytes(message[cursor : cursor + 4], "big")
        cursor += 4

    id_length = 0
    if id_length_present:
        if cursor >= len(message):
            return None
        id_length = message[cursor]
        cursor += 1

    record_type = message[cursor : cursor + type_length]
    cursor += type_length + id_length
    if record_type != RECORD_TYPE_URI:
        return None

    payload = message[cursor : cursor + payload_length]
    if len(payload) != payload_length or payload_length < 1:
        return None

    code = payload[0]
    if code >= len(URI_PREFIXES):
        return None
    try:
        rest = payload[1:].decode("utf-8")
    except UnicodeDecodeError:
        # A URI record is UTF-8 by definition, so this is a corrupt or
        # half-written tag. The UID fallback handles it.
        return None
    return URI_PREFIXES[code] + rest


def encode_uri_record(url: str) -> bytes:
    """The NDEF message bytes for `url`, abbreviated where possible.

    The inverse of `parse_uri_record`, and a round-trip test is the only check on
    that parser which does not consist of bytes typed out by the same person who
    wrote it. Since ADR 0014 this is also the real write path: `pages_for_uri`
    wraps it, and `Pn532TagSource.write_uri` puts the result on a tag.
    """
    code, prefix = 0, ""
    for candidate, text in enumerate(URI_PREFIXES):
        if text and url.startswith(text) and len(text) > len(prefix):
            code, prefix = candidate, text
    payload = bytes([code]) + url[len(prefix) :].encode("utf-8")
    if len(payload) > 0xFE:
        raise ValueError("URI too long for a short NDEF record")
    header = 0xD1  # MB | ME | SR, TNF=1
    return bytes([header, len(RECORD_TYPE_URI), len(payload)]) + RECORD_TYPE_URI + payload


#: Bytes in one Type 2 Tag page. Fixed by the tag, not a tuning knob.
PAGE_SIZE: Final = 4

#: User memory on an NTAG213, the tag PLAN.md specifies: pages 4-39, so 36 pages
#: and 144 bytes. A payload that does not fit is refused *before* anything is
#: written, because a write that runs off the end of the tag leaves the earlier
#: pages committed — a half-written tag rather than an untouched one.
NTAG213_USER_PAGES: Final = 36


def pages_for_uri(url: str, *, user_pages: int = NTAG213_USER_PAGES) -> list[bytes]:
    """The 4-byte pages to write to a blank Type 2 Tag, starting at `FIRST_USER_PAGE`.

    Zero-padded to a page boundary, because a Type 2 Tag write is page-atomic
    and there is no way to write three bytes. The padding lands after the
    terminator TLV, so a reader stops before it.

    Raises `ValueError` if the payload will not fit. That check is here rather
    than in the caller so that every writer — the PN532 today, a Flipper
    tomorrow — refuses the same payloads for the same reason. `NTAG215`/`216`
    have more room; passing a larger `user_pages` is how a caller says so, and
    nothing guesses on the tag's behalf.
    """
    body = wrap_tlv(encode_uri_record(url))
    padding = (-len(body)) % PAGE_SIZE
    body += b"\x00" * padding
    pages = [body[i : i + PAGE_SIZE] for i in range(0, len(body), PAGE_SIZE)]
    if len(pages) > user_pages:
        raise ValueError(f"{url!r} needs {len(pages)} pages and the tag has {user_pages}")
    return pages


def wrap_tlv(message: bytes) -> bytes:
    """`message` as it sits in a Type 2 Tag's user memory. Test helper's twin of
    `_ndef_message`, and the shape `collect_ndef_bytes` is expected to return."""
    if len(message) < 0xFF:
        return bytes([TLV_NDEF, len(message)]) + message + bytes([TLV_TERMINATOR])
    return (
        bytes([TLV_NDEF, 0xFF, (len(message) >> 8) & 0xFF, len(message) & 0xFF])
        + message
        + bytes([TLV_TERMINATOR])
    )
