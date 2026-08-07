"""Reading a photograph into ranked proposals: the schema, the parser, the fake.

## What this file is really guarding

**The prohibitions that are supposed to be unrepresentable.** `CLAUDE.md` forbids
auto-accepting a model-read part number and ADR 0017 forbids the model asserting a
datasheet URL. Those are rules a future edit could quietly break, so `schema_for`
is written to make them undecodable rather than merely rejected -- and the tests
below assert the *absence* of `url`, `quantity`, `date_code` and `lot_code`
properties. An assertion about something not existing looks odd until you notice
that adding one of them back is a two-line change nobody would flag in review.

**Empty is an answer.** A model that cannot read a creased label must be able to
say so, and the queue distinguishes that (`UNIDENTIFIED`) from a run that broke
(`FAILED`). If `parse_response` ever started treating an empty candidate list as
malformed, every illegible capture would be reported as a system fault and the
distinction `research.py` fought for would be lost one stage earlier.

**A server that ignores the constraint.** Same guard `test_openai_compat.py`
describes: `response_format` is advisory in the sense that nothing stops a backend
accepting the field and sampling freely. So more candidates than were asked for is
refused rather than truncated -- truncating would hide the very fact worth knowing.

Everything here is offline. There is no model, no network and no image decoding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.enrichment.vision import (
    DEFAULT_MAX_CANDIDATES,
    LABEL_KINDS,
    FakeVisionProvider,
    IdentityCandidate,
    VisionFixtureMiss,
    VisionRequest,
    VisionResponseError,
    VisionResult,
    parse_response,
    schema_for,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "vision" / "capture_identities.json"

#: The real sha256 of frontend/src/lib/capture/fixtures/digikey-creased-datamatrix.jpg.
ANCHORED_SHA = "ec12cd38add3e2a6e2a0ddf95dc1786d0577f9d7100e649586cda3aa7cea3d69"
UNANCHORED_SHA = "5b7f0d1a4c9e2836af51d0c7b3e64912d85a07f3c6b19e40d2a85f31c704b6e9"
ILLEGIBLE_SHA = "c3a91e77b0d4265faf83c10e9d7b45286103fae9d24c8b7051fd93a2e6081b4c"

JPEG_MAGIC = b"\xff\xd8\xff\xe0stand-in for a photograph"


def _request(sha: str = ANCHORED_SHA, **overrides: Any) -> VisionRequest:
    kwargs: dict[str, Any] = {
        "image": JPEG_MAGIC,
        "media_type": "image/jpeg",
        "document_sha256": sha,
    }
    kwargs.update(overrides)
    return VisionRequest(**kwargs)


def _response(*candidates: dict[str, Any], label_kind: str | None = "bag") -> dict[str, Any]:
    return {"candidates": list(candidates), "label_kind": label_kind}


def _candidate(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "mpn": "CF14JT100K",
        "manufacturer": "Stackpole Electronics Inc",
        "package": "Axial",
        "confidence": 0.61,
        "source_text": "CF14JT100K",
        "note": None,
    }
    body.update(overrides)
    return body


def _parse(payload: object, request: VisionRequest | None = None) -> VisionResult:
    return parse_response(payload, request or _request(), provider="test", model="test-model")


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def test_a_request_without_an_image_is_refused() -> None:
    with pytest.raises(ValueError, match="image is empty"):
        _request(image=b"")


def test_a_request_must_name_the_document_it_is_about() -> None:
    # The sha256 is the provenance: it is what a reviewer opens and what the
    # result is keyed by. A result that cannot name its image cannot be reviewed.
    with pytest.raises(ValueError, match="document_sha256"):
        _request(document_sha256="")


def test_max_candidates_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _request(max_candidates=0)


def test_a_barcode_anchors_the_read() -> None:
    assert _request(barcode_texts=("CF14JT100K",)).anchored is True
    assert _request(ocr_lines=("CF14JT100K",)).anchored is False


# ---------------------------------------------------------------------------
# The schema, and what it makes impossible
# ---------------------------------------------------------------------------


def _candidate_properties(request: VisionRequest) -> dict[str, Any]:
    items = schema_for(request)["properties"]["candidates"]["items"]
    assert items["additionalProperties"] is False
    properties: dict[str, Any] = items["properties"]
    return properties


@pytest.mark.parametrize("forbidden", ["url", "datasheet_url", "quantity", "date_code", "lot_code"])
def test_the_schema_has_no_property_the_model_could_assert_a_fact_with(forbidden: str) -> None:
    """ADR 0017, enforced by the decoder rather than by a reviewer's memory.

    A model asked for a datasheet URL produces a well-formed, plausible,
    frequently nonexistent one, and the failure is silent because a 404 reads as
    a network problem rather than a fabrication. It cannot produce one it has no
    field for. Quantity, date code and lot code are absent for a different
    reason: they come off the barcode deterministically, and a second worse
    source for a solved problem is not an improvement.
    """
    assert forbidden not in _candidate_properties(_request())


def test_source_text_is_required_and_bounded() -> None:
    items = schema_for(_request())["properties"]["candidates"]["items"]
    assert "source_text" in items["required"]
    assert items["properties"]["source_text"]["minLength"] == 1
    assert items["properties"]["source_text"]["maxLength"] == 500


def test_the_schema_caps_candidates_at_what_was_asked_for() -> None:
    assert schema_for(_request())["properties"]["candidates"]["maxItems"] == DEFAULT_MAX_CANDIDATES
    # An anchored read is held to one candidate by the decoder, not by a hopeful
    # sentence in the prompt.
    assert schema_for(_request(max_candidates=1))["properties"]["candidates"]["maxItems"] == 1


def test_label_kind_is_a_closed_enum() -> None:
    schema = schema_for(_request())["properties"]["label_kind"]
    assert set(schema["enum"]) == {*LABEL_KINDS, None}


def test_the_schema_is_json_serialisable() -> None:
    # It is sent over the wire verbatim in two different spellings; a schema
    # holding a tuple or a set would fail at the transport rather than here.
    json.dumps(schema_for(_request()))


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_a_well_formed_response_parses_into_ranked_candidates() -> None:
    result = _parse(
        _response(
            _candidate(),
            _candidate(mpn="CFR4JT100K", manufacturer=None, confidence=0.22),
        )
    )
    assert [c.mpn for c in result.candidates] == ["CF14JT100K", "CFR4JT100K"]
    assert result.best is not None and result.best.mpn == "CF14JT100K"
    assert result.candidates[1].manufacturer is None
    assert result.label_kind == "bag"
    assert result.identified is True
    assert result.document_sha256 == ANCHORED_SHA


def test_an_empty_candidate_list_is_a_normal_answer() -> None:
    """The single most important assertion in this file.

    "I cannot tell what this is" settles a queue entry as UNIDENTIFIED, which is
    deliberately not FAILED -- a photograph problem whose fix is another
    photograph, rather than a broken run. If this ever raised, every illegible
    capture would be reported as a system fault.
    """
    result = _parse(_response(label_kind="bare_part"))
    assert result.candidates == ()
    assert result.identified is False
    assert result.best is None
    assert result.label_kind == "bare_part"


def test_more_candidates_than_requested_is_refused_not_truncated() -> None:
    payload = _response(
        _candidate(mpn="A1"), _candidate(mpn="A2"), _candidate(mpn="A3"), _candidate(mpn="A4")
    )
    with pytest.raises(VisionResponseError, match="more than the 3 requested"):
        _parse(payload)


def test_a_candidate_without_source_text_is_refused() -> None:
    # An assertion nobody can trace back to characters on the label cannot be
    # reviewed, and an unreviewable assertion is what this pipeline exists not to
    # store.
    with pytest.raises(VisionResponseError, match="source_text"):
        _parse(_response(_candidate(source_text="   ")))


def test_a_candidate_without_a_part_number_is_refused() -> None:
    with pytest.raises(VisionResponseError, match="mpn"):
        _parse(_response(_candidate(mpn="")))


@pytest.mark.parametrize("confidence", [-0.1, 1.4, "high", True, None])
def test_a_confidence_outside_zero_to_one_is_refused(confidence: object) -> None:
    with pytest.raises(VisionResponseError, match="confidence"):
        _parse(_response(_candidate(confidence=confidence)))


def test_the_same_printed_string_twice_is_refused() -> None:
    with pytest.raises(VisionResponseError, match="more than once"):
        _parse(_response(_candidate(), _candidate()))


def test_two_readings_differing_only_in_punctuation_are_both_kept() -> None:
    """Deduplication is on the printed string, deliberately not a normalised form.

    Which of `CF14JT100K` and `CF14JT-100K` is right is exactly what the datasheet
    fetch will settle, by finding one of them in a PDF and not the other.
    Collapsing them here would throw the alternative away before anything had a
    chance to test it.
    """
    result = _parse(_response(_candidate(), _candidate(mpn="CF14JT-100K")))
    assert [c.mpn for c in result.candidates] == ["CF14JT100K", "CF14JT-100K"]


def test_an_unknown_label_kind_is_refused() -> None:
    with pytest.raises(VisionResponseError, match="label_kind"):
        _parse(_response(_candidate(), label_kind="shrink_wrap"))


def test_a_blank_optional_field_becomes_none() -> None:
    # Several servers spell "I do not know" as an empty string under a schema
    # that permits a string. That is an answer, not a malformed response.
    result = _parse(_response(_candidate(manufacturer="", package="  ", note="")))
    assert result.candidates[0].manufacturer is None
    assert result.candidates[0].package is None
    assert result.candidates[0].note is None


def test_candidates_must_be_an_array() -> None:
    with pytest.raises(VisionResponseError, match="candidates must be an array"):
        _parse({"candidates": {"mpn": "CF14JT100K"}})


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


def test_the_fake_replays_the_anchored_case_through_the_real_parser() -> None:
    provider = FakeVisionProvider(FIXTURE)
    request = _request(ANCHORED_SHA, barcode_texts=("CF14JT100K",), max_candidates=1)
    result = provider.read(request)

    assert result.candidates == (
        IdentityCandidate(
            mpn="CF14JT100K",
            manufacturer="Stackpole Electronics Inc",
            confidence=0.93,
            source_text="CF14JT100K",
            package="Axial",
            note=(
                "Part number is also carried in the Data Matrix; the printed line "
                "agrees with it character for character."
            ),
        ),
    )
    assert provider.calls == [request]


def test_the_fake_replays_the_unanchored_case_with_a_loser_kept() -> None:
    result = FakeVisionProvider(FIXTURE).read(_request(UNANCHORED_SHA))
    assert [c.mpn for c in result.candidates] == ["CF14JT100K", "CFR4JT100K"]
    # Ranked, and the runner-up is kept rather than discarded: it is what a
    # reviewer needs in order to see that the winner was chosen over something.
    assert result.candidates[0].confidence > result.candidates[1].confidence


def test_the_fake_replays_an_illegible_capture_as_no_candidates() -> None:
    result = FakeVisionProvider(FIXTURE).read(_request(ILLEGIBLE_SHA))
    assert result.identified is False


def test_the_fake_refuses_a_document_it_has_no_recording_for() -> None:
    # Not an empty result: empty is meaningful here, so returning it on a miss
    # would let a test assert the unidentified path while actually testing a typo.
    with pytest.raises(VisionFixtureMiss, match="no recorded response"):
        FakeVisionProvider(FIXTURE).read(_request("0" * 64))


def test_the_fixture_still_matches_the_committed_photograph() -> None:
    """The anchored key is the real sha256 of the one photograph in the repo.

    If `frontend/src/lib/capture/fixtures/digikey-creased-datamatrix.jpg` is ever
    replaced, this fixture is describing an image that no longer exists and the
    "recorded from a real read" claim in its `_provenance` stops being true.
    """
    from hashlib import sha256

    photo = (
        Path(__file__).parents[3]
        / "frontend"
        / "src"
        / "lib"
        / "capture"
        / "fixtures"
        / "digikey-creased-datamatrix.jpg"
    )
    assert sha256(photo.read_bytes()).hexdigest() == ANCHORED_SHA
