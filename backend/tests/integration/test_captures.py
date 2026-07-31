"""Captures: the still, its outlines, and the instalments they arrive in.

Runs against real Alembic migrations like every integration test here, which is
what makes the two things worth checking actually checked: that `captures` and
`capture_regions` carry no `CHECK` constraint (the schema-wide rule a `sa.Enum`
would silently break), and that `pending_intakes.capture_id` really is nullable
with `SET NULL`, since the whole "curate at the desk" story rests on a photo
outliving nothing and a deleted photo not taking the worklist entry with it.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

PNG = "image/png"


def _png(body: bytes = b"label") -> bytes:
    """Begins with the PNG magic `blobstore` checks, unique per `body`."""
    return b"\x89PNG\r\n\x1a\n" + body


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload(client: TestClient, data: bytes = _png()) -> str:
    response = client.post(
        "/api/documents",
        content=data,
        params={"media_type": PNG},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    sha256: str = response.json()["document"]["sha256"]
    return sha256


def _corners(x: int, y: int, w: int = 40, h: int = 20) -> list[dict[str, int]]:
    """Top-left, top-right, bottom-right, bottom-left — `zxing-wasm`'s order."""
    return [
        {"x": x, "y": y},
        {"x": x + w, "y": y},
        {"x": x + w, "y": y + h},
        {"x": x, "y": y + h},
    ]


def _create(client: TestClient, **overrides: object) -> Response:
    body: dict[str, object] = {
        "sha256": _upload(client),
        "width_px": 1600,
        "height_px": 1200,
        "regions": [
            {
                "kind": "barcode",
                "text": "RC0805FR-0710KL",
                "corners": _corners(10, 10),
                "symbology": "DataMatrix",
            }
        ],
    }
    body.update(overrides)
    return client.post("/api/captures", json=body)


# ---------------------------------------------------------------------------
# The shape of the thing
# ---------------------------------------------------------------------------


def test_capture_records_the_image_and_what_was_read(client: TestClient) -> None:
    response = _create(client)
    assert response.status_code == 201, response.text
    capture = response.json()

    assert capture["width_px"] == 1600
    # Never looked, which is not the same as "found nothing" — the distinction
    # `CaptureTextStatus` exists to keep.
    assert capture["text_status"] == "not_attempted"
    # The whole document row, so the overlay can draw the image without a second
    # call.
    assert capture["document"]["url"].endswith(capture["document"]["sha256"])

    (region,) = capture["regions"]
    assert region["kind"] == "barcode"
    assert region["text"] == "RC0805FR-0710KL"
    assert region["symbology"] == "DataMatrix"
    assert region["corners"][1] == {"x": 50, "y": 10}
    assert region["order_index"] == 0


def test_capture_needs_an_uploaded_image_first(client: TestClient) -> None:
    """A capture points at a stored blob; it does not carry one."""
    response = _create(client, sha256="0" * 64)
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "document_not_found"


def test_a_capture_with_no_regions_is_legal(client: TestClient) -> None:
    """The image is the asset. A photo of a label nothing could be read off is
    exactly the one worth keeping — the same posture `scan_events` takes."""
    response = _create(client, regions=[])
    assert response.status_code == 201
    assert response.json()["regions"] == []


def test_a_region_needs_four_corners(client: TestClient) -> None:
    response = _create(
        client,
        regions=[{"kind": "text", "text": "10uF", "corners": _corners(0, 0)[:3]}],
    )
    assert response.status_code == 422


def test_confidence_is_dropped_for_a_barcode(client: TestClient) -> None:
    """A symbology checksummed or it did not; there is no 87%-true DataMatrix.

    Storing a fabricated score would invite a UI that ranks a guessed word
    alongside a verified payload, which is the one comparison that must never
    look reasonable.
    """
    response = _create(
        client,
        regions=[
            {
                "kind": "barcode",
                "text": "4K7T92M8",
                "corners": _corners(0, 0),
                "confidence": 99,
            },
            {
                "kind": "text",
                "text": "MURATA",
                "corners": _corners(0, 40),
                "confidence": 71,
            },
        ],
    )
    assert response.status_code == 201
    barcode, ocr = response.json()["regions"]
    assert barcode["confidence"] is None
    assert ocr["confidence"] == 71


# ---------------------------------------------------------------------------
# Instalments — the OCR pass finishing later
# ---------------------------------------------------------------------------


