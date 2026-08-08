"""Watching an answer arrive, in the two framings the two servers use.

The sibling of `test_vision_openai_compat.py`, split off because streaming doubles
every wire-shape case and the two concerns read better apart. What these guard is
**not** "streaming works":

**That the constraint survives.** A streamed read is still a schema-constrained
single-shot decode. If asking for a stream dropped `format` / `response_format`, the
model could emit a datasheet URL again and every test in the sibling file would still
pass, because none of them ask for a stream.

**That the rest of the module cannot tell.** `_post_streamed` reassembles frames into
the body an unstreamed call would have returned, precisely so `_content_of`,
`_stats_of`, the truncation diagnosis and `parse_response` stay one code path. So the
assertions below are about *the same outputs* the non-streaming tests check — token
counts in two vocabularies, an empty answer, a refusal — arriving through the other
pipe.

**That the reasoning is visible.** ADR 0021's measured pathology is a run that spends
its whole budget thinking and answers nothing: 12 318 characters, once. Streaming only
`content` would render that as forty seconds of nothing, which is the exact silence
this feature exists to remove.

Everything here is offline — `urlopen` is substituted. Nothing sleeps and no GPU is
touched. What cannot be proved this way is that a real Ollama frames its stream the
way `_ollama_frames` below claims; that is `tests/integration/test_vision_live.py`'s
job and it is `live`-marked.
"""

from __future__ import annotations

import base64
import io
import itertools
import json
from typing import Any

import pytest

from app.services.enrichment import vision_openai_compat
from app.services.enrichment.openai_compat import ModelUnavailable
from app.services.enrichment.vision import VisionRequest
from app.services.enrichment.vision_openai_compat import OpenAICompatVisionProvider

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


def _request() -> VisionRequest:
    return VisionRequest(image=IMAGE, media_type="image/jpeg", document_sha256=SHA)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _StreamCapture:
    """Stands in for `urlopen`, replaying pre-framed lines."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.sent: dict[str, Any] = {}

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        self.sent = json.loads(request.data)
        return _Response("".join(f"{line}\n" for line in self.lines).encode())


class _Plain:
    """Stands in for `urlopen` on the unstreamed path."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.sent: dict[str, Any] = {}

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        self.sent = json.loads(request.data)
        return _Response(json.dumps(self.body).encode())


def _chunks(text: str, pieces: int) -> list[str]:
    if not text:
        return []
    step = max(1, len(text) // pieces)
    return [text[start : start + step] for start in range(0, len(text), step)]


def _ollama_frames(text: str, *, thinking: str = "", pieces: int = 4) -> list[str]:
    """Ollama's native framing: newline-delimited JSON, counts on the `done` object."""
    frames = []
    if thinking:
        frames.append(json.dumps({"message": {"thinking": thinking}, "done": False}))
    frames.extend(
        json.dumps({"message": {"content": piece}, "done": False})
        for piece in _chunks(text, pieces)
    )
    frames.append(
        json.dumps(
            {
                "message": {"content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 1234,
                "eval_count": 44,
            }
        )
    )
    return frames


def _openai_frames(text: str, *, thinking: str = "", pieces: int = 4) -> list[str]:
    """SSE: `data:` lines, `usage` on a frame of its own after `finish_reason`."""
    frames = []
    if thinking:
        frames.append(
            "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": thinking}}]})
        )
    frames.extend(
        "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]})
        for piece in _chunks(text, pieces)
    )
    frames.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    frames.append(
        "data: "
        + json.dumps({"choices": [], "usage": {"prompt_tokens": 1234, "completion_tokens": 44}})
    )
    frames.append("data: [DONE]")
    return frames


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


def _discard(text: str) -> None:
    """An `on_progress` that throws the partial away.

    Passing *something* is what asks for a stream, and several tests below are about the
    reassembled result rather than the partials — so the sink is named rather than an
    inline lambda, which keeps the intent readable and keeps `ARG005` honest about
    genuinely unused arguments elsewhere.
    """
    del text


def _patch(monkeypatch: pytest.MonkeyPatch, capture: Any) -> None:
    monkeypatch.setattr(vision_openai_compat.urllib.request, "urlopen", capture)


