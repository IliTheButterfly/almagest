"""The PN532 contract test. **Needs a real reader**, so it is skipped by default.

`make agent-test-live` runs it. Everything here is a claim about hardware that
has never been attached to this code, which is exactly why it exists as a
runnable test rather than as a paragraph in a README: the day a reader is wired
up, this is the checklist, and it either passes or it names what is wrong.

Run it with a provisioned tag held on the antenna. It asserts the two things the
whole design rests on — that a tag identifies with no gesture, and that the
station sees the *same* tag on consecutive polls rather than a stream of new ones.
"""

from __future__ import annotations

import pytest

from agent.config import get_settings
from agent.identity import VIA_NDEF, VIA_UID, identify
from agent.nfc_pn532 import Pn532TagSource
from agent.tags import TagSource, TagSourceError

pytestmark = pytest.mark.live


@pytest.fixture
def reader() -> Pn532TagSource:
    settings = get_settings()
    try:
        source = Pn532TagSource(settings.pn532_port)
    except TagSourceError as error:
        pytest.skip(f"no PN532 on {settings.pn532_port}: {error}")
    return source


def test_the_driver_satisfies_the_protocol_the_fake_stands_in_for(
    reader: Pn532TagSource,
) -> None:
    assert isinstance(reader, TagSource)
    reader.close()


def test_the_chip_answers_before_any_tag_is_involved(reader: Pn532TagSource) -> None:
    """A wrong port otherwise presents as "no tags ever", which is the worst
    possible diagnostic."""
    assert reader.firmware_version
    reader.close()


def test_a_provisioned_tag_reads_through_the_platform(reader: Pn532TagSource) -> None:
    """The design's biggest unverified claim, per PLAN.md: a bottom-pocket tag
    ~8-12 mm above the antenna through printed PETG, with no scanning gesture.

    Hold a provisioned container on the platform for the duration.
    """
    reads = [reader.poll() for _ in range(10)]
    reader.close()
    identities = [identify(read) for read in reads if read is not None]
    assert identities, "no tag answered in 10 polls — check antenna centring first"

    identified = [identity for identity in identities if identity.is_identified]
    assert identified, "the tag answered but neither carrier read"
    assert identified[0].via in {VIA_NDEF, VIA_UID}

    # The property the station depends on: one tag, not a stream of new ones. A
    # reader that reported a different UID per poll would make every poll look
    # like a container swap.
    assert len({identity.key for identity in identified}) == 1


def test_an_empty_platform_reads_as_no_tag(reader: Pn532TagSource) -> None:
    """Remove everything from the platform before running this.

    Trivial-looking, and it is the assertion that a stray metal object or a
    detuned antenna is not producing phantom presence — which would hold the
    station out of IDLE for ever.
    """
    assert all(reader.poll() is None for _ in range(5))
    reader.close()
