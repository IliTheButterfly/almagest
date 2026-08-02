"""The one part of `Rc522TagSource` that is a decision rather than a register.

`agent.ndef` walks user memory one 4-byte page at a time, because that is what
the PN532 library offers. A Type 2 `READ` returns *four* pages for the same round
trip, so the RC522 driver serves the other three from a cache — which is a real
speed-up and also the only place in that file where a wrong answer, rather than a
slow one, could come from. Hence a test that reaches it without a chip.

`Rc522TagSource.__init__` opens SPI, so the instance here is built with
`__new__` and given the two attributes the page path uses. That is deliberately
crude: the alternative is a seam in the driver that exists only for tests, and
this file's whole subject is three methods.
"""

from __future__ import annotations

from agent import ndef
from agent.nfc_rc522 import Rc522TagSource


class _Memory:
    """A tag's user memory, answering `READ` and counting transactions."""

    def __init__(self, pages: dict[int, bytes]) -> None:
        self.pages = pages
        self.reads = 0

    def __call__(self, frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        from agent import iso14443a

        self.reads += 1
        first = frame[1]
        block = b"".join(self.pages.get(first + offset, b"\x00\x00\x00\x00") for offset in range(4))
        if first not in self.pages:
            return None  # past the end of memory: a NAK
        return iso14443a.with_crc(block)


def _driver(memory: _Memory) -> Rc522TagSource:
    source = Rc522TagSource.__new__(Rc522TagSource)
    source._blocks = {}
    source._transceive = memory  # type: ignore[method-assign, assignment]
    return source


def _tag_holding(url: str) -> _Memory:
    data = ndef.wrap_tlv(ndef.encode_uri_record(url))
    padded = data + b"\x00" * (-len(data) % 4)
    pages = {4 + index // 4: padded[index : index + 4] for index in range(0, len(padded), 4)}
    return _Memory(pages)


def test_a_page_read_serves_the_three_pages_that_came_with_it() -> None:
    memory = _tag_holding("https://almagest.lan/s/4K7T92M8")
    source = _driver(memory)

    first = source._read_page(4)
    assert first == memory.pages[4]
    assert memory.reads == 1

    assert source._read_page(5) == memory.pages[5]
    assert source._read_page(6) == memory.pages[6]
    assert source._read_page(7) == memory.pages[7]
    assert memory.reads == 1, "pages 5-7 arrived with page 4 and must not be re-read"


def test_the_whole_url_comes_back_in_about_two_transactions() -> None:
    """The claim in the module docstring, pinned. A 30-character URL is 21 bytes
    of payload, so the walk to the TLV terminator crosses two blocks — nine
    single-page round trips on the PN532 path, two here."""
    url = "https://almagest.lan/s/4K7T92M8"
    memory = _tag_holding(url)
    source = _driver(memory)

    data = ndef.collect_ndef_bytes(source._read_page)

    assert ndef.parse_uri_record(data) == url
    assert memory.reads <= 3


def test_the_cache_cannot_outlive_the_container_it_was_read_from() -> None:
    """`poll` clears it. Without that, a second container could be described by
    the first one's NDEF — a wrong short id that reads as perfectly valid, which
    is the failure this repo refuses everywhere else."""
    source = _driver(_tag_holding("https://almagest.lan/s/4K7T92M8"))
    source._read_page(4)
    assert source._blocks

    source._blocks.clear()  # what poll() does at the top of every poll
    assert not source._blocks


def test_a_read_past_the_end_of_memory_stops_the_walk() -> None:
    memory = _Memory({4: b"\x01\x02\x03\x04"})
    source = _driver(memory)
    assert source._read_page(4) == b"\x01\x02\x03\x04"
    assert source._read_page(8) is None
