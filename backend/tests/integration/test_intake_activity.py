"""The transcript and the timeline: `/api/runs` and one entry's `/activity`.

## What these tests are guarding

**A transcript that stops being recorded.** `process_one` records a run either side
of the model call, and both branches are one `try`/`except` away from being lost —
the failure branch especially, since nothing downstream of it would notice. So the
worker is driven with a provider that answers and one that raises, and the recorded
submissions are counted in both cases.

**The image creeping back into the payload.** `request_json` must carry
`{"image_sha256": ...}` and never base64. A copy of a 4K frame per run, in a table
nothing prunes, is a slow and quiet way to fill a volume, and the sanitiser is one
line from being deleted as redundant. Both wire shapes are checked.

**A missing count rendered as zero.** `CallStats` keeps `None` distinguishable from
`0` all the way from the server's response to the column, and a `or 0` anywhere on
that path would be invisible — a benchmark average would simply drift. So a run
submitted with no counts is read back with nulls.

**The timeline quietly acquiring a write.** `GET .../activity` reads seven tables.
A convenience write there ("while we are here, promote the best candidate") would
break ADR 0021's central rule, so a test walks a whole entry through the route and
asserts `mpn`, `resolved_part_id` and `status` are exactly what they were.

**A truncated transcript that does not say so.** Cutting silently would leave a
reader comparing a prompt against a response that referred to something no longer
in it. The bound is exercised with a transcript past it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models.runs import MAX_TRANSCRIPT_CHARS
from app.scripts.dispatch_captures import QueuedCapture, process_one
from app.services.enrichment.vision import FakeVisionProvider, VisionRequest
from app.services.enrichment.vision_openai_compat import OpenAICompatVisionProvider

PNG = "image/png"
IMAGE = b"\x89PNG\r\n\x1a\n" + b"bag"
SHA = hashlib.sha256(IMAGE).hexdigest()

#: The recorded OCR near-miss ADR 0021 was written about: tesseract read
#: `CFI4JT100K` off a bag whose part number is `CF14JT100K`.
RIGHT = "CF14JT100K"
MISREAD = "CFI4JT100K"


# ---------------------------------------------------------------------------
# Fixtures over the real routes
# ---------------------------------------------------------------------------


def _upload(client: TestClient, data: bytes = IMAGE) -> str:
    response = client.post(
        "/api/documents",
        content=data,
        params={"media_type": PNG},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["document"]["sha256"])


def _corners(x: int, y: int) -> list[dict[str, int]]:
    return [
        {"x": x, "y": y},
        {"x": x + 40, "y": y},
        {"x": x + 40, "y": y + 20},
        {"x": x, "y": y + 20},
    ]


def _capture(client: TestClient) -> int:
    response = client.post(
        "/api/captures",
        json={
            "sha256": _upload(client),
            "width_px": 1600,
            "height_px": 1200,
            "text_status": "ok",
            "regions": [
                {
                    "kind": "barcode",
                    "text": "[)>06P1234-ND1PCF14JT100K",
                    "corners": _corners(10, 10),
                    "symbology": "DataMatrix",
                },
                {
                    "kind": "text",
                    "text": MISREAD,
                    "corners": _corners(10, 40),
                    "confidence": 40,
                },
            ],
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _park(client: TestClient, *, op_id: str = "op-1", capture_id: int | None = None) -> int:
    body: dict[str, object] = {"client_op_id": op_id, "raw_payload": f"capture:{op_id}"}
    if capture_id is not None:
        body["capture_id"] = capture_id
    response = client.post("/api/intake/pending", json=body)
    assert response.status_code == 201, response.text
    return int(response.json()["entry"]["id"])


def _entry_with_photo(client: TestClient, op_id: str = "op-1") -> int:
    return _park(client, op_id=op_id, capture_id=_capture(client))


def _run_body(intake_id: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "vision",
        "provider": "local-ollama",
        "model": "qwen3-vl:8b",
        "intake_id": intake_id,
        "document_sha256": SHA,
        "latency_ms": 53_200,
        "prompt_tokens": 3084,
        "completion_tokens": 41,
        "finish_reason": "stop",
        "request_json": json.dumps({"messages": [{"images": [{"image_sha256": SHA}]}]}),
        "response_text": '{"candidates": [{"mpn": "CF14JT100K"}]}',
    }
    body.update(overrides)
    return body


def _post_run(client: TestClient, intake_id: int, **overrides: Any) -> dict[str, Any]:
    response = client.post("/api/runs", json=_run_body(intake_id, **overrides))
    assert response.status_code == 201, response.text
    run: dict[str, Any] = response.json()["run"]
    return run


def _activity(client: TestClient, entry_id: int) -> dict[str, Any]:
    response = client.get(f"/api/intake/pending/{entry_id}/activity")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# Recording a run
# ---------------------------------------------------------------------------


def test_a_run_keeps_the_prompt_and_the_raw_answer(client: TestClient) -> None:
    """The whole point: what the model was told, and what it said before parsing."""
    entry_id = _entry_with_photo(client)
    run = _post_run(client, entry_id)

    assert run["provider"] == "local-ollama"
    assert run["model"] == "qwen3-vl:8b"
    assert run["prompt_tokens"] == 3084
    assert run["finish_reason"] == "stop"
    assert run["truncated"] is False
    assert '"image_sha256"' in run["request_json"]
    assert run["response_text"] == '{"candidates": [{"mpn": "CF14JT100K"}]}'
    assert run["error"] is None


def test_a_missing_count_stays_null_and_never_becomes_zero(client: TestClient) -> None:
    """`CallStats`' rule, at the column.

    A server that omits `usage` is ordinary. A zero here would read as "the prompt was
    empty" and would pull any average over these rows toward whichever servers were
    quiet — and it would do so invisibly, which is why this is asserted rather than
    trusted to the absence of a default.
    """
    entry_id = _entry_with_photo(client)
    run = _post_run(
        client,
        entry_id,
        latency_ms=None,
        prompt_tokens=None,
        completion_tokens=None,
        finish_reason=None,
    )

    assert run["prompt_tokens"] is None
    assert run["completion_tokens"] is None
    assert run["latency_ms"] is None
    assert run["finish_reason"] is None


def test_a_failed_run_is_recorded_with_its_error(client: TestClient) -> None:
    """The case a transcript matters most for: it leaves no candidate row behind."""
    entry_id = _entry_with_photo(client)
    run = _post_run(
        client,
        entry_id,
        response_text=None,
        error="ModelUnavailable: local-ollama returned an empty completion.",
    )

    assert run["error"] is not None
    assert "empty completion" in run["error"]
    assert run["response_text"] is None
    # The prompt survived even though the answer did not — which is exactly the
    # asymmetry that makes a failed run diagnosable.
    assert run["request_json"] is not None


def test_two_runs_both_survive_because_a_retry_is_not_a_correction(
    client: TestClient,
) -> None:
    """Deliberately not idempotent, unlike `dispatch.record_result`.

    A second reading of one photograph *replaces* the first — that is a corrected
    opinion. Two calls having happened is a different kind of fact, and collapsing them
    would erase the retry history that makes `dispatch_attempts` legible: "it failed then
    worked" and "it worked first time" would look identical.
    """
    entry_id = _entry_with_photo(client)
    _post_run(client, entry_id, error="ModelUnavailable: connection refused", response_text=None)
    _post_run(client, entry_id)

    runs = _activity(client, entry_id)["model_runs"]
    assert len(runs) == 2
    assert runs[0]["error"] is not None
    assert runs[1]["error"] is None


def test_a_long_transcript_is_cut_and_says_so(client: TestClient) -> None:
    """Truncation is visible, because a silent cut is unreadable evidence.

    ADR 0021 measured a run emitting 12 318 characters of reasoning and no answer, so
    the bound is loose on purpose — but when it does bite, a reader comparing a prompt
    against a response must not be doing so against a response that was quietly
    shortened.
    """
    entry_id = _entry_with_photo(client)
    run = _post_run(client, entry_id, response_text="z" * (MAX_TRANSCRIPT_CHARS + 500))

    assert run["truncated"] is True
    assert len(run["response_text"]) == MAX_TRANSCRIPT_CHARS


def test_a_run_against_a_photograph_that_does_not_exist_is_refused(
    client: TestClient,
) -> None:
    """A dangling `intake_id` is a worker bug, not a null.

    Storing it as NULL would produce a row nothing can ever show — it would not appear
    on any entry's timeline and would not be findable at all.
    """
    response = client.post("/api/runs", json=_run_body(9999))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "unknown_intake"


# ---------------------------------------------------------------------------
# The transport's sanitised payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["ollama_native", "openai_content_parts"])
def test_the_sanitised_payload_carries_the_hash_and_never_the_bytes(transport: str) -> None:
    """No image bytes in a transcript, on either wire shape.

    Checked per transport because the image lives in a different place in each —
    `images: [...]` on the message for Ollama, an `image_url` content part for the
    OpenAI spelling — so a sanitiser written for one silently passes the other through.
    That is precisely the divergence `vision_openai_compat`'s docstring says fails at
    2 a.m. rather than in review.
    """
    provider = OpenAICompatVisionProvider(
        base_url="http://localhost:11434",
        model="qwen3-vl:8b",
        image_transport=transport,  # type: ignore[arg-type]
    )
    request = VisionRequest(image=IMAGE, media_type=PNG, document_sha256=SHA, ocr_lines=(MISREAD,))
    schema: dict[str, Any] = {"type": "object"}
    payload = (
        provider._ollama_payload(request, schema)
        if transport == "ollama_native"
        else provider._openai_payload(request, schema)
    )

    sent = provider._sanitised(payload, request)
    assert SHA in sent
    assert '"image_sha256"' in sent
    # The bytes themselves, in the encoding the real payload uses. Asserted as the
    # actual base64 rather than as "no long strings", because a length heuristic would
    # also redact the prompt — and the prompt is what a reviewer is here to read.
    import base64

    assert base64.b64encode(IMAGE).decode("ascii") not in sent
    # ...and the prompt *did* survive, including the OCR hint that is the whole reason
    # a wrong reading is explicable.
    assert MISREAD in sent


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


def _fixture(tmp_path: Any, candidates: list[dict[str, Any]]) -> FakeVisionProvider:
    path = tmp_path / "vision.json"
    path.write_text(
        json.dumps(
            {
                "provider": "local-ollama",
                "model": "qwen3-vl:8b",
                "responses": {SHA: {"candidates": candidates, "label_kind": "bag"}},
            }
        ),
        encoding="utf-8",
    )
    return FakeVisionProvider(path)


class _Recorder:
    """An `ApiClient` that keeps what the worker handed it."""

    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.submitted: list[dict[str, Any]] = []

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedCapture]:
        return []

    def fetch_image(self, sha256: str) -> bytes:
        return IMAGE

    def create_stub_part(
        self, *, name: str, mpn: str, client_op_id: str, device_id: str
    ) -> int | None:
        return 1

    def submit_candidates(self, *, intake_id: int, candidates: Any, label_kind: str | None) -> None:
        self.submitted.append({"intake_id": intake_id, "candidates": list(candidates)})

    def submit_failure(self, *, intake_id: int, error: str) -> None:
        pass

    def record_run(self, run: dict[str, Any]) -> None:
        self.recorded.append(run)


def _queued() -> QueuedCapture:
    return QueuedCapture(
        intake_id=1,
        capture_id=7,
        capture_sha256=SHA,
        media_type=PNG,
        barcode_texts=(),
        ocr_lines=(MISREAD,),
        mpn=None,
        attempts=1,
    )


def test_the_worker_records_a_run_for_a_read_that_worked(tmp_path: Any) -> None:
    """One row per call, with the fixture's own answer as the raw response."""
    provider = _fixture(
        tmp_path,
        [{"mpn": RIGHT, "confidence": 0.95, "source_text": "MFR PART NO: CF14JT100K"}],
    )
    client = _Recorder()

    assert process_one(client, provider, _queued()) is True

    assert len(client.recorded) == 1
    run = client.recorded[0]
    assert run["kind"] == "vision"
    assert run["intake_id"] == 1
    assert run["document_sha256"] == SHA
    assert run["error"] is None
    assert RIGHT in run["response_text"]
    # The fake has no wire payload, so what it reports is what it was *handed* — and it
    # is flagged as a replay so nobody reads it as provenance.
    assert json.loads(run["request_json"])["replayed"] is True
    assert MISREAD in run["request_json"]