#: Both shapes, with the framing each one actually uses. Every test below runs twice,
#: because the whole reason two transports exist is that the servers disagree — and a
#: streaming path that was only ever exercised against one of them is a streaming path
#: that breaks at 2 a.m. against the other.
SHAPES = [
    pytest.param(_openai, _openai_frames, id="openai_content_parts"),
    pytest.param(_ollama, _ollama_frames, id="ollama_native"),
]


@pytest.mark.parametrize("build,frames", SHAPES)
def test_a_streamed_read_still_carries_the_schema(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """The safety property, asserted on the streaming path specifically.

    `vision.schema_for` is what makes a datasheet URL unrepresentable rather than
    discouraged. A transport that dropped it when asked to stream would turn the
    module's central guarantee into a suggestion, in the branch production uses.
    """
    capture = _StreamCapture(frames(json.dumps(ANSWER)))
    _patch(monkeypatch, capture)

    build().read(_request(), on_progress=_discard)

    sent = capture.sent
    assert sent["stream"] is True
    schema = (
        sent["format"] if "format" in sent else sent["response_format"]["json_schema"]["schema"]
    )
    candidate = schema["properties"]["candidates"]["items"]
    assert "url" not in candidate["properties"]
    assert "source_text" in candidate["required"]
    assert candidate["additionalProperties"] is False


@pytest.mark.parametrize("build,frames", SHAPES)
def test_the_reassembled_answer_parses_and_the_stats_survive(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """Frames in, the ordinary result out — including the token counts.

    The prompt count is the number ADR 0021 asks for: `grab.ts` does not downscale, so
    it is what will say whether sending a 4K frame whole is affordable. Losing it as the
    price of watching the answer arrive would be a bad trade, and on the OpenAI shape
    keeping it takes `stream_options.include_usage`.
    """
    _patch(monkeypatch, _StreamCapture(frames(json.dumps(ANSWER))))

    result = build().read(_request(), on_progress=_discard)

    assert result.candidates[0].mpn == "CF14JT100K"
    assert result.stats is not None
    assert result.stats.prompt_tokens == 1234
    assert result.stats.completion_tokens == 44
    assert result.stats.finish_reason == "stop"
    # The raw completion is the assembled string, not one frame of it.
    assert result.raw_response is not None
    assert json.loads(result.raw_response) == ANSWER


@pytest.mark.parametrize("build,frames", SHAPES)
def test_partials_grow_and_end_at_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """Every partial is a prefix of the next, and the last one is the answer.

    Monotonic growth is a contract rather than an observation: `ProgressReporter.offer`
    compares lengths to decide whether anything changed, so a producer that could shrink
    would silently stop publishing partway through the read.
    """
    seen: list[str] = []
    _patch(monkeypatch, _StreamCapture(frames(json.dumps(ANSWER))))

    build().read(_request(), on_progress=seen.append)

    assert len(seen) > 1, "one call is not a stream"
    assert all(later.startswith(earlier) for earlier, later in itertools.pairwise(seen))
    assert seen[-1] == json.dumps(ANSWER)


@pytest.mark.parametrize("build,frames", SHAPES)
def test_reasoning_is_visible_while_there_is_no_answer_yet(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """The measured pathology, made watchable.

    A run that emits 12 318 characters of reasoning and never answers is invisible if
    only `content` is published — which is the forty seconds of silence this exists to
    fill. Both spellings are read on both paths (`thinking` and `reasoning_content`),
    because reading only the expected one is how a whole reasoning stream goes
    unrecorded against a server that chose the other.
    """
    seen: list[str] = []
    _patch(
        monkeypatch,
        _StreamCapture(frames(json.dumps(ANSWER), thinking="the third digit is creased")),
    )

    build().read(_request(), on_progress=seen.append)

    assert vision_openai_compat.LIVE_REASONING_HEADER in seen[0]
    assert "creased" in seen[0]
    # Labelled rather than silently concatenated: a reader has to be able to tell
    # working-out from the schema-constrained answer.
    assert vision_openai_compat.LIVE_ANSWER_HEADER in seen[-1]
    assert seen[-1].endswith(json.dumps(ANSWER))


@pytest.mark.parametrize("build,frames", SHAPES)
def test_the_prompt_handed_out_at_the_start_holds_no_image_bytes(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """`on_started` fires before the socket opens, and what it hands over is sanitised.

    The run row has to exist while the model is still thinking, so the prompt is handed
    out earlier than it used to be — and the sanitising must not have been left behind
    at the old call site. A base64'd 4K frame in a table nothing prunes is megabytes per
    run, duplicating a blob the document store already holds.
    """
    started: list[str] = []
    _patch(monkeypatch, _StreamCapture(frames(json.dumps(ANSWER))))

    build().read(_request(), on_started=started.append, on_progress=_discard)

    assert len(started) == 1
    payload = started[0]
    assert base64.b64encode(IMAGE).decode() not in payload
    assert "image_sha256" in payload
    assert SHA in payload


@pytest.mark.parametrize("build,frames", SHAPES)
def test_an_empty_streamed_answer_is_named_as_one(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """A stream that ends with nothing falls through to the same refusal.

    Not tolerated the way a malformed *frame* is: a partial frame is skipped because the
    next line usually completes it, but a whole stream that produced no content is the
    thinking-model failure `_content_of` already explains properly — including how much
    reasoning it burned getting there, which is the number worth reading.
    """
    reasoning = "x" * 500
    _patch(monkeypatch, _StreamCapture(frames("", thinking=reasoning)))

    with pytest.raises(ModelUnavailable) as caught:
        build().read(_request(), on_progress=_discard)

    assert "empty completion" in str(caught.value)
    assert str(len(reasoning)) in str(caught.value)
    # And emphatically not the diagnosis that sends the reader to the serving stack.
    assert "constrained decoding" not in str(caught.value)


@pytest.mark.parametrize("build,frames", SHAPES)
def test_a_stream_that_dies_keeps_what_had_arrived(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """The case a transcript matters most for.

    A socket that breaks mid-read leaves no candidate row at all, so what arrived before
    it broke is the only evidence there is. Discarding it because the call failed would
    throw away the one thing worth reading.
    """

    class _Dies(_StreamCapture):
        def __call__(self, request: Any, timeout: float = 0) -> Any:
            super().__call__(request, timeout)
            raise OSError("connection reset by peer")

    _patch(monkeypatch, _Dies(frames(json.dumps(ANSWER))))

    with pytest.raises(ModelUnavailable) as caught:
        build().read(_request(), on_progress=_discard)

    assert caught.value.request_json is not None
    assert caught.value.elapsed_ms is not None


@pytest.mark.parametrize("build,frames", SHAPES)
def test_a_malformed_frame_is_skipped_rather_than_fatal(
    monkeypatch: pytest.MonkeyPatch, build: Any, frames: Any
) -> None:
    """A half-arrived frame must not cost the whole answer.

    Matching `chat_agent.OpenAICompatChatModel.stream`: the next line usually completes
    the thought, and dying here would discard a reply that is most of the way there.
    """
    lines = frames(json.dumps(ANSWER))
    lines.insert(1, "{not json at all")
    lines.insert(2, "")
    _patch(monkeypatch, _StreamCapture(lines))

    result = build().read(_request(), on_progress=_discard)

    assert result.candidates[0].mpn == "CF14JT100K"


def test_not_asking_for_progress_leaves_the_wire_unstreamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-streaming path is not a legacy path — every fixture run uses it.

    Asserted on both shapes, because "we stream now" quietly becoming unconditional is
    how a caller with nowhere to put a partial ends up waiting on a body it cannot
    parse.
    """
    plain = _Plain({"message": {"content": json.dumps(ANSWER)}, "done_reason": "stop"})
    _patch(monkeypatch, plain)
    _ollama().read(_request())
    assert plain.sent["stream"] is False

    plain = _Plain(
        {"choices": [{"message": {"content": json.dumps(ANSWER)}, "finish_reason": "stop"}]}
    )
    _patch(monkeypatch, plain)
    _openai().read(_request())
    assert "stream" not in plain.sent
    assert "stream_options" not in plain.sent
