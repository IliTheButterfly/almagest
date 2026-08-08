"""The dispatch worker: what it reads, what it mints, and what it refuses to write.

## What this file is really guarding

**A model's reading recorded as a fact.** The worker is the one process that holds both
a photograph and a database connection's worth of intent, so it is where the
never-auto-accept rule would break. The tests below pin that a run leaves the intake
entry `pending`, leaves `resolved_part_id` NULL, and leaves the barcode's `mpn`
untouched — through the **real routes**, not against a mock that could be wrong in the
same direction as the code.

**The losers silently dropped.** A stub per candidate is what lets `datasheet_validation`
eliminate a wrong reading later (ADR 0021). "Mint one for the winner" is a small,
plausible edit that would pass every other test here, so one test counts the parts.

**An unreadable photograph reported as breakage.** `candidates: []` must settle
`unidentified`, and the worker must not turn that into a failure — a blurred label in a
health check is how the health check stops being read.

**The anchor silently stopping.** A capture whose barcode decoded is held to one
candidate *by the decoder*, because `vision.schema_for` builds `maxItems` from
`max_candidates`. If `build_request` ever stopped narrowing, the fan-out would come back
and nothing would fail — the reads would just get worse. So the narrowing is asserted
directly.

Everything here is offline. The vision model is `FakeVisionProvider`, which replays a
recorded response through the **real** `parse_response`, so every refusal in that parser
is still exercised — the argument `FakeExtractionProvider` makes, unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import DispatchState, PendingIntakeStatus
from app.models.scanning import PendingIntake
from app.scripts.dispatch_captures import (
    HttpApiClient,
    QueuedCapture,
    build_request,
    process_one,
    run_once,
)
from app.services.enrichment.vision import DEFAULT_MAX_CANDIDATES, FakeVisionProvider

PNG = "image/png"
IMAGE = b"\x89PNG\r\n\x1a\n" + b"bag"
SHA = hashlib.sha256(IMAGE).hexdigest()

#: The OCR near-miss ADR 0021 was written about: tesseract read `CFI4JT100K` off a bag
#: whose part number is `CF14JT100K` — a capital I for the digit 1. Recorded, not invented.
RIGHT = "CF14JT100K"
MISREAD = "CFI4JT100K"


def _fixture(
    tmp_path: Path, candidates: list[dict[str, Any]], label_kind: str | None = "bag"
) -> Path:
    """A recorded provider response, keyed by the image's sha256 as the fake requires."""
    path = tmp_path / "vision.json"
    path.write_text(
        json.dumps(
            {
                "provider": "local-ollama",
                "model": "qwen3-vl:8b",
                "responses": {SHA: {"candidates": candidates, "label_kind": label_kind}},
            }
        ),
        encoding="utf-8",
    )
    return path


def _two_readings(tmp_path: Path) -> FakeVisionProvider:
    """The model repairing the OCR, with the misreading kept as a second candidate."""
    return FakeVisionProvider(
        _fixture(
            tmp_path,
            [
                {
                    "mpn": RIGHT,
                    "manufacturer": "Stackpole",
                    "package": "axial",
                    "confidence": 0.95,
                    "source_text": "MFR PART NO: CF14JT100K",
                },
                {
                    "mpn": MISREAD,
                    "manufacturer": None,
                    "confidence": 0.35,
                    "source_text": "CFI4JT100K",
                    "note": "the same line, read the other way",
                },
            ],
        )
    )


class _Client:
    """An `ApiClient` that records rather than calls."""

    def __init__(self, claims: list[QueuedCapture] | None = None) -> None:
        self.claims = claims or []
        self.minted: list[tuple[str, str]] = []
        self.submitted: list[tuple[int, list[dict[str, Any]], str | None]] = []
        self.failures: list[tuple[int, str]] = []
        self.fetched: list[str] = []
        self.refuse_stubs = False

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedCapture]:
        out, self.claims = self.claims[:limit], self.claims[limit:]
        return out

    def fetch_image(self, sha256: str) -> bytes:
        self.fetched.append(sha256)
        return IMAGE

    def create_stub_part(
        self, *, name: str, mpn: str, client_op_id: str, device_id: str
    ) -> int | None:
        if self.refuse_stubs:
            return None
        self.minted.append((mpn, client_op_id))
        return 100 + len(self.minted)

    def submit_candidates(self, *, intake_id: int, candidates: Any, label_kind: str | None) -> None:
        self.submitted.append((intake_id, list(candidates), label_kind))

    def submit_failure(self, *, intake_id: int, error: str) -> None:
        self.failures.append((intake_id, error))