def test_the_worker_records_a_run_for_a_read_that_broke(tmp_path: Any) -> None:
    """The branch nothing downstream would notice the loss of.

    A broken read submits a queue failure and produces no candidate row, so without a
    run recorded here the call leaves nothing behind but a one-line message. The
    exception still propagates — `run_once` owns reporting it to the queue — which is
    why this asserts the raise as well as the row.
    """
    provider = _fixture(tmp_path, [{"mpn": RIGHT, "confidence": 0.9, "source_text": "x"}])
    client = _Recorder()
    # A capture the fixture has no recorded answer for: `FakeVisionProvider` raises
    # `VisionFixtureMiss`, which is the offline stand-in for a model server refusing.
    missing = QueuedCapture(
        intake_id=1,
        capture_id=7,
        capture_sha256="0" * 64,
        media_type=PNG,
        barcode_texts=(),
        ocr_lines=(),
        mpn=None,
        attempts=1,
    )

    with pytest.raises(Exception, match=r"VisionFixtureMiss|no recorded response"):
        process_one(client, provider, missing)

    assert len(client.recorded) == 1
    assert client.submitted == []
    run = client.recorded[0]
    assert run["error"] is not None
    assert "VisionFixtureMiss" in run["error"]


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------


def test_the_timeline_reports_every_section_even_when_it_is_empty(
    client: TestClient,
) -> None:
    """ "No worker has run" has to be sayable, so no section may be absent.

    An empty list and an absent key look the same to a careless client and mean
    different things to a careful one: nothing has been asked versus something answered
    nothing. The screen's honest copy depends on the difference.
    """
    entry_id = _entry_with_photo(client)
    body = _activity(client, entry_id)

    assert body["model_runs"] == []
    assert body["identity_candidates"] == []
    assert body["resolved_part"] is None
    assert body["dispatch"]["state"] == "not_requested"
    assert body["dispatch"]["error"] is None
    assert body["dispatch"]["max_attempts"] == 2
    # The capture is the one section that is legitimately null — a bare barcode scan has
    # no photograph — so this entry, which has one, must not be.
    assert body["capture"] is not None
    assert [region["text"] for region in body["capture"]["regions"]] == [
        "[)>06P1234-ND1PCF14JT100K",
        MISREAD,
    ]


