"""A run watched while it happens: `/api/runs/{id}/progress` and `/finish`.

Real migrations, real routes, no model and no GPU.

## What these tests are guarding

**The screen polling forever.** `runs_in_flight` is the whole polling contract:
above zero, refresh about once a second; at zero, stop. If it stopped reaching zero
— a `finish` that forgets `finished_at`, an `in_flight` that ignores it — every open
tab would keep hitting the single SQLite writer for as long as it stayed open, and
nothing would look broken. So the count is asserted before, during and after, and
the staleness backstop is asserted separately.

**A partial overwriting the real answer.** `progress` replaces `response_text`, so a
frame that arrived after the completion would replace a finished answer with an
earlier prefix of itself. Out-of-order delivery is ordinary over HTTP; the route
refuses it with 409 rather than silently doing it.

**The image creeping in through the new door.** The run row is now created *before*
the call, which moved where `request_json` comes from. The sanitising must not have
been left behind at the old call site, and no partial may carry pixels either.

**The bound applying to a partial.** The runaway-reasoning transcript is the text
most likely to reach `MAX_TRANSCRIPT_CHARS`, and it reaches it while somebody is
watching. Cutting silently would leave a reader comparing a prompt against a
response that referred to something no longer in it.

**The throttle.** Counted at the worker, because it cannot be seen from anywhere
else: an unthrottled worker produces a correct transcript and correct candidates,
and the only symptom is thousands of transactions per photograph.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.models.runs import MAX_TRANSCRIPT_CHARS
from app.scripts.dispatch_captures import QueuedCapture, process_one
from app.services.runs import STALE_AFTER_SECONDS

PNG = "image/png"
IMAGE = b"\x89PNG\r\n\x1a\n" + b"bag"
SHA = hashlib.sha256(IMAGE).hexdigest()
RIGHT = "CF14JT100K"


def _park(client: TestClient, *, op_id: str = "live-1") -> int:
    response = client.post(
        "/api/intake/pending", json={"client_op_id": op_id, "raw_payload": f"capture:{op_id}"}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["entry"]["id"])


def _start(client: TestClient, entry_id: int, **overrides: Any) -> dict[str, Any]:
    """Open a run row the way the worker does: no `finished_at`."""
    body: dict[str, Any] = {
        "kind": "vision",
        "provider": "local-ollama",
        "model": "qwen3-vl:8b",
        "intake_id": entry_id,
        "document_sha256": SHA,
        "request_json": json.dumps({"messages": [{"images": [{"image_sha256": SHA}]}]}),
    }
    body.update(overrides)
    response = client.post("/api/runs", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json()["run"])


def _activity(client: TestClient, entry_id: int) -> dict[str, Any]:
    response = client.get(f"/api/intake/pending/{entry_id}/activity")
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# In flight, and stopping
# ---------------------------------------------------------------------------


def test_a_run_with_no_finished_at_is_in_flight_and_the_screen_is_told_to_poll(
    client: TestClient,
) -> None:
    """The row exists while the model is still thinking. That is the whole point.

    Recorded afterwards — which is what this pipeline did until now — a forty-second
    read is forty seconds with no row at all, and a person watching cannot tell an
    overrunning read from a hung worker.
    """
    entry_id = _park(client)
    run = _start(client, entry_id)

    assert run["finished_at"] is None
    assert run["in_flight"] is True

    activity = _activity(client, entry_id)
    assert activity["runs_in_flight"] == 1
    assert activity["model_runs"][0]["in_flight"] is True


def test_finishing_takes_the_run_out_of_flight_and_stops_the_polling(
    client: TestClient,
) -> None:
    """The assertion that keeps a dashboard from leaking against the one writer.

    `runs_in_flight` reaching zero is what tells the client to stop refreshing. A
    `finish` that failed to set `finished_at` would leave every open tab polling
    forever and nothing would look broken.
    """
    entry_id = _park(client)
    run = _start(client, entry_id)

    response = client.post(
        f"/api/runs/{run['id']}/finish",
        json={
            "latency_ms": 39_000,
            "prompt_tokens": 1234,
            "completion_tokens": 44,
            "finish_reason": "stop",
            "response_text": json.dumps({"candidates": [], "label_kind": "bag"}),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["finished_at"] is not None
    assert response.json()["in_flight"] is False

    activity = _activity(client, entry_id)
    assert activity["runs_in_flight"] == 0
    assert activity["model_runs"][0]["in_flight"] is False
    # And the numbers went in as sent, with no zero substituted anywhere.
    assert activity["model_runs"][0]["prompt_tokens"] == 1234
    assert activity["model_runs"][0]["latency_ms"] == 39_000


def test_a_worker_that_died_mid_read_stops_being_watched(client: TestClient) -> None:
    """The backstop, and it is not optional.

    An evicted pod or an expired lease leaves a row that will never be finished. Honest
    — something *was* asked and never came back — but if that kept the screen polling,
    one killed worker would mean one tab hammering the single writer until somebody
    closed it. `STALE_AFTER_SECONDS` bounds it well inside the queue's 1800 s lease, so
    the retry that will happen is not competing with a watcher of a run that is gone.
    """
    entry_id = _park(client)
    long_ago = datetime.now(tz=UTC) - timedelta(seconds=STALE_AFTER_SECONDS + 60)
    run = _start(client, entry_id, started_at=long_ago.isoformat())

    assert run["finished_at"] is None, "still unfinished — this is not about finishing"
    assert run["in_flight"] is False
    assert _activity(client, entry_id)["runs_in_flight"] == 0


def test_a_slow_read_inside_the_bound_is_still_watched(client: TestClient) -> None:
    """The other side of the same line, so the bound cannot be set to nothing.

    ADR 0021 measured a hard read at ~39 s and the first call after a model swap waits
    on weights arriving in VRAM. A bound that retired those would turn every genuinely
    slow read into a screen that gave up watching just as the answer arrived.
    """
    entry_id = _park(client)
    recent = datetime.now(tz=UTC) - timedelta(seconds=STALE_AFTER_SECONDS // 2)
    run = _start(client, entry_id, started_at=recent.isoformat())

    assert run["in_flight"] is True


# ---------------------------------------------------------------------------
# Partials
# ---------------------------------------------------------------------------


def test_a_partial_is_readable_on_the_timeline_while_the_run_is_open(
    client: TestClient,
) -> None:
    """What makes the silence visible: the reasoning, on the screen, as it accumulates.

    ADR 0021 measured a run spending 12 318 characters on reasoning and never
    answering. None of that was visible until the call returned. This is the path that
    changes.
    """
    entry_id = _park(client)
    run = _start(client, entry_id)

    first = client.post(
        f"/api/runs/{run['id']}/progress",
        json={"response_text": "[reasoning, no answer yet]\nthe third digit is"},
    )
    assert first.status_code == 200, first.text

    activity = _activity(client, entry_id)
    assert activity["runs_in_flight"] == 1
    assert "third digit" in activity["model_runs"][0]["response_text"]

    # Replaced, not appended — the worker sends the whole accumulated text, so a
    # duplicated or reordered POST costs a frame rather than corrupting the transcript
    # into something the model never emitted.
    second = client.post(
        f"/api/runs/{run['id']}/progress",
        json={"response_text": "[reasoning, no answer yet]\nthe third digit is creased"},
    )
    assert second.status_code == 200
    assert second.json()["response_text"].endswith("creased")
    assert second.json()["response_text"].count("third digit") == 1


def test_a_partial_cannot_overwrite_a_finished_answer(client: TestClient) -> None:
    """409, rather than silently replacing the completion with a prefix of it.

    Out-of-order delivery is ordinary over HTTP. Accepting a late frame would mean the
    stored transcript is an earlier draft of the answer, which is data loss dressed as
    an update — and it would be invisible, because a prefix of valid JSON looks like a
    truncated response from a healthy model.
    """
    entry_id = _park(client)
    run = _start(client, entry_id)
    final = json.dumps({"candidates": [], "label_kind": "bag"})
    client.post(f"/api/runs/{run['id']}/finish", json={"response_text": final})

    late = client.post(f"/api/runs/{run['id']}/progress", json={"response_text": '{"candi'})

    assert late.status_code == 409, late.text
    assert late.json()["detail"]["reason"] == "run_finished"
    assert _activity(client, entry_id)["model_runs"][0]["response_text"] == final


def test_finishing_with_no_completion_keeps_what_had_arrived(client: TestClient) -> None:
    """A stream that died is the case a transcript matters most for.

    It leaves no candidate row at all, so the partial is the only evidence. Clearing it
    to NULL because the final answer is absent would throw away the one thing there is
    to read.
    """
    entry_id = _park(client)
    run = _start(client, entry_id)
    client.post(f"/api/runs/{run['id']}/progress", json={"response_text": "half an answer arrived"})

    response = client.post(
        f"/api/runs/{run['id']}/finish",
        json={"error": "OSError streaming from http://ollama: connection reset by peer"},
    )
    assert response.status_code == 200, response.text

    stored = _activity(client, entry_id)["model_runs"][0]
    assert stored["response_text"] == "half an answer arrived"
    assert "connection reset" in stored["error"]
    assert stored["in_flight"] is False


def test_the_transcript_bound_applies_to_a_partial_and_says_so(client: TestClient) -> None:
    """`MAX_TRANSCRIPT_CHARS` bounds a partial too, and `truncated` latches.

    The runaway-reasoning transcript is exactly the text that reaches this ceiling, and
    it reaches it mid-flight. Cutting silently would leave a reader comparing a prompt
    against a response that referred to something no longer in it, with nothing to say
    why.
    """
    entry_id = _park(client)
    run = _start(client, entry_id)

    response = client.post(
        f"/api/runs/{run['id']}/progress",
        json={"response_text": "x" * (MAX_TRANSCRIPT_CHARS + 500)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["truncated"] is True
    assert len(response.json()["response_text"]) == MAX_TRANSCRIPT_CHARS

    # Latched: a later partial that fits does not undo the fact that one was cut.
    client.post(f"/api/runs/{run['id']}/finish", json={"response_text": "short"})
    stored = _activity(client, entry_id)["model_runs"][0]
    assert stored["response_text"] == "short"
    assert stored["truncated"] is True


def test_no_partial_and_no_prompt_ever_carries_image_bytes(client: TestClient) -> None:
    """The rule that must survive the row being created earlier than it used to be.

    Creating the run before the call moved where `request_json` comes from — it is now
    handed out by the transport's `on_started` rather than read off the result — so the
    sanitising could have been left behind at the old call site. A base64'd 4K frame per
    run, in a table nothing prunes, duplicating a blob the document store already holds.
    """
    entry_id = _park(client)
    run = _start(client, entry_id)
    client.post(f"/api/runs/{run['id']}/progress", json={"response_text": "partial"})
    client.post(f"/api/runs/{run['id']}/finish", json={"response_text": "done"})

    stored = _activity(client, entry_id)["model_runs"][0]
    whole = json.dumps(stored)
    assert "image_sha256" in stored["request_json"]
    # The magic bytes of a PNG, base64'd, are `iVBORw`. Their absence is the assertion.
    assert "iVBORw" not in whole
    assert "data:image" not in whole


def test_progress_and_finish_refuse_a_run_that_does_not_exist(client: TestClient) -> None:
    """404 rather than a silent no-op, so a worker's bad id is visible."""
    assert client.post("/api/runs/999999/progress", json={"response_text": "x"}).status_code == 404
    assert client.post("/api/runs/999999/finish", json={}).status_code == 404