def _queued(*, anchored: bool = False, intake_id: int = 1) -> QueuedCapture:
    return QueuedCapture(
        intake_id=intake_id,
        capture_id=7,
        capture_sha256=SHA,
        media_type=PNG,
        barcode_texts=("[)>06P1234-ND1PCF14JT100K",) if anchored else (),
        ocr_lines=(MISREAD, "STACKPOLE"),
        mpn=RIGHT if anchored else None,
        attempts=1,
    )


# ---------------------------------------------------------------------------
# The request the worker builds
# ---------------------------------------------------------------------------


def test_a_barcode_narrows_the_read_to_one_candidate() -> None:
    """The anchor, and it is enforced by the decoder rather than by the prompt.

    `vision.schema_for` builds `maxItems` from `max_candidates`, so this number *is* the
    constraint. Asserted directly because the failure mode of losing it is silent: the
    fan-out returns and the reads simply get worse.
    """
    anchored = build_request(_queued(anchored=True), IMAGE)
    assert anchored.max_candidates == 1
    assert anchored.anchored is True

    bare = build_request(_queued(), IMAGE)
    assert bare.max_candidates == DEFAULT_MAX_CANDIDATES
    assert bare.anchored is False


def test_both_of_the_browsers_readings_go_into_the_request() -> None:
    """The OCR lines are included **labelled as unreliable**, not omitted.

    They are usually nearly right, and nearly right is what a second reader repairs.
    Dropping them would remove the repair that turns `CFI4JT100K` into `CF14JT100K`.
    """
    request = build_request(_queued(anchored=True), IMAGE)
    assert request.barcode_texts == ("[)>06P1234-ND1PCF14JT100K",)
    assert request.ocr_lines == (MISREAD, "STACKPOLE")
    assert request.media_type == PNG
    assert request.document_sha256 == SHA


# ---------------------------------------------------------------------------
# One photograph
# ---------------------------------------------------------------------------


def test_every_candidate_gets_a_stub_including_the_losers(tmp_path: Path) -> None:
    """ADR 0021's mechanism: a reading with no `parts` row is one research can never test.

    Counted rather than checked for "at least one", because "mint only the winner" is the
    plausible edit and it would satisfy every other assertion in this file.
    """
    client = _Client()
    assert process_one(client, _two_readings(tmp_path), _queued()) is True

    assert [mpn for mpn, _ in client.minted] == [RIGHT, MISREAD]
    intake_id, reported, label_kind = client.submitted[0]
    assert intake_id == 1
    assert label_kind == "bag"
    assert [row["mpn"] for row in reported] == [RIGHT, MISREAD]
    assert [row["rank"] for row in reported] == [0, 1]
    assert all(row["part_id"] is not None for row in reported)
    # Provenance rides on every candidate, so a re-read under a different model leaves the
    # disagreement visible instead of overwriting it.
    assert {row["model"] for row in reported} == {"qwen3-vl:8b"}


def test_a_stub_key_is_stable_across_a_re_read(tmp_path: Path) -> None:
    """Re-reading the same photograph must not fork the catalogue.

    The key is derived from `(intake_id, mpn)`, so the second run reuses the part rather
    than minting a near-duplicate stub beside it — which is the failure that would turn
    an upgrade path (read it again with a better model) into catalogue pollution.
    """
    client = _Client()
    process_one(client, _two_readings(tmp_path), _queued())
    first = list(client.minted)
    client.minted.clear()
    process_one(client, _two_readings(tmp_path), _queued())

    assert [key for _, key in client.minted] == [key for _, key in first]
    # And a *different* entry with the same reading is a different key: two photographs
    # of two bags are two intakes, and collapsing them would attach one stub to both.
    client.minted.clear()
    process_one(client, _two_readings(tmp_path), _queued(intake_id=2))
    assert [key for _, key in client.minted] != [key for _, key in first]


