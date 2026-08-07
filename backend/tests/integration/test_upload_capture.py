"""Getting a photograph off this machine and into Almagest.

Two halves, and the fiddly one is not the HTTP.

**Reading dimensions from a file header.** The API refuses to derive
`width_px`/`height_px` itself (ADR 0005), so this script must, and it does it
without an imaging library. A JPEG is a chain of marker segments and only one
of them carries the picture's size; three markers sit inside the Start-Of-Frame
numeric range and are not frame headers at all. Getting that wrong yields
*plausible* numbers -- a Huffman table read as a frame gives you a size, just not
the right one -- so these tests check against images whose dimensions are known
independently, including the real photographs in the repository.

**Not decoding barcodes or running OCR.** Those live in the browser and this
deliberately does not reimplement them, so an uploaded capture has no regions.
The test says so explicitly, because "no regions" looks like a bug until you
know it means "nobody has read this yet".

Everything here drives the real routes through `TestClient`. No socket.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.scripts.upload_capture import (
    SUPPORTED,
    UploadError,
    image_size,
    upload_one,
)

#: A real photograph, 1152x2048, committed as a frontend fixture.
DIGIKEY = (
    Path(__file__).parents[3]
    / "frontend"
    / "src"
    / "lib"
    / "capture"
    / "fixtures"
    / "digikey-creased-datamatrix.jpg"
)


class _Client:
    """The three calls, over the real routes, through `TestClient`."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def upload_document(self, data: bytes, *, media_type: str, filename: str) -> dict[str, Any]:
        response = self.client.post(
            "/api/documents",
            content=data,
            params={"media_type": media_type, "kind": "photo", "filename": filename},
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def create_capture(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post("/api/captures", json=body)
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    def park_intake(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post("/api/intake/pending", json=body)
        assert response.status_code == 201, response.text
        entry: dict[str, Any] = response.json()
        return entry

    def find_capture(self, sha256: str) -> dict[str, Any] | None:
        response = self.client.get("/api/captures", params={"limit": 100})
        assert response.status_code == 200, response.text
        for row in response.json()["items"]:
            if row["document"]["sha256"] == sha256:
                found: dict[str, Any] = row
                return found
        return None


def _png(width: int, height: int) -> bytes:
    """A PNG header with the given size. Enough for IHDR; not a valid image."""
    header = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    return b"\x89PNG\r\n\x1a\n" + header


def _jpeg(width: int, height: int, *, marker: int = 0xC0, decoys: bool = False) -> bytes:
    """A JPEG whose only real content is a frame header of the given size.

    With `decoys`, a Huffman table (FFC4) is placed *before* the frame header,
    carrying different dimensions in the position a naive reader would look.
    That is the bug this guards: FFC4 is inside the SOF range numerically and is
    not a frame.
    """
    out = b"\xff\xd8"
    if decoys:
        payload = struct.pack(">BHH", 8, 4444, 3333)
        out += b"\xff\xc4" + struct.pack(">H", len(payload) + 2) + payload
    frame = struct.pack(">BHH", 8, height, width)
    out += bytes([0xFF, marker]) + struct.pack(">H", len(frame) + 2) + frame
    return out


# ---------------------------------------------------------------------------
# Dimensions, without an imaging library
# ---------------------------------------------------------------------------


def test_a_real_photograph_measures_correctly() -> None:
    """The one that matters: a genuine phone JPEG, not a synthetic header."""
    assert image_size(DIGIKEY.read_bytes()) == (1152, 2048)


def test_png_dimensions_come_from_ihdr() -> None:
    assert image_size(_png(640, 480)) == (640, 480)


@pytest.mark.parametrize("marker", [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC9])
def test_every_real_frame_marker_is_read(marker: int) -> None:
    # Baseline, extended sequential, progressive, lossless, differential and
    # arithmetic-coded frames are all frames.
    assert image_size(_jpeg(800, 600, marker=marker)) == (800, 600)


def test_a_huffman_table_is_not_mistaken_for_a_frame() -> None:
    """FFC4 is in the SOF numeric range and is not a frame header.

    A reader that treats it as one returns 4444x3333 here -- plausible numbers,
    silently wrong, and stored on the capture row forever.
    """
    assert image_size(_jpeg(800, 600, decoys=True)) == (800, 600)


def test_something_that_is_not_an_image_is_refused_by_its_bytes() -> None:
    # By magic bytes, not by extension: a .jpg that is really a PDF must fail.
    with pytest.raises(UploadError, match="magic bytes"):
        image_size(b"%PDF-1.7 not a photograph at all")


def test_a_truncated_jpeg_says_so_rather_than_guessing() -> None:
    with pytest.raises(UploadError, match="frame header"):
        image_size(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_a_photograph_becomes_a_document_and_a_capture(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    photo = tmp_path / "bench.jpg"
    photo.write_bytes(DIGIKEY.read_bytes())

    result = upload_one(_Client(client), photo, note="top shelf")

    assert result.width_px, result.height_px == (1152, 2048)
    assert result.sha256 == hashlib.sha256(DIGIKEY.read_bytes()).hexdigest()

    # And it is really there, on the screen a person opens.
    listed = client.get("/api/captures").json()
    assert any(row["id"] == result.capture_id for row in listed["items"])

    capture = client.get(f"/api/captures/{result.capture_id}").json()
    assert capture["document"]["sha256"] == result.sha256
    assert capture["note"] == "top shelf"


def test_an_uploaded_capture_has_no_regions_and_says_so(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    """Not a gap. Barcodes and OCR are the browser's job (ADR 0015).

    Reimplementing them here would be a second reader that drifts from the one
    the PWA actually uses, and would be believed. `not_attempted` is the honest
    state and is what the capture screen should show as "nobody has read this".
    """
    photo = tmp_path / "bench.jpg"
    photo.write_bytes(DIGIKEY.read_bytes())

    result = upload_one(_Client(client), photo)

    capture = client.get(f"/api/captures/{result.capture_id}").json()
    assert capture["regions"] == []
    assert capture["text_status"] == "not_attempted"


def test_parking_puts_it_in_the_queue_a_person_works_through(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    photo = tmp_path / "bench.jpg"
    photo.write_bytes(DIGIKEY.read_bytes())

    result = upload_one(_Client(client), photo, park=True)

    assert result.intake_id is not None
    entries = client.get("/api/intake/pending").json()["entries"]
    parked = next(row for row in entries if row["id"] == result.intake_id)
    assert parked["capture_id"] == result.capture_id
    assert parked["status"] == "pending"
    # Nothing has claimed to know what the part is.
    assert parked["mpn"] is None
    # `raw_payload` is mandatory and nothing was scanned, so it carries the
    # image identity behind a prefix no symbology produces.
    assert parked["raw_payload"] == f"capture:{result.sha256}"
    # And deliberately NOT filed as UNKNOWN: that value measures vendor
    # formats nobody parses yet, and an upload is not one.
    assert parked["decoded_kind"] is None


def test_uploading_the_same_photograph_twice_is_idempotent(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    """Content addressing means a re-run costs a hash and changes nothing.

    Worth asserting because the obvious way to use this script is to point it at
    a directory and run it again after adding one file.
    """
    photo = tmp_path / "bench.jpg"
    photo.write_bytes(DIGIKEY.read_bytes())

    first = upload_one(_Client(client), photo, park=True)
    second = upload_one(_Client(client), photo, park=True)

    assert second.sha256 == first.sha256
    assert second.deduplicated is True
    # The blob is shared and the intake entry is the same one, keyed on the hash.
    assert second.intake_id == first.intake_id
    # **And the capture too.** This assertion is the one that was missing: the
    # first version of this test checked the sha and the intake id, both of which
    # were already idempotent, and passed while every re-run created a fresh
    # capture row. Two photographs uploaded twice against the cluster produced
    # four captures before anyone noticed.
    assert second.capture_id == first.capture_id
    assert second.capture_reused is True
    assert first.capture_reused is False

    captures = client.get("/api/captures").json()
    assert captures["total"] == 1


def test_a_file_that_is_not_an_image_is_refused_before_the_network(
    client: TestClient, tmp_path: Path
) -> None:
    doc = tmp_path / "datasheet.pdf"
    doc.write_bytes(b"%PDF-1.7")

    with pytest.raises(UploadError, match="not a JPEG or PNG"):
        upload_one(_Client(client), doc)


def test_an_empty_file_is_refused_by_name(client: TestClient, tmp_path: Path) -> None:
    empty = tmp_path / "nothing.jpg"
    empty.write_bytes(b"")

    with pytest.raises(UploadError, match=r"nothing\.jpg is empty"):
        upload_one(_Client(client), empty)


def test_the_supported_types_match_what_the_blob_store_accepts() -> None:
    """Refusing here means a message naming the file, not a 400 naming a hash."""
    from app.services.blobstore import MEDIA_TYPES

    assert set(SUPPORTED.values()) <= set(MEDIA_TYPES)
