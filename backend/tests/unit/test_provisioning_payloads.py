"""The two pure functions in provisioning: UID normalisation and NDEF parsing.

Both are small and both are where a whole class of false mismatch comes from. If
`04:1A:2B` and `041a2b` are recorded as two different tags, every verification
walk done with the other reader reports the whole cabinet as swapped — so the
folding is worth testing at this level rather than only through a route.
"""

from __future__ import annotations

import pytest

from app.services import provisioning, shortid
from app.services.provisioning import ProvisioningError


@pytest.mark.parametrize(
    "raw",
    [
        "041A2B3C4D5E6F",
        "04:1A:2B:3C:4D:5E:6F",
        "04 1a 2b 3c 4d 5e 6f",
        "04-1A-2B-3C-4D-5E-6F",
        "041a2b3c4d5e6f",
    ],
)
def test_every_rendering_of_one_uid_folds_to_the_same_string(raw: str) -> None:
    """A PN532 library, Web NFC and a human typing it out are all the same tag."""
    assert provisioning.normalize_tag_uid(raw) == "041A2B3C4D5E6F"


@pytest.mark.parametrize("raw", ["", "xy", "not-a-uid", "04G1", "0" * 33, "04 1A 2G"])
def test_a_non_uid_is_refused_rather_than_stored(raw: str) -> None:
    """A UID is hex from factory-locked pages; anything else arriving in that
    field is a reader fault or a mis-wired client, and storing it would create a
    binding nothing can ever match."""
    with pytest.raises(ProvisioningError) as caught:
        provisioning.normalize_tag_uid(raw)
    assert caught.value.reason == "invalid_tag_uid"


def test_the_short_id_is_read_out_of_the_payload() -> None:
    code = shortid.generate()
    assert provisioning.parse_ndef_url(f"https://almagest.example/s/{code}") == code


def test_the_host_is_not_part_of_the_payload() -> None:
    """`ALMAGEST_BASE_URL` is physically written into every tag, so it will
    eventually be wrong for tags already in the field. The short id is the
    meaning; the host is how that day's server was reached."""
    code = shortid.generate()
    for url in (
        f"http://localhost:8000/s/{code}",
        f"https://moved.example:8443/s/{code}",
        f"https://almagest.example/s/{code}/",
    ):
        assert provisioning.parse_ndef_url(url) == code


def test_a_payload_with_a_bad_check_symbol_is_rejected() -> None:
    """A tag is trusted no further than a scanned label: the mod-37 check is
    verified before the code is looked up."""
    code = shortid.generate()
    wrong = code[:-1] + ("0" if code[-1] != "0" else "1")
    assert provisioning.parse_ndef_url(f"https://almagest.example/s/{wrong}") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://almagest.example/",
        "https://almagest.example/parts/12",
        "not a url at all",
        "https://almagest.example/s/",
    ],
)
def test_a_payload_that_is_not_ours_carries_no_short_id(url: str) -> None:
    assert provisioning.parse_ndef_url(url) is None
