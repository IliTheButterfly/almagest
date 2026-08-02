"""The anticollision cascade, against scripted frames instead of a reader.

`agent.iso14443a`'s docstring names the bug this file exists to prevent: the
7-byte UID of an NTAG213 arrives in two pieces with a `0x88` cascade tag in front
of the first, and a driver that keeps the `0x88` or stops after the first piece
produces a UID that is wrong *and plausible*. Wrong-and-plausible is the failure
mode this whole codebase is arranged against — it is why `almagest-idcodec` is a
shared distribution rather than two copies.

So both cascade lengths are driven end to end here, and the malformed cases are
asserted to come back as *present but unreadable* rather than as a UID.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from agent import iso14443a
from agent.iso14443a import Selection

#: A real NTAG213 UID: 7 bytes, and it starts 0x04 because NXP's manufacturer
#: byte is 0x04. The cascade splits it 3 + 4.
NTAG_UID = bytes([0x04, 0xA2, 0x3B, 0x1C, 0x5D, 0x6E, 0x80])

#: A 4-byte UID, as a MIFARE Classic or an older tag would present. Not something
#: this station provisions, but the single-level path has to stay correct or the
#: cascade logic is only tested in one of its two shapes.
CLASSIC_UID = bytes([0x1A, 0x2B, 0x3C, 0x4D])

ATQA_SINGLE = bytes([0x44, 0x00])
SAK_COMPLETE = 0x00
SAK_INCOMPLETE = 0x04


def _bcc(part: bytes) -> bytes:
    return bytes([iso14443a.bcc(part)])


class ScriptedTag:
    """A tag that answers the frames a correct implementation would send.

    It asserts the *request* as well as producing a response, which is the half
    that matters: a driver that sent `0x93 0x20` twice and never advanced to
    `0x95` would otherwise pass by accident on a recorded-response fake.
    """

    def __init__(self, uid: bytes, *, atqa: bytes = ATQA_SINGLE) -> None:
        self.uid = uid
        self.atqa = atqa
        self.sent: list[bytes] = []
        self.halted = False

    def _levels(self) -> Iterator[tuple[int, bytes, int]]:
        """(SEL, the four bytes this level reports, SAK) per cascade level."""
        if len(self.uid) == 4:
            yield 0x93, self.uid, SAK_COMPLETE
        elif len(self.uid) == 7:
            yield 0x93, bytes([iso14443a.CASCADE_TAG]) + self.uid[:3], SAK_INCOMPLETE
            yield 0x95, self.uid[3:], SAK_COMPLETE
        else:  # pragma: no cover — a 10-byte UID is not exercised
            raise AssertionError(f"unscripted UID length {len(self.uid)}")

    def __call__(self, frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        self.sent.append(frame)
        if frame == bytes([iso14443a.CMD_WUPA]):
            assert tx_last_bits == iso14443a.SHORT_FRAME_BITS, "WUPA is a 7-bit short frame"
            return self.atqa
        if frame[:1] == bytes([iso14443a.CMD_HLTA]):
            self.halted = True
            return None
        for sel, part, sak in self._levels():
            if frame == bytes([sel, iso14443a.NVB_ANTICOLLISION]):
                return part + _bcc(part)
            if frame == iso14443a.with_crc(bytes([sel, iso14443a.NVB_SELECT]) + part + _bcc(part)):
                return iso14443a.with_crc(bytes([sak]))
        raise AssertionError(f"the driver sent a frame no tag would answer: {frame.hex()}")


def test_the_crc_matches_the_one_frame_the_spec_publishes() -> None:
    """HLTA is `50 00 57 CD` in ISO/IEC 14443-3 and in every implementation that
    ever worked. It is the only externally-fixed vector available without a
    reader, so it is the one that anchors the polynomial."""
    assert iso14443a.crc_a(bytes([0x50, 0x00])) == bytes([0x57, 0xCD])
    assert iso14443a.with_crc(bytes([0x50, 0x00])) == bytes([0x50, 0x00, 0x57, 0xCD])


def test_a_frame_with_a_bad_crc_is_discarded_rather_than_used() -> None:
    good = iso14443a.with_crc(b"\x0a\x0b")
    assert iso14443a.strip_crc(good) == b"\x0a\x0b"
    assert iso14443a.strip_crc(good[:-1] + b"\x00") is None
    assert iso14443a.strip_crc(b"\x01") is None
    assert iso14443a.strip_crc(None) is None


def test_a_seven_byte_uid_survives_the_cascade_whole() -> None:
    """The assertion this module was written for. A driver that kept the cascade
    tag would return 8 bytes starting 0x88; one that stopped at level 1 would
    return 4."""
    tag = ScriptedTag(NTAG_UID)
    selection = iso14443a.select_unique(tag)
    assert selection == Selection(present=True, uid=NTAG_UID, sak=SAK_COMPLETE)
    assert selection.uid is not None and len(selection.uid) == 7


def test_the_cascade_actually_advances_to_the_second_level() -> None:
    tag = ScriptedTag(NTAG_UID)
    iso14443a.select_unique(tag)
    assert bytes([0x95, iso14443a.NVB_ANTICOLLISION]) in tag.sent


def test_a_four_byte_uid_finishes_at_the_first_level() -> None:
    tag = ScriptedTag(CLASSIC_UID)
    assert iso14443a.select_unique(tag) == Selection(True, CLASSIC_UID, SAK_COMPLETE)
    assert all(frame[:1] != b"\x95" for frame in tag.sent)


def test_an_empty_field_is_not_the_same_answer_as_an_unreadable_tag() -> None:
    """The distinction the whole three-valued protocol rests on: no WUPA answer is
    an empty platform, a WUPA answer that then fails to select is a container
    whose tag will not read."""

    def silence(frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        return None

    assert iso14443a.select_unique(silence) == Selection(False)


class _CorruptibleTag(ScriptedTag):
    """`ScriptedTag` with one deliberate defect switched on at a time."""

    def __init__(self, uid: bytes) -> None:
        super().__init__(uid)
        self._bad_bcc = False
        self._bad_sak_crc = False
        self._lying_cascade = False

    def corrupt_bcc(self) -> None:
        self._bad_bcc = True

    def corrupt_sak_crc(self) -> None:
        self._bad_sak_crc = True

    def lie_about_cascade(self) -> None:
        """Report the cascade tag while claiming the UID is complete — which is
        how a UID quietly loses its first three bytes if the SAK is trusted
        alone."""
        self._lying_cascade = True

    def __call__(self, frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        answer = super().__call__(frame, tx_last_bits=tx_last_bits)
        if answer is None:
            return None
        if self._bad_bcc and frame == bytes([0x93, iso14443a.NVB_ANTICOLLISION]):
            return answer[:4] + bytes([answer[4] ^ 0xFF])
        if frame[:1] == b"\x93" and frame[1:2] == bytes([iso14443a.NVB_SELECT]):
            if self._bad_sak_crc:
                return answer[:-1] + bytes([answer[-1] ^ 0xFF])
            if self._lying_cascade:
                return iso14443a.with_crc(bytes([SAK_COMPLETE]))
        return answer


def _truncate_atqa(tag: _CorruptibleTag) -> None:
    tag.atqa = b"\x44"


@pytest.mark.parametrize(
    "corrupt",
    [
        _truncate_atqa,
        _CorruptibleTag.corrupt_bcc,
        _CorruptibleTag.corrupt_sak_crc,
        _CorruptibleTag.lie_about_cascade,
    ],
    ids=[
        "a truncated ATQA",
        "a wrong BCC",
        "a SAK that fails its CRC",
        "a cascade bit disagreeing with the cascade tag",
    ],
)
def test_a_malformed_answer_reads_as_present_but_unreadable(
    corrupt: Callable[[_CorruptibleTag], None],
) -> None:
    """Never a UID. Every one of these is a state a real tag reaches — half out of
    the field, two containers stacked, a reader picking up noise — and the only
    safe answer is the one the station already renders as "this tag will not
    read"."""
    tag = _CorruptibleTag(NTAG_UID)
    corrupt(tag)
    selection = iso14443a.select_unique(tag)
    assert selection.present
    assert selection.uid is None


