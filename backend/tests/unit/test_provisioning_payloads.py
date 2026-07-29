"""The seam between the codec and the API's error vocabulary.

The payload rules themselves are tested in `idcodec/tests/test_tagpayload.py`,
where the station agent can also reach them. What is *only* true on this side is
the translation: `idcodec.tagpayload.normalize_tag_uid` raises `InvalidTagUid`,
and four provisioning routes catch `ProvisioningError`. If that translation is
dropped the codec's exception escapes as a 500 instead of the 422 the client
branches on — and every unit test of the folding itself would still pass.

So this file asserts the two things the refactor could silently break: the names
are still reachable at their old import paths, and `reason="invalid_tag_uid"`
still reaches a response body unchanged.
"""

from __future__ import annotations

import pytest
from idcodec.tagpayload import InvalidTagUid
from idcodec.tagpayload import normalize_tag_uid as codec_normalize_tag_uid

from app.services import provisioning, shortid
from app.services.provisioning import ProvisioningError


def test_a_bad_uid_is_raised_in_the_api_s_own_error_type() -> None:
    """`ProvisioningError`, not the codec's `InvalidTagUid`. The routes catch the
    former, and `app.api.routes.provisioning` maps this exact reason to 422."""
    with pytest.raises(ProvisioningError) as caught:
        provisioning.normalize_tag_uid("not-a-uid")
    assert caught.value.reason == "invalid_tag_uid"


def test_the_translation_preserves_the_codec_s_reason_rather_than_restating_it() -> None:
    """Two spellings of the wire contract is how it drifts. The route's status
    map is keyed on this string, so it is read off the codec's exception."""
    # `codec_normalize_tag_uid` is the function the station agent calls directly.
    with pytest.raises(InvalidTagUid) as raw:
        codec_normalize_tag_uid("not-a-uid")
    with pytest.raises(ProvisioningError) as translated:
        provisioning.normalize_tag_uid("not-a-uid")
    assert translated.value.reason == raw.value.reason


def test_a_good_uid_passes_through_the_wrapper_unchanged() -> None:
    assert provisioning.normalize_tag_uid("04:1A:2B:3C:4D:5E:6F") == "041A2B3C4D5E6F"


def test_parse_ndef_url_is_still_reachable_here() -> None:
    """Re-exported rather than moved out of reach: `app.services.provisioning` is
    where the tag payload rules live as far as this package's callers know."""
    code = shortid.generate()
    assert provisioning.parse_ndef_url(f"https://almagest.example/s/{code}") == code


def test_the_codec_is_still_reachable_through_app_services_shortid() -> None:
    """The compatibility promise for the hundreds of `shortid.validate(...)` call
    sites: the codec moved, the namespace did not."""
    code = shortid.generate()
    assert shortid.validate(code) == code
    assert shortid.normalize("BIN 4K7T-92M8") == "4K7T92M8"
    assert shortid.is_valid("4K7T92M8")
    assert shortid.format_display("4K7T92M8", "location") == "BIN 4K7T-92M8"
