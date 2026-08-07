"""The real extraction provider: the constraint it sends, and what it refuses.

## What this file is really guarding

**A server that quietly ignores the schema.** `response_format` is advisory in the
sense that nothing stops a backend accepting the field and sampling freely anyway,
and the deployment that does is precisely the one whose output nobody has checked.
So the provider re-validates through `parse_response` regardless, and reports a
non-JSON completion as a *deployment* fact — "does this model support constrained
decoding?" — rather than as a fact about the datasheet.

**A silent retry loop hiding a broken model.** There is deliberately no retry here.
The extraction queue already counts attempts and expires leases; a retry inside the
provider would turn a model that always fails into a slow worker rather than a
reported failure, and the queue would never see it.

**Sampling.** `temperature=0`, because extraction is a reading task with a right
answer and a nightly re-run that produces different candidate rows each time is a
source of churn rather than a check.

Everything here is offline — `urlopen` is substituted.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from app.models.enums import ValueType
from app.services.enrichment import openai_compat
from app.services.enrichment.extract import (
    ExtractionRequest,
    FakeExtractionProvider,
    TargetField,
)
from app.services.enrichment.openai_compat import (
    ModelUnavailable,
    OpenAICompatExtractionProvider,
)

MPN = "GRM188R71H104KA93D"


def _request() -> ExtractionRequest:
    return ExtractionRequest(
        document_ref="a" * 64,
        document_text=f"{MPN}  100nF  50V  X7R  0603",
        mpns=(MPN,),
        fields=(TargetField(name="capacitance", value_type=ValueType.NUMERIC, unit="F"),),
    )


def _completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


class _Capture:
    """Stands in for `urlopen`, recording the request and replaying a body."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.sent: dict[str, Any] = {}
        self.url = ""
        self.headers: dict[str, str] = {}

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        self.url = request.full_url
        self.headers = dict(request.headers)
        self.sent = json.loads(request.data)
        return _Response(json.dumps(self.body).encode())


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def provider() -> OpenAICompatExtractionProvider:
    return OpenAICompatExtractionProvider(
        base_url="http://model.test", model="qwen3-8b", name="local-vllm"
    )