def test_text_regions_append_after_the_barcodes(client: TestClient) -> None:
    """Barcodes decode in milliseconds; the OCR model is megabytes and may never
    load. So the second instalment must not disturb the first."""
    capture_id = _create(client).json()["id"]

    appended = client.post(
        f"/api/captures/{capture_id}/regions",
        json={
            "text_status": "ok",
            "regions": [
                {
                    "kind": "text",
                    "text": "Murata Electronics",
                    "corners": _corners(10, 60),
                    "confidence": 88,
                }
            ],
        },
    )
    assert appended.status_code == 200, appended.text
    body = appended.json()
    assert body["text_status"] == "ok"

    barcode, ocr = body["regions"]
    # The barcode keeps both its identity and its place at the top of the list.
    assert barcode["order_index"] == 0
    assert barcode["text"] == "RC0805FR-0710KL"
    assert ocr["order_index"] == 1


def test_appending_barcodes_alone_leaves_text_status_untouched(client: TestClient) -> None:
    """A re-decode at higher effort must not claim anything about whether text
    was read — which is why `text_status` is optional on the append."""
    capture_id = _create(client).json()["id"]
    response = client.post(
        f"/api/captures/{capture_id}/regions",
        json={"regions": [{"kind": "barcode", "text": "X", "corners": _corners(0, 90)}]},
    )
    assert response.status_code == 200
    assert response.json()["text_status"] == "not_attempted"


def test_unavailable_is_a_recordable_answer(client: TestClient) -> None:
    """A browser that could not load the OCR model at all is a fact about the
    reader, not about the image — and must not look like a blank label."""
    response = _create(client, text_status="unavailable")
    assert response.status_code == 201
    assert response.json()["text_status"] == "unavailable"


# ---------------------------------------------------------------------------
# Reading back, and throwing away
# ---------------------------------------------------------------------------


def test_read_and_list(client: TestClient) -> None:
    first = _create(client, sha256=_upload(client, _png(b"one"))).json()["id"]
    second = _create(client, sha256=_upload(client, _png(b"two"))).json()["id"]

    assert client.get(f"/api/captures/{first}").json()["id"] == first

    listing = client.get("/api/captures").json()
    assert listing["total"] == 2
    # Newest first: the desk pass wants the reel it just scanned.
    assert [item["id"] for item in listing["items"]] == [second, first]


def test_unknown_capture_is_a_404(client: TestClient) -> None:
    assert client.get("/api/captures/9999").status_code == 404


def test_delete_takes_the_regions_and_leaves_the_blob(client: TestClient) -> None:
    """A notebook, not a ledger: a blurry photo is deleted outright rather than
    compensated for. The blob is content-addressed and may be another capture's
    image, so reclaiming it is the scrub job's business."""
    body = _create(client).json()
    sha256 = body["document"]["sha256"]

    assert client.delete(f"/api/captures/{body['id']}").status_code == 204
    assert client.get(f"/api/captures/{body['id']}").status_code == 404
    assert client.get(f"/api/documents/{sha256}").status_code == 200


# ---------------------------------------------------------------------------
# The link that makes deferring honest
# ---------------------------------------------------------------------------


def test_a_parked_scan_can_carry_its_photograph(client: TestClient) -> None:
    capture_id = _create(client).json()["id"]
    response = client.post(
        "/api/intake/pending",
        json={
            "client_op_id": "op-with-a-photo",
            "raw_payload": "RC0805FR-0710KL",
            "capture_id": capture_id,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["entry"]["capture_id"] == capture_id


def test_parking_against_an_unknown_capture_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/intake/pending",
        json={"client_op_id": "op-bad-capture", "raw_payload": "X", "capture_id": 4242},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_capture"


def test_deleting_a_capture_keeps_the_worklist_entry(client: TestClient) -> None:
    """`SET NULL`, not cascade. Deleting a blurry photo must not silently delete
    the parked scan it was attached to — the payload is the thing intake cannot
    afford to lose."""
    capture_id = _create(client).json()["id"]
    client.post(
        "/api/intake/pending",
        json={
            "client_op_id": "op-survives",
            "raw_payload": "RC0805FR-0710KL",
            "capture_id": capture_id,
        },
    )
    assert client.delete(f"/api/captures/{capture_id}").status_code == 204

    entries = client.get("/api/intake/pending").json()["entries"]
    (entry,) = [item for item in entries if item["client_op_id"] == "op-survives"]
    assert entry["capture_id"] is None
    assert entry["raw_payload"] == "RC0805FR-0710KL"


# ---------------------------------------------------------------------------
# Schema rules the whole project depends on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["captures", "capture_regions"])
def test_no_check_constraints(db: Session, table: str) -> None:
    """`sa.Enum` is the trap: it silently emits `VARCHAR + CHECK`, and SQLite
    cannot alter one — so a new `CaptureTextStatus` member would mean a full
    table rebuild instead of a one-line addition. See `CLAUDE.md`."""
    sql = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).scalar_one()
    assert "CHECK" not in sql.upper()
