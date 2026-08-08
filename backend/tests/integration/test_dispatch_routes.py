"""`/api/dispatch` over the wire: the opt-in door, the lease, and what it refuses.

`test_dispatch.py` drives the service. This drives the five routes, and it exists for
the things only the HTTP layer can get wrong:

* **`candidates: []` and `error` must stay different submissions.** Sending both, or
  neither, is refused rather than resolved by precedence — a worker that sends both has
  a bug, and picking one would record an outcome it did not report.
* **The claim has to be self-sufficient.** A worker gets one call before it starts
  working, so the barcode anchor and the OCR lines have to arrive *in* the claim. A
  claim that made the worker fetch the capture separately would be two round trips and,
  worse, a second place that decides which regions count as hints.
* **There is no route that resolves an entry.** Asserted by walking the schema rather
  than by reading it, because "somebody adds a convenience endpoint" is exactly the
  change this would otherwise not notice.
* **The person's acceptance is the *existing* resolve.** A test takes a proposal all the
  way to a resolved entry through `POST /api/intake/pending/{id}/resolve` and nothing
  else, which is what "no second acceptance mechanism" has to mean concretely.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.models.enums import DispatchState

PNG = "image/png"


def _png(body: bytes = b"label") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + body


def _upload(client: TestClient, data: bytes) -> str:
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


def _capture(client: TestClient, *, body: bytes = b"label", regions: object = None) -> int:
    default = [
        {
            "kind": "barcode",
            "text": "[)>06P1234-ND1PCF14JT100K",
            "corners": _corners(10, 10),
            "symbology": "DataMatrix",
        },
        {"kind": "text", "text": "CFI4JT100K", "corners": _corners(10, 40), "confidence": 40},
        {"kind": "text", "text": "STACKPOLE", "corners": _corners(10, 70), "confidence": 80},
    ]
    response = client.post(
        "/api/captures",
        json={
            "sha256": _upload(client, _png(body)),
            "width_px": 1600,
            "height_px": 1200,
            "regions": default if regions is None else regions,
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
    return _park(client, op_id=op_id, capture_id=_capture(client, body=op_id.encode()))


def _claim(client: TestClient, *, worker: str = "w1", limit: int = 1) -> list[dict[str, object]]:
    response = client.post("/api/dispatch/claims", json={"worker_id": worker, "limit": limit})
    assert response.status_code == 200, response.text
    claims: list[dict[str, object]] = response.json()["claims"]
    return claims


# ---------------------------------------------------------------------------
# The opt-in door
# ---------------------------------------------------------------------------


def test_status_reports_the_uncosted_photographs_apart_from_the_queue(client: TestClient) -> None:
    """`not_requested` is not queue depth, and the two are surfaced separately.

    A dashboard that folded them together would report a queue forty deep when nothing
    had been asked for, and the number a person acts on is "how many runs did I ask
    for".
    """
    _entry_with_photo(client, "a")
    _entry_with_photo(client, "b")

    body = client.get("/api/dispatch/status").json()
    assert body["not_requested"] == 2
    assert body["pending"] == 0
    assert body["lease_seconds"] == 1800
    assert body["max_attempts"] == 2
    assert _claim(client) == []


def test_requesting_then_claiming_hands_the_worker_everything_it_needs(
    client: TestClient,
) -> None:
    """One call before work starts, both readings included.

    The barcode is the **anchor** and the OCR lines are the thing most likely to be
    wrong and most useful to correct — ADR 0021's whole mechanism. If either stopped
    arriving the worker would still run, and would silently lose the repair that turns
    `CFI4JT100K` into `CF14JT100K`.
    """
    entry_id = _entry_with_photo(client)
    requested = client.post("/api/dispatch/requests", json={"intake_id": entry_id})
    assert requested.status_code == 200, requested.text
    assert requested.json()["entry"]["state"] == DispatchState.PENDING

    claims = _claim(client)
    assert len(claims) == 1
    claim = claims[0]
    assert claim["intake_id"] == entry_id
    assert claim["media_type"] == PNG
    assert claim["capture_sha256"] == hashlib.sha256(_png(b"op-1")).hexdigest()
    assert claim["barcode_texts"] == ["[)>06P1234-ND1PCF14JT100K"]
    assert claim["ocr_lines"] == ["CFI4JT100K", "STACKPOLE"]
    assert claim["attempts"] == 1
    assert claim["lease_expires_at"] is not None


def test_an_entry_with_no_photograph_is_refused_at_the_door(client: TestClient) -> None:
    """409, not a queued item a worker discovers and burns an attempt on."""
    entry_id = _park(client, op_id="bare-scan")
    response = client.post("/api/dispatch/requests", json={"intake_id": entry_id})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "no_capture"


def test_requesting_an_unknown_entry_is_a_404(client: TestClient) -> None:
    response = client.post("/api/dispatch/requests", json={"intake_id": 4242})
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "no_such_intake"


def test_cancelling_takes_it_out_and_keeps_the_candidates(client: TestClient) -> None:
    entry_id = _entry_with_photo(client)
    client.post("/api/dispatch/requests", json={"intake_id": entry_id})
    _claim(client)
    client.post(
        "/api/dispatch/results",
        json={
            "intake_id": entry_id,
            "candidates": [
                {"mpn": "CF14JT100K", "confidence": 0.7, "source_text": "MFR PN: CF14JT100K"}
            ],
        },
    )

    cancelled = client.delete(f"/api/dispatch/requests/{entry_id}")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()["entry"]
    assert body["state"] == DispatchState.NOT_REQUESTED
    assert [row["mpn"] for row in body["candidates"]] == ["CF14JT100K"]
    assert _claim(client) == []


# ---------------------------------------------------------------------------
# The submit door
# ---------------------------------------------------------------------------


def test_naming_nothing_is_unidentified_over_the_wire(client: TestClient) -> None:
    entry_id = _entry_with_photo(client)
    client.post("/api/dispatch/requests", json={"intake_id": entry_id})
    _claim(client)

    response = client.post(
        "/api/dispatch/results",
        json={"intake_id": entry_id, "candidates": [], "label_kind": "bag"},
    )
    assert response.status_code == 200, response.text
    body = response.json()["entry"]
    assert body["state"] == DispatchState.UNIDENTIFIED
    assert body["error"] is None
    assert body["label_kind"] == "bag"
    assert body["candidates"] == []


def test_sending_both_a_result_and_an_error_is_refused(client: TestClient) -> None:
    """Refused rather than resolved by precedence — a worker that sends both has a bug."""
    entry_id = _entry_with_photo(client)
    response = client.post(
        "/api/dispatch/results",
        json={"intake_id": entry_id, "candidates": [], "error": "also broken"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "ambiguous_result"


def test_sending_neither_is_refused(client: TestClient) -> None:
    entry_id = _entry_with_photo(client)
    response = client.post("/api/dispatch/results", json={"intake_id": entry_id})
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "ambiguous_result"


def test_a_candidate_with_an_empty_quote_is_refused_by_the_schema(client: TestClient) -> None:
    """`min_length=1` on `source_text`, so this never reaches the service.

    Checked at the wire as well as in the service on purpose: the schema refusal is what
    makes it impossible to write a client that omits the quote, and the service refusal
    is what covers a caller that is not a client.
    """
    entry_id = _entry_with_photo(client)
    response = client.post(
        "/api/dispatch/results",
        json={
            "intake_id": entry_id,
            "candidates": [{"mpn": "CF14JT100K", "confidence": 0.7, "source_text": ""}],
        },
    )
    assert response.status_code == 422


def test_a_percentage_confidence_is_refused_by_the_schema(client: TestClient) -> None:
    """`le=1.0`. `vision._confidence` normalises 95 to 0.95 *before* submission, so a 95
    arriving here means the worker did not use that parser."""
    entry_id = _entry_with_photo(client)
    response = client.post(
        "/api/dispatch/results",
        json={
            "intake_id": entry_id,
            "candidates": [{"mpn": "X", "confidence": 95, "source_text": "X"}],
        },
    )
    assert response.status_code == 422


def test_a_failure_leaves_it_claimable_then_walks_it_to_failed(client: TestClient) -> None:
    entry_id = _entry_with_photo(client)
    client.post("/api/dispatch/requests", json={"intake_id": entry_id})

    for _ in range(2):
        assert len(_claim(client)) == 1
        response = client.post(
            "/api/dispatch/results",
            json={"intake_id": entry_id, "error": "the model server was still loading"},
        )
        assert response.status_code == 200, response.text

    body = response.json()["entry"]
    assert body["state"] == DispatchState.FAILED
    assert body["error"] == "the model server was still loading"
    assert _claim(client) == []

    status_body = client.get("/api/dispatch/status").json()
    assert status_body["failed"] == 1
    assert status_body["unidentified"] == 0


# ---------------------------------------------------------------------------
# What the machine may never do
# ---------------------------------------------------------------------------


def test_the_proposal_shows_up_on_the_intake_entry_without_resolving_it(
    client: TestClient,
) -> None:
    """The panel's read, and the assertion ADR 0021 turns on.

    Every field a model produced is visible; the entry is still `pending`, still has no
    `resolved_part_id`, and its `mpn` is still whatever the barcode said.
    """
    entry_id = _entry_with_photo(client)
    client.post("/api/dispatch/requests", json={"intake_id": entry_id})
    _claim(client)

    part = client.post(
        "/api/parts", json={"name": "CF14JT100K", "part_kind": "component", "is_stub": True}
    )
    assert part.status_code == 201, part.text
    part_id = part.json()["part"]["id"]

    client.post(
        "/api/dispatch/results",
        json={
            "intake_id": entry_id,
            "label_kind": "bag",
            "candidates": [
                {
                    "mpn": "CF14JT100K",
                    "manufacturer": "Stackpole",
                    "confidence": 0.95,
                    "source_text": "MFR PART NO: CF14JT100K",
                    "part_id": part_id,
                    "rank": 0,
                },
                {
                    "mpn": "CFI4JT100K",
                    "confidence": 0.4,
                    "source_text": "CFI4JT100K",
                    "rank": 1,
                },
            ],
        },
    )

    listed = client.get("/api/intake/pending").json()["entries"]
    entry = next(row for row in listed if row["id"] == entry_id)

    assert entry["dispatch_state"] == DispatchState.PROPOSED
    assert entry["dispatch_label_kind"] == "bag"
    assert [row["mpn"] for row in entry["identity_candidates"]] == ["CF14JT100K", "CFI4JT100K"]
    # The identity always waits for a person, at any confidence.
    assert entry["status"] == "pending"
    assert entry["resolved_part_id"] is None
    # And the clamp held, so nothing downstream can read this as promotable.
    assert entry["identity_candidates"][0]["confidence"] < 0.8


def test_choosing_a_candidate_goes_through_the_existing_resolve(client: TestClient) -> None:
    """The person's half, and there is no other door to it.

    Deliberately uses only `POST /api/intake/pending/{id}/resolve` with the candidate's
    own `part_id`. If a second acceptance mechanism is ever added, this test keeps
    passing and `test_no_dispatch_route_resolves_an_intake_entry` is the one that fails —
    which is the right division: this one says the intended path works, that one says it
    is the only path.
    """
    entry_id = _entry_with_photo(client)
    client.post("/api/dispatch/requests", json={"intake_id": entry_id})
    _claim(client)
    part_id = client.post(
        "/api/parts", json={"name": "CF14JT100K", "part_kind": "component", "is_stub": True}
    ).json()["part"]["id"]
    client.post(
        "/api/dispatch/results",
        json={
            "intake_id": entry_id,
            "candidates": [
                {
                    "mpn": "CF14JT100K",
                    "confidence": 0.9,
                    "source_text": "MFR PART NO: CF14JT100K",
                    "part_id": part_id,
                }
            ],
        },
    )

    resolved = client.post(
        f"/api/intake/pending/{entry_id}/resolve", json={"resolved_part_id": part_id}
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["resolved_part_id"] == part_id
    # The proposal is still attached, which is what makes the decision auditable.
    assert [row["mpn"] for row in body["identity_candidates"]] == ["CF14JT100K"]


def test_no_dispatch_route_resolves_an_intake_entry(client: TestClient) -> None:
    """Walked out of the live schema, not read out of the source.

    The change this guards against is somebody adding a "accept the best candidate"
    convenience route because the two-step felt clumsy. That is a reasonable-sounding
    change and it is the one thing ADR 0021 forbids outright, so it is checked against
    what the app actually serves.
    """
    schema = client.get("/openapi.json").json()
    dispatch_paths = [path for path in schema["paths"] if path.startswith("/api/dispatch")]
    assert sorted(dispatch_paths) == [
        "/api/dispatch/claims",
        "/api/dispatch/requests",
        "/api/dispatch/requests/{intake_id}",
        "/api/dispatch/results",
        "/api/dispatch/status",
    ]

    for path in dispatch_paths:
        for method, operation in schema["paths"][path].items():
            body = operation.get("requestBody", {})
            rendered = repr(body) + repr(operation.get("responses", {}))
            assert "resolved_part_id" not in rendered, (
                f"{method.upper()} {path} mentions resolved_part_id; "
                "only a person may set that, through /api/intake/pending/{id}/resolve"
            )


#: Fields no dispatch submission may carry, and why each one is somebody else's.
#:
#: `date_code`, `lot_code` and `quantity_milli` come off the barcode deterministically
#: (`extract.ts:extractSuggestions`), and ADR 0021 keeps them out of the *vision* schema
#: for that reason — a model reading them would be a second and worse source for a solved
#: problem. Keeping them out of the wire type too is what stops the two from drifting.
#:
#: `resolved_part_id` and `status` are the person's. Note `mpn` is deliberately absent
#: from this list: `IdentitySubmission.mpn` is the *proposal*, which is the whole point.
#: What must not exist is a way to write the **entry's** `mpn`, which is checked against
#: `DispatchResultRequest` alone below.
FORBIDDEN_SUBMISSION_FIELDS = (
    "resolved_part_id",
    "status",
    "quantity_milli",
    "date_code",
    "lot_code",
)


@pytest.mark.parametrize("field", FORBIDDEN_SUBMISSION_FIELDS)
def test_a_run_cannot_write_the_entrys_own_fields(client: TestClient, field: str) -> None:
    """The submit door has no field for any of these, so a worker cannot set one."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert field not in schema["DispatchResultRequest"]["properties"]
    assert field not in schema["IdentitySubmission"]["properties"]


def test_a_run_cannot_write_the_entrys_part_number(client: TestClient) -> None:
    """The entry's `mpn` is what the checksummed symbology said, and is not submittable.

    Separate from the test above because `IdentitySubmission.mpn` *is* a legal field —
    the proposed reading — so a blanket "mpn appears nowhere" assertion would be wrong
    rather than strict.
    """
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert "mpn" not in schema["DispatchResultRequest"]["properties"]
    assert "mpn" in schema["IdentitySubmission"]["properties"]