def test_the_schema_is_sent_as_a_constraint_in_both_spellings(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`response_format` for the OpenAI spelling, `guided_json` for vLLM's older
    one. Servers ignore fields they do not know, so sending both costs a few bytes
    and covers both backends with no branch."""
    capture = _Capture(
        _completion(
            json.dumps(
                {
                    "variants": [
                        {
                            "mpn": MPN,
                            "mpn_source_text": MPN,
                            "fields": [
                                {
                                    "template_name": "capacitance",
                                    "raw_value": "100nF",
                                    "confidence": 0.9,
                                    "source_text": "100nF",
                                }
                            ],
                        }
                    ]
                }
            )
        )
    )
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    provider.extract(_request())

    assert capture.url == "http://model.test/v1/chat/completions"
    assert capture.sent["response_format"]["json_schema"]["strict"] is True
    assert capture.sent["guided_json"] == capture.sent["response_format"]["json_schema"]["schema"]
    # The field enum is built per request, so an invented parameter name is
    # unrepresentable rather than merely rejected.
    schema = capture.sent["guided_json"]
    field = schema["properties"]["variants"]["items"]["properties"]["fields"]["items"]
    assert field["properties"]["template_name"]["enum"] == ["capacitance"]


def test_sampling_is_off(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nightly re-run that yields different candidates each time is churn, not a
    check."""
    capture = _Capture(_completion(json.dumps({"variants": []})))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    provider.extract(_request())

    assert capture.sent["temperature"] == 0


def test_a_non_json_completion_is_reported_as_a_deployment_problem(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message names the likely cause rather than blaming the datasheet.

    A schema-constrained decode that produced prose means the server ignored
    `response_format` — which is a fact about the deployment, and the one a person
    reading the log needs.
    """
    capture = _Capture(_completion("Sure! Here are the values you asked for:"))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    with pytest.raises(ModelUnavailable) as caught:
        provider.extract(_request())
    assert "constrained decoding" in str(caught.value)


def test_a_malformed_variant_is_refused_by_the_shared_parser(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-validation that is not redundant.

    The schema said `source_text` is required; this response omits it, which can
    only happen on a server that ignored the schema. `parse_response` refuses it —
    an unreviewable assertion is exactly what the pipeline exists not to store — and
    the provider does not soften that.
    """
    capture = _Capture(
        _completion(
            json.dumps(
                {
                    "variants": [
                        {
                            "mpn": MPN,
                            "mpn_source_text": MPN,
                            "fields": [
                                {
                                    "template_name": "capacitance",
                                    "raw_value": "100nF",
                                    "confidence": 0.9,
                                }
                            ],
                        }
                    ]
                }
            )
        )
    )
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    with pytest.raises(ValueError):
        provider.extract(_request())


def test_an_unreachable_endpoint_names_the_run_not_the_document(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was read, so no document is implicated. That distinction is what
    keeps a dead GPU from burning every queued document's attempts."""

    def boom(request: Any, timeout: float = 0) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", boom)

    with pytest.raises(ModelUnavailable) as caught:
        provider.extract(_request())
    assert "connection refused" in str(caught.value)


def test_an_api_key_is_sent_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local vLLM of ADR 0016 needs no key; a frontier endpoint does. Same
    class, different configuration — `PLAN.md`'s "local first pass, frontier as
    escalation" is two instances and a rule, not two code paths."""
    capture = _Capture(_completion(json.dumps({"variants": []})))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    OpenAICompatExtractionProvider(
        base_url="http://model.test", model="m", api_key="sk-test"
    ).extract(_request())
    assert capture.headers["Authorization"] == "Bearer sk-test"

    OpenAICompatExtractionProvider(base_url="http://model.test", model="m").extract(_request())
    assert "Authorization" not in capture.headers


def test_the_prompt_puts_the_question_before_the_datasheet(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long datasheet between the instruction and the question is how the question
    gets lost."""
    capture = _Capture(_completion(json.dumps({"variants": []})))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    provider.extract(_request())

    user = capture.sent["messages"][1]["content"]
    assert user.index(MPN) < user.index("Datasheet text follows")


# ---------------------------------------------------------------------------
# What the call cost
# ---------------------------------------------------------------------------


def _completion_with(
    content: str, *, usage: object = None, finish_reason: str | None = "stop"
) -> dict[str, Any]:
    choice: dict[str, Any] = {"message": {"content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    body: dict[str, Any] = {"choices": [choice]}
    if usage is not None:
        body["usage"] = usage
    return body


def _empty() -> str:
    return json.dumps({"variants": []})


def test_the_usage_block_is_read_rather_than_discarded(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tokens and latency ride back on the result.

    Not for a benchmark's sake alone: the nightly extraction pass has no
    throughput number at all today, and `usage` was already sitting in the
    response body being thrown away.
    """
    capture = _Capture(
        _completion_with(_empty(), usage={"prompt_tokens": 4096, "completion_tokens": 311})
    )
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    stats = provider.extract(_request()).stats

    assert stats is not None
    assert stats.prompt_tokens == 4096
    assert stats.completion_tokens == 311
    assert stats.finish_reason == "stop"
    assert stats.latency_ms >= 0
    assert stats.truncated is False


def test_a_server_that_reports_no_usage_is_not_recorded_as_zero(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`usage` is optional and several local servers omit it.

    None and zero must stay distinguishable, or an average over a benchmark run
    silently pulls toward whichever models happened to be quiet.
    """
    capture = _Capture(_completion_with(_empty()))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    stats = provider.extract(_request()).stats

    assert stats is not None
    assert stats.prompt_tokens is None
    assert stats.completion_tokens is None


@pytest.mark.parametrize("bogus", [True, -1, "many", 1.5, None])
def test_an_unreadable_token_count_is_none_rather_than_a_guess(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch, bogus: object
) -> None:
    # `True` is the interesting one: bool is an int, so an unguarded read would
    # record it as one token -- and nothing sane sends it, which is exactly why
    # it would go unnoticed.
    capture = _Capture(_completion_with(_empty(), usage={"prompt_tokens": bogus}))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    stats = provider.extract(_request()).stats

    assert stats is not None and stats.prompt_tokens is None


def test_a_truncated_answer_is_diagnosed_as_truncation_not_as_a_broken_server(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this whole change exists for.

    `max_tokens` cutting off a 24-variant batch produces invalid JSON, and until
    now that was reported as "does this model support constrained decoding?" --
    which sends whoever reads it to investigate the serving stack when the actual
    fix is a smaller batch. `finish_reason` tells the two apart.
    """
    capture = _Capture(_completion_with('{"variants": [{"mpn": "GRM188', finish_reason="length"))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    with pytest.raises(ModelUnavailable) as caught:
        provider.extract(_request())

    message = str(caught.value)
    assert "token ceiling" in message
    assert "fewer part numbers" in message
    # And emphatically NOT the old diagnosis.
    assert "constrained decoding" not in message


def test_non_json_without_truncation_still_blames_the_deployment(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete answer that is not JSON is a server ignoring `response_format`."""
    capture = _Capture(_completion_with("Sure! Here are the values:", finish_reason="stop"))
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", capture)

    with pytest.raises(ModelUnavailable) as caught:
        provider.extract(_request())
    assert "constrained decoding" in str(caught.value)


def test_a_failure_says_how_long_it_took_to_fail(
    provider: OpenAICompatExtractionProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused-in-3ms and wedged-for-300s read identically in a log otherwise, and
    they are fixed by opposite actions."""

    def boom(request: Any, timeout: float = 0) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", boom)

    with pytest.raises(ModelUnavailable) as caught:
        provider.extract(_request())
    assert caught.value.elapsed_ms is not None


def test_a_replayed_fixture_reports_no_stats(tmp_path: Path) -> None:
    """A recording has no latency of its own, and inventing one would put made-up
    numbers into a benchmark."""
    fixture = tmp_path / "f.json"
    fixture.write_text(
        json.dumps({"provider": "fake", "model": "fake", "responses": {"a" * 64: {"variants": []}}})
    )
    assert FakeExtractionProvider(fixture).extract(_request()).stats is None
