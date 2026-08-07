"""Sending a photograph to two servers that disagree about how to receive one.

## What this file is really guarding

**The wire shapes not silently converging.** The whole reason two transports exist
is that Ollama and vLLM want different payloads, and the failure mode is that
somebody "simplifies" one into the other and it works against whichever server
they had running. Each shape is asserted here in full: where the image bytes go,
whether they carry a `data:` prefix, and -- the one that actually matters -- how
the JSON schema is attached.

**The constraint surviving the transport choice.** `vision.schema_for` is what
makes a datasheet URL unrepresentable rather than merely discouraged. If a
transport dropped the schema, the module's central safety property would quietly
become a suggestion, and every test in `test_vision.py` would still pass. So both
shapes are asserted to carry the schema, and both are asserted to carry the
`source_text` requirement inside it.

**Token accounting in two vocabularies.** Ollama counts in `prompt_eval_count`
and `eval_count`; the OpenAI shape uses a `usage` block. The prompt count is the
number that will decide whether sending a 4K frame whole is affordable, so
reading it wrong in one of the two shapes would be a silent hole in exactly the
measurement the benchmark exists to take.

Everything here is offline -- `urlopen` is substituted. The live contract tests
that settle which shape each server *actually* accepts are in
`tests/integration/test_vision_live.py`, and are skipped by default.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest

from app.services.enrichment import vision_openai_compat
from app.services.enrichment.openai_compat import ModelUnavailable
from app.services.enrichment.vision import VisionRequest
from app.services.enrichment.vision_openai_compat import (
    OpenAICompatVisionProvider,
    for_base_url,
)

IMAGE = b"\xff\xd8\xff\xe0not-really-a-jpeg-but-bytes-are-bytes"
SHA = "ec12cd38add3e2a6e2a0ddf95dc1786d0577f9d7100e649586cda3aa7cea3d69"

ANSWER = {
    "candidates": [
        {
            "mpn": "CF14JT100K",
            "manufacturer": "Stackpole Electronics Inc",
            "package": "Axial",
            "confidence": 0.9,
            "source_text": "CF14JT100K",
            "note": None,
        }
    ],
    "label_kind": "bag",
}


def _request(**overrides: Any) -> VisionRequest:
    kwargs: dict[str, Any] = {
        "image": IMAGE,
        "media_type": "image/jpeg",
        "document_sha256": SHA,
    }
    kwargs.update(overrides)
    return VisionRequest(**kwargs)


class _Capture:
    """Stands in for `urlopen`, recording the request and replaying a body."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.sent: dict[str, Any] = {}
        self.url = ""

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        self.url = request.full_url
        self.sent = json.loads(request.data)
        return _Response(json.dumps(self.body).encode())


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _openai_body(content: object = None, **extra: Any) -> dict[str, Any]:
    text = json.dumps(ANSWER) if content is None else content
    choice: dict[str, Any] = {"message": {"content": text}, "finish_reason": "stop"}
    return {"choices": [choice], **extra}


def _ollama_body(content: object = None, **extra: Any) -> dict[str, Any]:
    text = json.dumps(ANSWER) if content is None else content
    return {"message": {"content": text}, "done_reason": "stop", **extra}


def _openai() -> OpenAICompatVisionProvider:
    return OpenAICompatVisionProvider(
        base_url="http://vllm.test:8000",
        model="almagest-large",
        image_transport="openai_content_parts",
    )


def _ollama() -> OpenAICompatVisionProvider:
    return OpenAICompatVisionProvider(
        base_url="http://ollama.test:11434", model="qwen3-vl:8b", image_transport="ollama_native"
    )


def _patch(monkeypatch: pytest.MonkeyPatch, capture: _Capture) -> None:
    monkeypatch.setattr(vision_openai_compat.urllib.request, "urlopen", capture)


# ---------------------------------------------------------------------------
# Picking a transport
# ---------------------------------------------------------------------------


def test_the_server_decides_the_transport_so_no_caller_has_to_remember() -> None:
    assert for_base_url("http://almagest-llm:11434", "qwen3-vl:8b").image_transport == (
        "ollama_native"
    )
    assert for_base_url("http://almagest-llm-27b:8000", "almagest-large").image_transport == (
        "openai_content_parts"
    )


# ---------------------------------------------------------------------------
# The OpenAI content-part shape
# ---------------------------------------------------------------------------


def test_the_openai_shape_sends_a_data_uri_in_a_content_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _Capture(_openai_body())
    _patch(monkeypatch, capture)

    _openai().read(_request())

    assert capture.url == "http://vllm.test:8000/v1/chat/completions"
    parts = capture.sent["messages"][1]["content"]
    assert isinstance(parts, list)
    image_part = next(part for part in parts if part["type"] == "image_url")
    # Nested under `url`, and prefixed. Both halves are the spec and both are
    # what Ollama's documentation does NOT show, which is why this is a shape of
    # its own rather than one payload for everybody.
    assert image_part["image_url"]["url"] == (
        "data:image/jpeg;base64," + base64.b64encode(IMAGE).decode()
    )


def test_the_openai_shape_sends_the_schema_in_both_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _Capture(_openai_body())
    _patch(monkeypatch, capture)

    _openai().read(_request())

    assert capture.sent["response_format"]["json_schema"]["strict"] is True
    schema = capture.sent["response_format"]["json_schema"]["schema"]
    assert capture.sent["guided_json"] == schema
    assert capture.sent["temperature"] == 0


