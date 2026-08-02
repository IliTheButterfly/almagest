"""The RC522 contract test. **Needs the reader wired up**, so it is skipped by default.

`make agent-test-live` runs it. The twin of `tests/test_pn532_live.py`, asserting
the same two things the design rests on — that a container identifies itself with
no gesture, and that consecutive polls see the *same* tag rather than a stream of
new ones — plus the two questions that are specific to this module and cannot be
answered anywhere else:

* that the UID has the length the tag really has. An NTAG213's is **7 bytes / 14
  hex characters**, and the classic MFRC522-library bug returns 4 of them or 8
  with a `0x88` in front. `tests/test_iso14443a.py` proves the cascade is right
  against scripted frames; only a real tag proves the frames are.
* whether this reader has the range the platform needs. It has less margin than a
  PN532 (ADR 0013), and PLAN.md already calls antenna centring the design's
  biggest unknown.

Run it with a provisioned tag held on the antenna.
"""

from __future__ import annotations

import pytest

from agent.config import get_settings
from agent.identity import VIA_NDEF, VIA_UID, identify
from agent.nfc_rc522 import Rc522TagSource
from agent.tags import TagSource, TagSourceError

pytestmark = pytest.mark.live


@pytest.fixture
def reader() -> Rc522TagSource:
    settings = get_settings()
    try:
        source = Rc522TagSource(
            bus=settings.rc522_spi_bus,
            device=settings.rc522_spi_device,
            speed_hz=settings.rc522_spi_hz,
        )
    except TagSourceError as error:
        pytest.skip(f"no RC522 on SPI: {error}")
    return source


def test_the_driver_satisfies_the_protocol_the_fake_stands_in_for(
    reader: Rc522TagSource,
) -> None:
    assert isinstance(reader, TagSource)
    reader.close()


def test_the_chip_answers_before_any_tag_is_involved(reader: Rc522TagSource) -> None:
    """VersionReg. A wrong bus, SPI left disabled or a floating RST otherwise all
    present as "no tags ever", which is the worst possible diagnostic — and on
    this module RST floating is the single most common wiring mistake."""
    assert reader.version not in (0x00, 0xFF)
    reader.close()


def test_a_provisioned_tag_reads_through_the_platform(reader: Rc522TagSource) -> None:
    """The design's biggest unverified claim, per PLAN.md, with less antenna
    margin than the reader it was claimed for.

    Hold a provisioned container on the platform for the duration.
    """
    reads = [reader.poll() for _ in range(10)]
    reader.close()
    identities = [identify(read) for read in reads if read is not None]
    assert identities, "no tag answered in 10 polls — check antenna centring first"

    identified = [identity for identity in identities if identity.is_identified]
    assert identified, "the tag answered but neither carrier read"
    assert identified[0].via in {VIA_NDEF, VIA_UID}
    assert len({identity.key for identity in identified}) == 1


def test_the_uid_is_the_length_the_tag_actually_has(reader: Rc522TagSource) -> None:
    """The assertion this driver exists to get right. An NTAG21x UID is 7 bytes;
    14 hex characters. **4 bytes means the cascade stopped at level 1 and every
    binding written from this reader is wrong**, and 16 means the cascade tag was
    kept. Both look like perfectly ordinary UIDs on a screen.

    Skips rather than fails on a non-NTAG card, since a 4-byte UID is correct for
    one — but says so, because "it passed" would be the wrong thing to remember.
    """
    reads = [reader.poll() for _ in range(10)]
    reader.close()
    uids = {read.uid for read in reads if read is not None and read.uid}
    assert uids, "no UID read in 10 polls"
    assert len(uids) == 1, f"one tag on the platform reported several UIDs: {uids}"

    uid = uids.pop()
    if len(uid) == 8:
        pytest.skip(f"{uid} is a 4-byte UID: not an NTAG21x, so this proves nothing")
    assert len(uid) == 14, f"expected 14 hex characters for an NTAG21x UID, got {uid!r}"
    assert not uid.startswith("88"), "the cascade tag was kept as a UID byte"


def test_an_empty_platform_reads_as_no_tag(reader: Rc522TagSource) -> None:
    """Remove everything from the platform before running this.

    Trivial-looking, and it is the assertion that a stray metal object or a
    detuned antenna is not producing phantom presence — which would hold the
    station out of IDLE for ever.
    """
    assert all(reader.poll() is None for _ in range(5))
    reader.close()


def test_an_empty_poll_is_cheap_enough_for_the_budgets(reader: Rc522TagSource) -> None:
    """README.md item 2, finally measurable for one of the two readers.

    The removal debounce and the identify budget are counts of *empty or failed*
    polls, so what they cost in seconds is what an empty poll costs. The PN532
    spends its full 250 ms timeout here; this driver should spend ~25 ms, the
    chip's own timer. Bounded generously at 100 ms, because the claim being
    checked is "comfortably inside a 300 ms interval", not a benchmark.

    Run with the platform empty.
    """
    import time

    started = time.monotonic()
    polls = 10
    for _ in range(polls):
        reader.poll()
    elapsed = (time.monotonic() - started) / polls
    reader.close()

    assert elapsed < 0.1, f"an empty poll cost {elapsed * 1000:.0f} ms"
