"""Put a photograph from this machine into a running Almagest.

The PWA is the normal way a capture reaches the server: the browser grabs a
frame, decodes the barcodes, OCRs the printed lines and posts the lot. This is
for the times that does not apply -- a photo taken on a phone and copied over, a
corpus being assembled for the benchmark, a picture somebody sent you.

    python -m app.scripts.upload_capture ~/bench-photos/*.jpg
    python -m app.scripts.upload_capture --park --note "top shelf" label.jpg

It creates the same rows the PWA creates -- a `documents` blob and a `captures`
row referencing it -- so an uploaded photograph appears on the captures screen
and, with `--park`, in the intake queue, exactly as a scanned one does.

## What it deliberately does not do

**No barcode decoding and no OCR.** Those live in the browser (ADR 0015) and
reproducing them here would be a second implementation of the one thing this
project has been careful to keep single: `zxing-wasm` and `tesseract.js` are the
readers, and a Python approximation of them would drift and be believed. An
uploaded capture therefore has **no regions** until something reads it, which is
an honest state rather than a gap -- the capture screen shows the photograph with
nothing outlined, and that is what "nobody has read this yet" should look like.

**No image decoding either**, which is the more interesting restriction. The API
requires `width_px`/`height_px` and refuses to derive them itself (ADR 0005: it
must not grow an imaging dependency for a single replica pinned to an RWO
volume). So this reads the dimensions out of the file *header* -- a JPEG's SOF
marker, a PNG's IHDR -- in about forty lines of stdlib. Pillow would be a
hundred megabytes of wheels to learn two integers.

## Why it talks HTTP rather than opening the database

The same reason `extract_datasheets.py` and `research_datasheets.py` do. It runs
wherever the photographs are, which is not where the volume is mounted, and the
single SQLite writer stays single. `ApiClient` is a Protocol for the same reason
theirs is: the tests drive it with FastAPI's `TestClient` and never open a
socket.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

#: What the blob store will accept (`blobstore.MEDIA_TYPES`). Checked here as
#: well so a wrong file is refused before it crosses the network, with a message
#: that names the file rather than a 400 that names a sha256.
SUPPORTED = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

#: Matches `blobstore.MAX_DOCUMENT_BYTES`. Duplicated deliberately: the point is
#: to fail on this machine, where the person can see which photograph is too big.
MAX_BYTES = 64 * 1024 * 1024


class UploadError(RuntimeError):
    """This photograph cannot be uploaded, and the message says which and why."""


# ---------------------------------------------------------------------------
# Reading dimensions without decoding the image
# ---------------------------------------------------------------------------


def _png_size(data: bytes) -> tuple[int, int]:
    """IHDR is always the first chunk and always at a fixed offset."""
    if len(data) < 24:
        raise UploadError("PNG is truncated before its header")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Walk the marker segments to the frame header.

    A JPEG is a chain of `FF <marker> <length> <payload>` segments, and the one
    that carries the picture's size is a Start Of Frame -- SOF0 for baseline,
    SOF2 for progressive, and a family of others. Its payload begins with a
    precision byte then height then width, in that order, big-endian.

    Skipped deliberately: `FFD8` (start of image) and `FFD9` (end) have no
    length; `FFD0`-`FFD7` are restart markers and have none either; `FFC4`,
    `FFC8` and `FFCC` sit inside the SOF numeric range and are *not* frame
    headers -- Huffman tables, a JPEG extension and arithmetic coding
    conditioning. Treating those three as SOF is the classic bug in this
    forty-line function, so they are named rather than inferred.
    """
    index = 2  # past FFD8
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > len(data):
            break
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 > len(data):
                break
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        index += 2 + length
    raise UploadError("no frame header found; is this really a JPEG?")