def test_a_reading_survives_a_stub_that_could_not_be_minted(tmp_path: Path) -> None:
    """The quote is the useful part, so one failed mint must not discard the run.

    `create_stub_part` returning None is recorded as `part_id: null` rather than raised:
    a reviewer can act on a reading and its source text with no stub at all, and raising
    would throw away the other candidate too.
    """
    client = _Client()
    client.refuse_stubs = True
    assert process_one(client, _two_readings(tmp_path), _queued()) is True

    _, reported, _ = client.submitted[0]
    assert [row["part_id"] for row in reported] == [None, None]
    assert [row["source_text"] for row in reported] == ["MFR PART NO: CF14JT100K", MISREAD]
    assert client.failures == []


def test_reading_nothing_is_submitted_as_an_empty_list_not_a_failure(tmp_path: Path) -> None:
    """The whole point of the `UNIDENTIFIED`/`FAILED` split, from the worker's side."""
    client = _Client()
    provider = FakeVisionProvider(_fixture(tmp_path, [], label_kind="bare_part"))

    assert process_one(client, provider, _queued()) is False
    assert client.failures == []
    intake_id, reported, label_kind = client.submitted[0]
    assert (intake_id, reported, label_kind) == (1, [], "bare_part")


def test_a_broken_read_is_reported_as_a_failure(tmp_path: Path) -> None:
    """A fixture miss stands in for the model server refusing: the run broke.

    Reported rather than raised out of `run_once`, so the lease comes back and the queue
    retries — which is the difference between a model that was still loading and a
    photograph nobody can read.
    """
    client = _Client([_queued()])
    provider = FakeVisionProvider(
        _fixture(tmp_path, [{"mpn": RIGHT, "confidence": 0.9, "source_text": "x"}])
    )
    # A capture the fixture has no response for.
    client.claims = [
        QueuedCapture(
            intake_id=9,
            capture_id=7,
            capture_sha256="0" * 64,
            media_type=PNG,
            barcode_texts=(),
            ocr_lines=(),
            mpn=None,
            attempts=1,
        )
    ]

    assert run_once(client, provider, worker_id="w1") == 0
    assert client.submitted == []
    assert len(client.failures) == 1
    assert client.failures[0][0] == 9
    assert "VisionFixtureMiss" in client.failures[0][1]


def test_an_empty_claim_does_no_work(tmp_path: Path) -> None:
    """Where a healthy install spends nearly all its life, this queue being opt-in."""
    client = _Client([])
    provider = _two_readings(tmp_path)
    assert run_once(client, provider, worker_id="w1") == 0
    assert client.fetched == []


# ---------------------------------------------------------------------------
# End to end, through the real routes
# ---------------------------------------------------------------------------


