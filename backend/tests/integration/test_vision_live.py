"""The live contract test that settles which wire shape each server accepts.

**This is the file that answers the one genuinely unverified question in the
vision path.** `vision_openai_compat` ships two transports because Ollama and vLLM
document different payloads, and the choice between them was made from
documentation rather than from a server that answered. Everything else about the
vision path is tested offline and is not waiting on anything; this is.

Never runs in CI, and needs a real vision model:

    ALMAGEST_VISION_BASE_URL=http://localhost:11434 \\
    ALMAGEST_VISION_MODEL=qwen3-vl:8b \\
    uv run pytest tests/integration/test_vision_live.py -m live

Against the cluster, `kubectl -n ili port-forward svc/almagest-llm 11434:11434`
first -- the service is deliberately ClusterIP and unauthenticated (see
`deploy/base/llm.yaml`), so it is not reachable any other way and must not be
made so.

## The assertions are about shape, never about the answer

A model is not required to read the label correctly to pass. It is required to
return **something `parse_response` accepts**: candidates within the requested
cap, each quoting a `source_text`, each with a confidence in range, and no field
the schema did not permit. Those are the properties the never-auto-accept rule is
built on, and a model or server upgrade is exactly what silently breaks one.

Asserting the part number here would make the test a measure of model quality,
which is what the benchmark is for. A contract test that fails when a model gets
one photograph wrong is a contract test people delete.

## Both transports are tried against whatever is configured

Deliberately. The interesting result is not "the default works" -- it is *which
of the two this server accepts*, including the case where both do. The test
records that rather than assuming it, and `test_the_configured_default_is_the_one
_that_works` is the one that fails if `for_base_url` is picking wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.enrichment.openai_compat import ModelUnavailable
from app.services.enrichment.vision import VisionRequest, VisionResult
from app.services.enrichment.vision_openai_compat import (
    ImageTransport,
    OpenAICompatVisionProvider,
    for_base_url,
)

#: The one photograph in the repository. A real creased DigiKey bag, which is a
#: far better contract-test input than a synthetic image: a model that returns
#: well-formed output on a clean render and malformed output on a real one has
#: broken exactly the contract this is checking.
PHOTO = (
    Path(__file__).parents[3]
    / "frontend"
    / "src"
    / "lib"
    / "capture"
    / "fixtures"
    / "digikey-creased-datamatrix.jpg"
)


def _config() -> tuple[str, str]:
    base_url = os.environ.get("ALMAGEST_VISION_BASE_URL")
    model = os.environ.get("ALMAGEST_VISION_MODEL")
    if not base_url or not model:
        pytest.skip(
            "set ALMAGEST_VISION_BASE_URL and ALMAGEST_VISION_MODEL to run this "
            "(and port-forward the cluster service first)"
        )
    return base_url, model


def _request(**overrides: object) -> VisionRequest:
    kwargs: dict[str, object] = {
        "image": PHOTO.read_bytes(),
        "media_type": "image/jpeg",
        "document_sha256": "ec12cd38add3e2a6e2a0ddf95dc1786d0577f9d7100e649586cda3aa7cea3d69",
    }
    kwargs.update(overrides)
    return VisionRequest(**kwargs)  # type: ignore[arg-type]


def _assert_satisfies_the_contract(result: VisionResult, request: VisionRequest) -> None:
    """Everything downstream is entitled to assume, and nothing more."""
    assert len(result.candidates) <= request.max_candidates
    for candidate in result.candidates:
        # The one that matters most: an assertion nobody can trace back to
        # characters on the label cannot be reviewed.
        assert candidate.source_text.strip()
        assert candidate.mpn.strip()
        assert 0.0 <= candidate.confidence <= 1.0
    if result.label_kind is not None:
        assert result.label_kind in {
            "reel",
            "cut_tape",
            "bag",
            "tray",
            "bare_part",
            "unknown",
        }
    # The call reported what it cost. The benchmark depends on this being true of
    # a real server and not only of the offline fakes.
    assert result.stats is not None
    assert result.stats.latency_ms >= 0


@pytest.mark.live
@pytest.mark.parametrize("transport", ["openai_content_parts", "ollama_native"])
def test_which_transports_this_server_accepts(transport: ImageTransport) -> None:
    """Run each shape and report. A refusal here is information, not a bug.

    If this fails for the transport `for_base_url` would have chosen, the default
    is wrong and `vision_openai_compat`'s module docstring is making a claim the
    server disagrees with -- fix the default, and correct the docstring rather
    than only the code.
    """
    base_url, model = _config()
    provider = OpenAICompatVisionProvider(base_url=base_url, model=model, image_transport=transport)
    request = _request()

    try:
        result = provider.read(request)
    except ModelUnavailable as error:
        pytest.skip(f"{transport} not accepted by this server: {error}")

    _assert_satisfies_the_contract(result, request)


@pytest.mark.live
def test_the_configured_default_is_the_one_that_works() -> None:
    """The assertion with teeth: whatever `for_base_url` picks must actually work.

    No skip-on-refusal here. The previous test is allowed to discover that a
    shape is unsupported; this one says the shape we ship for this server is not
    the unsupported one.
    """
    base_url, model = _config()
    request = _request()

    result = for_base_url(base_url, model).read(request)

    _assert_satisfies_the_contract(result, request)


@pytest.mark.live
def test_the_constraint_actually_constrains() -> None:
    """A server that accepted the schema and ignored it is the dangerous case.

    It looks identical to a working deployment until a model emits a field the
    schema forbade -- and the field this exists to forbid is a datasheet URL.
    `parse_response` raising here is the intended detection, so this asserting
    "no exception" is asserting the constraint held.
    """
    base_url, model = _config()
    request = _request(max_candidates=1)

    result = for_base_url(base_url, model).read(request)

    # Held to one by the decoder, not by a hopeful sentence in the prompt.
    assert len(result.candidates) <= 1


@pytest.mark.live
def test_a_prompt_token_count_comes_back_so_the_image_cost_is_visible() -> None:
    """`grab.ts` does not downscale, so a phone frame arrives at full resolution.

    Whether that is affordable is unmeasured, and this is the measurement. It
    prints rather than asserting a ceiling: the number is the point, and a
    threshold invented here would be a guess pretending to be a requirement.
    """
    base_url, model = _config()
    result = for_base_url(base_url, model).read(_request())

    assert result.stats is not None
    print(
        f"\nvision call: {result.stats.prompt_tokens} prompt tokens, "
        f"{result.stats.completion_tokens} completion, {result.stats.latency_ms} ms "
        f"for a {PHOTO.stat().st_size} byte JPEG"
    )