def test_the_timeline_shows_the_stored_clamped_confidence(client: TestClient) -> None:
    """The number on screen is the stored one, not the model's self-report.

    `record_result` clamps a vision reading strictly below
    `candidates.AUTO_PROMOTE_CONFIDENCE`, because reading characters off a photograph
    and trusting a datasheet's statement of a value are different quantities that share
    a range. A view that handed back the raw 0.95 would undo that at the last step —
    and ADR 0021 measured 0.95 on an answer that was the item's FCC ID.
    """
    entry_id = _entry_with_photo(client)
    assert client.post("/api/dispatch/requests", json={"intake_id": entry_id}).status_code == 200
    assert client.post("/api/dispatch/claims", json={"worker_id": "w1"}).status_code == 200
    submitted = client.post(
        "/api/dispatch/results",
        json={
            "intake_id": entry_id,
            "label_kind": "bag",
            "candidates": [
                {"mpn": RIGHT, "confidence": 0.95, "source_text": "MFR PART NO: CF14JT100K"}
            ],
        },
    )
    assert submitted.status_code == 200, submitted.text

    body = _activity(client, entry_id)
    assert body["dispatch"]["state"] == "proposed"
    stored = body["identity_candidates"][0]["confidence"]
    assert stored < 0.8
    assert stored == pytest.approx(0.79)