def _park_with_photo(client: TestClient) -> int:
    upload = client.post(
        "/api/documents",
        content=IMAGE,
        params={"media_type": PNG},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert upload.status_code == 200, upload.text
    capture = client.post(
        "/api/captures",
        json={
            "sha256": upload.json()["document"]["sha256"],
            "width_px": 1600,
            "height_px": 1200,
            "regions": [
                {
                    "kind": "text",
                    "text": MISREAD,
                    "corners": [
                        {"x": 10, "y": 10},
                        {"x": 50, "y": 10},
                        {"x": 50, "y": 30},
                        {"x": 10, "y": 30},
                    ],
                    "confidence": 40,
                }
            ],
        },
    )
    assert capture.status_code == 201, capture.text
    parked = client.post(
        "/api/intake/pending",
        json={
            "client_op_id": "bag-1",
            "raw_payload": f"capture:{SHA}",
            "capture_id": capture.json()["id"],
        },
    )
    assert parked.status_code == 201, parked.text
    return int(parked.json()["entry"]["id"])


def test_the_worker_drains_the_queue_through_the_real_routes(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    """The unattended path, end to end, with nothing mocked but the model.

    Real migrations, real blob store holding the real bytes, real capture and intake
    routes, real claim and submit doors, real stub creation. `HttpApiClient` is pointed at
    `TestClient` by a two-line adapter rather than reimplemented, so the URLs and the JSON
    shapes it builds are the ones under test — a hand-written fake client here would test
    the test.
    """
    intake_id = _park_with_photo(client)
    assert client.post("/api/dispatch/requests", json={"intake_id": intake_id}).status_code == 200

    worker = _TestClientApi(client)
    assert run_once(worker, _two_readings(tmp_path), worker_id="w1") == 1

    entry = client.get("/api/intake/pending").json()["entries"][0]
    assert entry["dispatch_state"] == DispatchState.PROPOSED
    assert entry["dispatch_label_kind"] == "bag"
    assert [row["mpn"] for row in entry["identity_candidates"]] == [RIGHT, MISREAD]

    # The identity waits for a person, at any confidence. This is the assertion ADR 0021
    # turns on and the reason the whole chain is allowed to run unattended.
    assert entry["status"] == PendingIntakeStatus.PENDING
    assert entry["resolved_part_id"] is None
    assert entry["mpn"] is None
    assert entry["identity_candidates"][0]["confidence"] < 0.8

    # Two stubs exist, both `is_stub`, and both are now the *research* queue's problem —
    # which is the handover that makes this worker's job end here.
    stubs = list(db.execute(select(Part).where(Part.is_stub.is_(True))).scalars())
    assert sorted(part.mpn or "" for part in stubs) == sorted([MISREAD, RIGHT])
    researchable = client.post("/api/research/claims", json={"worker_id": "r1", "limit": 5}).json()
    assert {claim["mpn"] for claim in researchable["claims"]} >= {RIGHT, MISREAD}

    # And the queue is drained: nothing else asked for a model.
    assert (
        client.post("/api/dispatch/claims", json={"worker_id": "w1", "limit": 5}).json()["claims"]
        == []
    )


def test_a_person_choosing_a_candidate_is_the_only_way_it_resolves(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    """The last step, and it goes through the door that already existed."""
    intake_id = _park_with_photo(client)
    client.post("/api/dispatch/requests", json={"intake_id": intake_id})
    run_once(_TestClientApi(client), _two_readings(tmp_path), worker_id="w1")

    entry = client.get("/api/intake/pending").json()["entries"][0]
    chosen = entry["identity_candidates"][0]

    resolved = client.post(
        f"/api/intake/pending/{intake_id}/resolve",
        json={"resolved_part_id": chosen["part_id"]},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == PendingIntakeStatus.RESOLVED
    assert resolved.json()["resolved_part_id"] == chosen["part_id"]

    db.expire_all()
    stored = db.get(PendingIntake, intake_id)
    assert stored is not None
    # The proposal is still attached, which is what makes the decision auditable later.
    assert stored.dispatch_state == DispatchState.PROPOSED


class _TestClientApi(HttpApiClient):
    """`HttpApiClient` with `urllib` swapped for `TestClient`.

    A subclass rather than a second implementation on purpose: the URL paths, the JSON
    bodies and the response parsing are exactly the ones the real worker uses, so this
    test would catch a wrong path or a renamed field. A hand-rolled client satisfying the
    Protocol would test the test.
    """

    def __init__(self, client: TestClient) -> None:
        super().__init__("http://testserver")
        self._client = client

    def fetch_image(self, sha256: str) -> bytes:
        response = self._client.get(f"/api/documents/{sha256}")
        assert response.status_code == 200, response.text
        return response.content

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"/{path}", json=payload)
        if response.status_code >= 400:
            raise AssertionError(f"POST {path} -> {response.status_code}: {response.text}")
        decoded: Any = response.json() if response.content else {}
        return decoded if isinstance(decoded, dict) else {}


@pytest.mark.parametrize("forbidden", ["resolved_part_id", "status", "quantity_milli"])
def test_the_worker_has_no_call_that_could_write_a_persons_decision(forbidden: str) -> None:
    """Read off the module's source, because absence is the guarantee.

    Every other test here checks what the worker *does*. This checks that the code has no
    mention of the fields it must never touch — which is what stops a future edit from
    adding a convenient extra POST and passing everything above.

    Uses `test_route_fence._code_only` rather than a fresh line filter. The module
    docstring discusses `resolved_part_id` at length and must be able to, so the check has
    to read code and not text — and there is already exactly one place in this suite that
    knows how to tell those apart, with its own test proving it works.
    """
    import app.scripts.dispatch_captures as worker_module
    from tests.integration.test_route_fence import _code_only

    source = Path(worker_module.__file__ or "").read_text(encoding="utf-8")
    assert forbidden not in _code_only(source)
