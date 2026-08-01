"""ISO/IEC 14443-3 Type A, as arithmetic over a `transceive` callable.

The same split as `agent.ndef`, and for the same reason. Given a way to put a
frame on the air and get one back, everything from there to a UID is a decision:
which command, how many bits, is the checksum right, has the cascade finished.
Decisions can be tested with no reader on the desk, and **this file is where the
riskiest decision in the RC522 path lives** — assembling a 7-byte UID out of two
cascade levels.

That is not a hypothetical risk. `docs/PLAN.md` rejected the MFRC522 partly
because its Python ports "are UID-focused with hand-rolled NDEF across several
unmaintained forks", and the specific way those forks are wrong is that they
were written for MIFARE Classic's 4-byte UIDs: several return the cascade tag
`0x88` as if it were the first UID byte, or stop after cascade level 1 and hand
back four bytes of a seven-byte UID. NTAG213 has a 7-byte UID. In this system a
UID folded by a different rule "is invisible to the binding it should match while
looking perfectly correct in both places" (`deviceagent/pyproject.toml`), so that
class of bug does not fail loudly — it reports a whole cabinet as swapped. Hence
a pure module with `tests/test_iso14443a.py` driving both cascade lengths off
scripted frames, rather than a dependency nobody here can test.

**Single-tag anticollision only.** Every frame is sent whole-byte: the bit-oriented
collision-resolution loop of ISO 14443-3 §6.5.3.1 is not implemented, because the
station platform holds one container. Two tags in the field therefore corrupt the
anticollision answer, its BCC fails, and `select_unique` reports *present but
unreadable* — which is `TagRead(uid=None, ndef_url=None)`, a state the station
already renders as "this container's tag will not read". That is the honest
outcome; guessing which of two tags was meant would not be.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

#: 7-bit short frames. WUPA rather than REQA is what this module polls with:
#: after a SELECT a tag is ACTIVE and answers neither, and after the HALT that
#: ends every poll it answers only WUPA. Polling with REQA would identify a
#: container exactly once and then read an empty field until it was lifted.
CMD_WUPA: Final = 0x52
CMD_REQA: Final = 0x26
SHORT_FRAME_BITS: Final = 7

#: SELECT/anticollision, one per cascade level. A 4-byte UID finishes at level 1,
#: a 7-byte UID (every NTAG21x) at level 2; level 3 is 10-byte UIDs, which nothing
#: here uses but which costs one tuple entry to handle correctly.
SEL_CASCADE_LEVELS: Final[tuple[int, ...]] = (0x93, 0x95, 0x97)

#: Number of Valid Bits: 0x20 = "two bytes of this frame are valid", i.e. ask the
#: tag for its whole UID at this level. 0x70 = "seven bytes valid", a SELECT.
NVB_ANTICOLLISION: Final = 0x20
NVB_SELECT: Final = 0x70

#: Prefixed to the UID bytes at every cascade level *except* the last. It is not
#: part of the UID, and treating it as though it were is the classic 7-byte-UID
#: bug this module exists to not have.
CASCADE_TAG: Final = 0x88

#: SAK bit 3. Set means "UID not complete, run the next cascade level".
SAK_CASCADE: Final = 0x04

CMD_HLTA: Final = 0x50
CMD_READ: Final = 0x30

#: One READ returns four consecutive pages, not one. That is 16 bytes for the
#: same round trip a single page would cost, and `Rc522TagSource` caches the other
#: three — an NDEF read is then ~2 transactions rather than ~9.
READ_BLOCK_BYTES: Final = 16

ATQA_BYTES: Final = 2
ANTICOLLISION_ANSWER_BYTES: Final = 5


class Transceiver(Protocol):
    """Put one frame on the air, return the answer.

    `None` means nothing answered — an empty field or a tag that did not reply
    in time, which the caller distinguishes by *when* it happened. Raising is
    reserved for the reader itself having failed, exactly as in `agent.tags`.

    `tx_last_bits` is how many bits of the final byte to send, `0` meaning all
    eight. Only the short frames use it (7 bits), but it has to be in the
    signature because those frames are how a poll starts.
    """

    def __call__(self, frame: bytes, *, tx_last_bits: int = 0) -> bytes | None: ...


@dataclass(frozen=True, slots=True)
class Selection:
    """The outcome of one anticollision attempt.

    Three-valued in the same shape as `TagSource.poll`, because it is where two
    of those three values are decided:

    * `present=False` — nothing answered WUPA. The field is empty.
    * `present=True, uid=None` — something answered and then did not select: a
      damaged tag, a tag half out of the field, two tags colliding, or a card
      that is not Type A past the ATQA.
    * `present=True, uid=<bytes>` — a UID, 4, 7 or 10 bytes, cascade tags removed.
    """

    present: bool
    uid: bytes | None = None
    sak: int | None = None


def crc_a(data: bytes) -> bytes:
    """CRC_A over `data`, low byte first — ISO/IEC 14443-3 §6.2.4.

    Computed here rather than by the MFRC522's CRC coprocessor. The chip can do
    it, but doing it in Python costs microseconds, removes two register
    round-trips per frame, and above all makes every frame this module builds
    checkable in a unit test. `tests/test_iso14443a.py` pins it against the one
    frame the spec effectively publishes: HLTA is `50 00 57 CD`.
    """
    crc = 0x6363  # ITU-T v.41 with the Type A initial value
    for byte in data:
        scratch = byte ^ (crc & 0xFF)
        scratch = (scratch ^ (scratch << 4)) & 0xFF
        crc = ((crc >> 8) ^ (scratch << 8) ^ (scratch << 3) ^ (scratch >> 4)) & 0xFFFF
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def with_crc(data: bytes) -> bytes:
    return data + crc_a(data)


def strip_crc(frame: bytes | None) -> bytes | None:
    """The payload of `frame` if its CRC_A checks out, else `None`.

    A frame whose CRC fails is discarded rather than used: the whole reason the
    trailing two bytes are there is that the alternative to discarding it is
    acting on a corrupted UID or a corrupted page of NDEF.
    """
    if frame is None or len(frame) < 3:
        return None
    payload, checksum = frame[:-2], frame[-2:]
    return payload if crc_a(payload) == checksum else None


def bcc(uid_part: bytes) -> int:
    """The block check character: XOR of the four bytes of one cascade level."""
    return uid_part[0] ^ uid_part[1] ^ uid_part[2] ^ uid_part[3]


def select_unique(transceive: Transceiver) -> Selection:
    """Wake whatever is in the field and walk the cascade to a complete UID.

    The SAK's cascade bit is the authority on whether another level follows, and
    the cascade tag is cross-checked against it rather than trusted on its own: a
    level that claims to continue but does not carry `0x88`, or carries it while
    claiming to be the last, is a malformed answer and is refused. Refusing costs
    one unreadable poll; accepting either half of that disagreement is how a UID
    silently gains or loses a byte.
    """
    atqa = transceive(bytes([CMD_WUPA]), tx_last_bits=SHORT_FRAME_BITS)
    if atqa is None:
        return Selection(present=False)
    if len(atqa) != ATQA_BYTES:
        return Selection(present=True)

    uid = bytearray()
    for level in SEL_CASCADE_LEVELS:
        answer = transceive(bytes([level, NVB_ANTICOLLISION]))
        if answer is None or len(answer) != ANTICOLLISION_ANSWER_BYTES:
            return Selection(present=True)
        uid_part, check = answer[:4], answer[4]
        if bcc(uid_part) != check:
            return Selection(present=True)

        sak_frame = strip_crc(transceive(with_crc(bytes([level, NVB_SELECT]) + answer)))
        if sak_frame is None or len(sak_frame) != 1:
            return Selection(present=True)
        sak = sak_frame[0]

        incomplete = bool(sak & SAK_CASCADE)
        if incomplete != (uid_part[0] == CASCADE_TAG):
            return Selection(present=True)

        uid.extend(uid_part[1:] if incomplete else uid_part)
        if not incomplete:
            return Selection(present=True, uid=bytes(uid), sak=sak)

    # Three levels exhausted with the cascade bit still set. No UID is longer than
    # ten bytes, so this is a broken tag rather than an unsupported one.
    return Selection(present=True)


def read_block(transceive: Transceiver, page: int) -> bytes | None:
    """Type 2 Tag `READ`: 16 bytes, being `page` and the three after it.

    `None` covers both a NAK (a 4-bit answer, so never 18 bytes) and a CRC
    failure. Reading past the end of a tag's memory is a NAK on some parts and a
    wrapped read on others, which is why `agent.ndef` stops at the TLV terminator
    instead of relying on the read to end the walk.
    """
    frame = transceive(with_crc(bytes([CMD_READ, page])))
    payload = strip_crc(frame)
    if payload is None or len(payload) != READ_BLOCK_BYTES:
        return None
    return payload


def halt(transceive: Transceiver) -> None:
    """Send HLTA so the next poll's WUPA is answered.

    A correct tag does not reply, so there is nothing to check: per §6.4.3 any
    answer within the timeout means the HALT was *not* accepted, and the only
    consequence of that is the next poll finding a still-ACTIVE tag and reporting
    an empty field once. Not worth a branch — but worth knowing when a reader
    reports a container leaving and immediately arriving.
    """
    transceive(with_crc(bytes([CMD_HLTA, 0x00])))