def test_reading_the_timeline_changes_nothing_a_person_decided(client: TestClient) -> None:
    """The three columns ADR 0021 forbids anything automated from touching.

    A diagnostic view is exactly where a convenience write gets added, so the whole
    entry is snapshotted, the route is called, and the snapshot is compared. Asserted
    over the *entry read* rather than over the activity payload, because the activity
    payload is what a wrong implementation would also be reporting.
    """
    entry_id = _entry_with_photo(client)
    _post_run(client, entry_id)

    def entry() -> dict[str, Any]:
        listing = client.get("/api/intake/pending", params={"status": "pending"})
        assert listing.status_code == 200, listing.text
        rows: list[dict[str, Any]] = listing.json()["entries"]
        return next(row for row in rows if row["id"] == entry_id)

    before = entry()
    _activity(client, entry_id)
    after = entry()

    assert before == after
    assert after["mpn"] is None
    assert after["resolved_part_id"] is None
    assert after["status"] == "pending"


def test_the_timeline_follows_the_part_a_person_accepted(client: TestClient) -> None:
    """The chain past acceptance: research standing, documents, field candidates.

    Reached through `resolved_part_id` — a **person's** decision — and not through a
    candidate's stub `part_id`, which is a machine's proposal. Following the latter
    would present three unaccepted stubs' research as though it were this entry's
    outcome.
    """
    entry_id = _entry_with_photo(client)
    created = client.post(
        "/api/parts",
        json={"name": RIGHT, "part_kind": "component", "mpn": RIGHT, "is_stub": True},
    )
    assert created.status_code == 201, created.text
    part_id = int(created.json()["part"]["id"])

    resolved = client.post(
        f"/api/intake/pending/{entry_id}/resolve", json={"resolved_part_id": part_id}
    )
    assert resolved.status_code == 200, resolved.text

    body = _activity(client, entry_id)
    part = body["resolved_part"]
    assert part is not None
    assert part["id"] == part_id
    assert part["is_stub"] is True
    # `pending` is the default and is normal, not broken: no research worker has run.
    assert part["research_state"] == "pending"
    assert part["research_candidates"] == []
    assert part["documents"] == []
    assert part["field_candidates"] == []


def test_the_timeline_refuses_an_entry_that_does_not_exist(client: TestClient) -> None:
    response = client.get("/api/intake/pending/9999/activity")
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["reason"] == "not_found"