def test_a_read_returns_four_pages_and_checks_its_crc() -> None:
    block = bytes(range(16))

    def answer(frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        assert frame == iso14443a.with_crc(bytes([iso14443a.CMD_READ, 4]))
        return iso14443a.with_crc(block)

    assert iso14443a.read_block(answer, 4) == block


@pytest.mark.parametrize(
    "reply",
    [
        None,  # no answer at all
        b"\x00",  # a NAK, which is four bits and so never a frame
        b"\x00" * 18,  # eighteen bytes whose CRC is wrong
    ],
    ids=["no answer", "a NAK", "a bad CRC"],
)
def test_a_read_that_did_not_come_back_clean_is_none(reply: bytes | None) -> None:
    """`agent.ndef` treats `None` as the end of the data, so this is also what
    stops the walk at the end of a smaller tag's memory."""

    def answer(frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        return reply

    assert iso14443a.read_block(answer, 4) is None


def test_halt_is_sent_with_its_crc() -> None:
    sent: list[bytes] = []

    def record(frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        sent.append(frame)
        return None

    iso14443a.halt(record)
    assert sent == [bytes([0x50, 0x00, 0x57, 0xCD])]


def test_polling_uses_wupa_because_a_halted_tag_ignores_reqa() -> None:
    """The one protocol choice here that a passing unit test would not catch on
    its own: with REQA a container identifies once and then reads as an empty
    platform until it is lifted, because `poll` halts the tag every time."""
    tag = ScriptedTag(NTAG_UID)
    iso14443a.select_unique(tag)
    assert tag.sent[0] == bytes([iso14443a.CMD_WUPA])
    assert iso14443a.CMD_WUPA != iso14443a.CMD_REQA