def image_size(data: bytes) -> tuple[int, int]:
    """`(width_px, height_px)`, from the file header alone."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_size(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    raise UploadError("not a JPEG or PNG (checked the magic bytes, not the extension)")


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Uploaded:
    """One photograph, and where it ended up."""

    path: Path
    sha256: str
    width_px: int
    height_px: int
    byte_size: int
    capture_id: int
    #: True when the blob store already held these exact bytes. Not an error --
    #: content addressing means re-uploading the same photograph is free and
    #: idempotent -- but worth printing, because it usually means somebody ran
    #: this twice and wondered why nothing changed.
    deduplicated: bool
    intake_id: int | None = None


class ApiClient(Protocol):
    """The three calls this needs. A Protocol so tests need no socket."""

    def upload_document(self, data: bytes, *, media_type: str, filename: str) -> dict[str, Any]: ...

    def create_capture(self, body: dict[str, Any]) -> dict[str, Any]: ...

    def park_intake(self, body: dict[str, Any]) -> dict[str, Any]: ...


class HttpApiClient:
    """`urllib`, like every other client here. No SDK, no session, no retries."""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self, path: str, data: bytes, content_type: str, *, expect: tuple[int, ...]
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status not in expect:
                    raise UploadError(f"{path} answered {response.status}")
                decoded: Any = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read()[:500].decode("utf-8", "replace")
            raise UploadError(f"HTTP {error.code} from {path}: {detail}") from error
        except (urllib.error.URLError, OSError) as error:
            raise UploadError(f"cannot reach {self.base_url}: {error}") from error
        if not isinstance(decoded, dict):
            raise UploadError(f"{path} returned {type(decoded).__name__}, not an object")
        return decoded

    def upload_document(self, data: bytes, *, media_type: str, filename: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"media_type": media_type, "kind": "photo", "filename": filename}
        )
        return self._request(
            f"/api/documents?{query}", data, "application/octet-stream", expect=(200,)
        )

    def create_capture(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "/api/captures", json.dumps(body).encode(), "application/json", expect=(200, 201)
        )

    def park_intake(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "/api/intake/pending",
            json.dumps(body).encode(),
            "application/json",
            expect=(200, 201),
        )


def upload_one(
    client: ApiClient,
    path: Path,
    *,
    park: bool = False,
    note: str | None = None,
    device_id: str | None = None,
) -> Uploaded:
    """One photograph to a document, a capture, and optionally an intake entry."""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise UploadError(f"{path.name}: {suffix or 'no extension'} is not a JPEG or PNG")
    data = path.read_bytes()
    if not data:
        raise UploadError(f"{path.name} is empty")
    if len(data) > MAX_BYTES:
        raise UploadError(f"{path.name} is {len(data) / 1e6:.0f} MB; the ceiling is 64 MB")

    width, height = image_size(data)
    media_type = SUPPORTED[suffix]

    # `deduplicated` is a sibling of `document`, not a field inside it: it is a
    # fact about *this upload*, not about the stored blob, and can be True while
    # `created` is also True when a previous attempt died between the write and
    # the commit.
    response = client.upload_document(data, media_type=media_type, filename=path.name)
    document = response["document"]
    sha256 = str(document["sha256"])
    # Checked rather than trusted: the sha256 is the whole provenance story, and
    # a mismatch here would mean the bytes on the wire were not the bytes on
    # disk.
    local = hashlib.sha256(data).hexdigest()
    if sha256 != local:
        raise UploadError(f"{path.name}: server stored {sha256}, local bytes hash to {local}")

    body: dict[str, Any] = {
        "sha256": sha256,
        "width_px": width,
        "height_px": height,
        # Nothing has read this image. `not_attempted` is exactly that state, and
        # is why an uploaded capture is honest rather than incomplete.
        "text_status": "not_attempted",
        "regions": [],
    }
    if note:
        body["note"] = note
    if device_id:
        body["device_id"] = device_id
    capture = client.create_capture(body)

    intake_id = None
    if park:
        # `raw_payload` is mandatory and nothing was scanned, so it carries the
        # image's identity with a prefix that cannot be mistaken for a symbology.
        #
        # **`decoded_kind` is deliberately left NULL rather than set to
        # `UNKNOWN`.** That value is not a shrug -- its docstring says a rising
        # share of it means "a vendor format nobody parses yet, and the raw
        # payloads to build that parser from are sitting in the same table".
        # Filing uploads under it would inflate the one number that is supposed
        # to say where intake hurts, and salt the sample somebody would later
        # mine for a parser with strings that were never barcodes.
        #
        # Keyed on the sha256 so re-running this is idempotent -- the same
        # property `client_op_id` gives the PWA. Re-parking a photograph returns
        # the existing entry instead of making a second one.
        entry = client.park_intake(
            {
                "client_op_id": f"upload-{sha256[:24]}",
                "raw_payload": f"capture:{sha256}",
                "capture_id": capture["id"],
                "note": note,
            }
        )
        intake_id = int(entry["entry"]["id"])

    return Uploaded(
        path=path,
        sha256=sha256,
        width_px=width,
        height_px=height,
        byte_size=len(data),
        capture_id=int(capture["id"]),
        deduplicated=bool(response.get("deduplicated", False)),
        intake_id=intake_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upload_capture",
        description="Put a photograph from this machine into a running Almagest.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="JPEG or PNG files")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--park",
        action="store_true",
        help="also queue each capture in the intake queue, as a scan would",
    )
    parser.add_argument("--note", default=None, help="free text stored on the capture")
    parser.add_argument("--device-id", default=None, help="what took the picture")
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help=(
            "also copy each image to this directory named by its sha256 "
            "(bench/corpus/_captures is the gitignored one the corpus loader reads)"
        ),
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="carry on after a file fails instead of stopping",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    client = HttpApiClient(args.base_url)
    failures = 0

    for path in args.paths:
        try:
            result = upload_one(
                client, path, park=args.park, note=args.note, device_id=args.device_id
            )
        except UploadError as error:
            failures += 1
            print(f"FAILED {path}: {error}", file=sys.stderr)
            if not args.keep_going:
                return 1
            continue

        if args.cache:
            args.cache.mkdir(parents=True, exist_ok=True)
            suffix = mimetypes.guess_extension(SUPPORTED[path.suffix.lower()]) or path.suffix
            (args.cache / f"{result.sha256}{'.jpg' if suffix == '.jpe' else suffix}").write_bytes(
                path.read_bytes()
            )

        where = f"capture {result.capture_id}"
        if result.intake_id is not None:
            where += f", intake {result.intake_id}"
        again = " (already stored)" if result.deduplicated else ""
        print(
            f"{path.name}: {result.width_px}x{result.height_px}, "
            f"{result.byte_size / 1000:.0f} kB -> {where}{again}\n  {result.sha256}"
        )

    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