def test_the_openai_shape_reads_the_usage_block(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture(_openai_body(usage={"prompt_tokens": 2100, "completion_tokens": 64}))
    _patch(monkeypatch, capture)

    stats = _openai().read(_request()).stats

    assert stats is not None
    assert (stats.prompt_tokens, stats.completion_tokens) == (2100, 64)


# ---------------------------------------------------------------------------
# The Ollama native shape
# ---------------------------------------------------------------------------


def test_the_ollama_shape_sends_bare_base64_with_no_data_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The difference that would otherwise be found in production at 2 a.m."""
    capture = _Capture(_ollama_body())
    _patch(monkeypatch, capture)

    _ollama().read(_request())

    assert capture.url == "http://ollama.test:11434/api/chat"
    message = capture.sent["messages"][1]
    assert message["images"] == [base64.b64encode(IMAGE).decode()]
    assert not message["images"][0].startswith("data:")
    # And the content stays a plain string rather than a list of parts.
    assert isinstance(message["content"], str)


def test_the_ollama_shape_carries_the_schema_in_its_format_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason `ollama_native` is the default for Ollama at all.

    `response_format: json_schema` is undocumented on Ollama's compatibility
    endpoint; `format` taking a whole schema is documented. Constrained decoding
    is what makes a URL unrepresentable, so it must not be the part that is
    guessed at.
    """
    capture = _Capture(_ollama_body())
    _patch(monkeypatch, capture)

    _ollama().read(_request())

    schema = capture.sent["format"]
    assert schema["properties"]["candidates"]["items"]["required"] == [
        "mpn",
        "confidence",
        "source_text",
    ]
    assert capture.sent["options"]["temperature"] == 0
    assert capture.sent["stream"] is False


def test_the_ollama_shape_reads_its_own_token_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `prompt_eval_count`/`eval_count`, not a `usage` block. Getting this wrong
    # would leave a silent hole in the one measurement that says whether a
    # full-resolution frame is affordable.
    capture = _Capture(_ollama_body(prompt_eval_count=1899, eval_count=57))
    _patch(monkeypatch, capture)

    stats = _ollama().read(_request()).stats

    assert stats is not None
    assert (stats.prompt_tokens, stats.completion_tokens) == (1899, 57)
    assert stats.latency_ms >= 0


# ---------------------------------------------------------------------------
# Both shapes: the prompt, and what it does with the browser's readings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", [_openai, _ollama])
def test_a_barcode_is_offered_as_the_stronger_reading(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    capture = _Capture(_openai_body() if build is _openai else _ollama_body())
    _patch(monkeypatch, capture)

    build().read(_request(barcode_texts=("1PCF14JT100K",), max_candidates=1))

    sent = json.dumps(capture.sent)
    assert "1PCF14JT100K" in sent
    assert "checksummed" in sent
    # Anchored, so the model is asked to confirm rather than to range over guesses.
    assert "Confirm the part number" in sent


@pytest.mark.parametrize("build", [_openai, _ollama])
def test_ocr_lines_are_offered_as_the_weaker_reading(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    """Labelled unreliable rather than omitted.

    They are usually nearly right, and nearly right is exactly what a second
    reader can repair -- the `CFI4JT100K` case this whole path exists for.
    """
    capture = _Capture(_openai_body() if build is _openai else _ollama_body())
    _patch(monkeypatch, capture)

    build().read(_request(ocr_lines=("CFI4JT100K",)))

    sent = json.dumps(capture.sent)
    assert "CFI4JT100K" in sent
    assert "often wrong about single characters" in sent


@pytest.mark.parametrize("build", [_openai, _ollama])
def test_the_system_prompt_permits_refusing_to_answer(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    capture = _Capture(_openai_body() if build is _openai else _ollama_body())
    _patch(monkeypatch, capture)

    build().read(_request())

    system = capture.sent["messages"][0]["content"]
    assert "return no candidates" in system
    assert "Inventing a plausible part number" in system


# ---------------------------------------------------------------------------
# Both shapes: failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", [_openai, _ollama])
def test_a_non_json_answer_names_the_transport_it_failed_on(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    """Because "the model ignored the schema" and "this transport does not carry
    the schema to this server" are different faults with different fixes."""
    body = (
        _openai_body("Looks like a resistor!")
        if build is _openai
        else _ollama_body("Looks like a resistor!")
    )
    _patch(monkeypatch, _Capture(body))

    with pytest.raises(ModelUnavailable) as caught:
        build().read(_request())
    assert "constrained decoding" in str(caught.value)
    assert build().image_transport in str(caught.value)


@pytest.mark.parametrize("build", [_openai, _ollama])
def test_an_unreachable_server_says_how_long_it_took_to_fail(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    def boom(request: Any, timeout: float = 0) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(vision_openai_compat.urllib.request, "urlopen", boom)

    with pytest.raises(ModelUnavailable) as caught:
        build().read(_request())
    assert caught.value.elapsed_ms is not None


@pytest.mark.parametrize("build", [_openai, _ollama])
def test_an_empty_answer_survives_the_transport(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    """ "I cannot read this" must reach the caller as a result, not an exception."""
    empty = json.dumps({"candidates": [], "label_kind": "bare_part"})
    body = _openai_body(empty) if build is _openai else _ollama_body(empty)
    _patch(monkeypatch, _Capture(body))

    result = build().read(_request())

    assert result.identified is False
    assert result.stats is not None