# ---------------------------------------------------------------------------
# The worker, driven end to end against the real routes
# ---------------------------------------------------------------------------


class _Client:
    """An `ApiClient` that keeps the run's lifecycle as one row, plus every partial."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.partials: list[str] = []
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
        self.rows.append(dict(run))

    def start_run(self, run: dict[str, Any]) -> int | None:
        self.rows.append(dict(run))
        return len(self.rows)

    def record_progress(self, run_id: int, response_text: str) -> None:
        self.partials.append(response_text)

    def finish_run(self, run_id: int, patch: dict[str, Any]) -> None:
        self.rows[run_id - 1].update(patch)


def _fixture(tmp_path: Any) -> Any:
    from app.services.enrichment.vision import FakeVisionProvider

    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps(
            {
                "provider": "fake-vision",
                "model": "fixture",
                "responses": {
                    SHA: {
                        "candidates": [
                            {"mpn": RIGHT, "confidence": 0.9, "source_text": "MFR PN: CF14JT100K"}
                        ],
                        "label_kind": "bag",
                    }
                },
            }
        )
    )
    return FakeVisionProvider(path)


def _queued() -> QueuedCapture:
    return QueuedCapture(
        intake_id=1,
        capture_id=7,
        capture_sha256=SHA,
        media_type=PNG,
        barcode_texts=(),
        ocr_lines=("CFI4JT100K",),
        mpn=None,
        attempts=1,
    )


def test_the_worker_opens_the_row_before_it_reads_and_settles_it_after(tmp_path: Any) -> None:
    """One row, opened then settled — not two rows and not one written at the end.

    The ordering is the feature. `start_run` must happen before `provider.read` returns,
    or there is nothing to watch; the fake provider calls `on_started` before it looks
    up its fixture for exactly that reason.
    """
    client = _Client()

    assert process_one(client, _fixture(tmp_path), _queued(), progress_interval=0.0) is True

    assert len(client.rows) == 1, "a run recorded twice is a run recorded wrong"
    row = client.rows[0]
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    assert row["error"] is None
    assert RIGHT in row["response_text"]
    # The prompt was on the row from the moment it was opened. A row with a NULL prompt
    # is not yet worth opening on the screen.
    assert json.loads(row["request_json"])["image_sha256"] == SHA


def test_the_worker_republishes_partials_and_the_throttle_bounds_them(tmp_path: Any) -> None:
    """The throttle, counted.

    `FakeVisionProvider` offers `PROGRESS_CHUNKS` growing prefixes. With no interval all
    of them are published; with an interval longer than the whole read exactly one is —
    the first, because a delayed first frame would reintroduce the silence in miniature.

    **This is the assertion that would fail if the throttle were removed**, and nothing
    else in the suite would: an unthrottled worker produces a correct transcript and
    correct candidates, and the only symptom is transactions on the one writer.
    """
    from app.services.enrichment.vision import FakeVisionProvider

    unthrottled = _Client()
    process_one(unthrottled, _fixture(tmp_path), _queued(), progress_interval=0.0)
    assert len(unthrottled.partials) == FakeVisionProvider.PROGRESS_CHUNKS
    assert all(
        later.startswith(earlier) for earlier, later in itertools.pairwise(unthrottled.partials)
    )

    throttled = _Client()
    process_one(throttled, _fixture(tmp_path), _queued(), progress_interval=3600.0)
    assert len(throttled.partials) == 1


def test_asking_for_no_stream_publishes_nothing_and_still_records_the_run(
    tmp_path: Any,
) -> None:
    """The opt-out, so a caller with nowhere to put a partial is not made to invent one.

    It is also what a `--fixture` probe and a `bench` run use, so this is the ordinary
    path rather than a legacy one.
    """
    client = _Client()

    assert process_one(client, _fixture(tmp_path), _queued(), stream=False) is True

    assert client.partials == []
    assert len(client.rows) == 1
    assert client.rows[0]["finished_at"] is not None


def test_a_read_that_broke_is_settled_rather_than_left_open(tmp_path: Any) -> None:
    """A read that broke *is* finished, and the row has to say so.

    Left unsettled, every watching screen would wait on an answer that is never coming
    until the staleness bound retired it — and that bound is a backstop, not a design.
    """
    import pytest

    client = _Client()
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

    with pytest.raises(Exception, match="no recorded response"):
        process_one(client, _fixture(tmp_path), missing, progress_interval=0.0)

    assert len(client.rows) == 1
    assert client.rows[0]["finished_at"] is not None
    assert "VisionFixtureMiss" in client.rows[0]["error"]


def test_a_worker_whose_start_call_failed_still_records_a_complete_run(
    tmp_path: Any,
) -> None:
    """The fallback, and it is the reason a bookkeeping failure costs no GPU handover.

    An API older than this worker has no `/progress` route. `start_run` returns None,
    nothing is watchable, and the single-shot `record_run` this pipeline shipped with
    produces the row instead — because pushing a photograph back into the queue over a
    missing diagnostic route would spend a **second** GPU handover on it.
    """

    class _NoStart(_Client):
        def start_run(self, run: dict[str, Any]) -> int | None:
            return None

    client = _NoStart()

    assert process_one(client, _fixture(tmp_path), _queued(), progress_interval=0.0) is True

    assert client.partials == []
    assert len(client.rows) == 1
    row = client.rows[0]
    assert row["finished_at"] is not None
    assert RIGHT in row["response_text"]
    assert json.loads(row["request_json"])["replayed"] is True
