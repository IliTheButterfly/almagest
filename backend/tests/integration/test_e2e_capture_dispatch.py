"""End to end: a photograph set aside becomes a part somebody can review.

`test_e2e_autonomous.py` proves the chain works when you already know the part
number. This proves the link in front of it: **a capture, and nothing else.** That
is the flow the bench actually has -- a phone photograph of a bag parked in the
intake queue at 2 a.m., and a filled-in part waiting in the morning.

The flow, in the order it happens:

1. **A photograph.** The real one in this repository, uploaded as a document and
   recorded as a `capture` with the regions the browser decoded off it.
2. **Parked**, into the intake queue, exactly as `ScanScreen` parks one.
3. **A vision model proposes identities** -- ranked, quoted, never asserted.
4. The best one becomes a **stub part**, which is the same act as enqueuing it for
   research (`Part.research_state` defaults to `PENDING`).
5. The existing chain runs: propose URLs, fetch, validate against the part number,
   extract, cross-check, promote what clears the bar.
6. What did not clear it is **in the review queue with its source line**.

## What is faked, and what is emphatically not

Faked: the network (`_Fetcher` is a dict), the extraction model (a fixed response
through the *real* `parse_response`), and the vision model (`FakeVisionProvider`
replaying a fixture through the *real* `parse_response`). Those three are the only
things that cannot run offline, and each is the seam the design put there.

**Not faked:** real Alembic migrations, the real blob store holding the real
JPEG, the real capture and intake routes, the real validation gate, the real
cross-check, the real promotion rules, the real enrichment queue.

## The two assertions that matter

**The OCR was wrong and the pipeline was right.** tesseract read this bag's part
number as `CFI4JT100K` -- capital I for the digit 1 -- and that misreading is real,
recorded in `digikey-label-26.json`. The part this run produces must be
`CF14JT100K`. A pipeline that trusted the OCR line would produce a part number
that does not exist, find no datasheet for it, and report `EXHAUSTED` as though
the part were obscure rather than misread.

**Nothing accepted the identity.** At the end of a fully unattended run the part
is still `is_stub`, the intake entry is still `pending`, and a person still has to
say yes. Every *field* is populated; the *identity* always waits. That asymmetry
is ADR 0017's, and it is the whole reason this pipeline is allowed to run
unattended at all.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enrichment import ParameterValueCandidate
from app.models.enums import ResearchState, ValueType
from app.models.parameter import ParameterTemplate
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services.agent import pipeline
from app.services.enrichment.extract import (
    ExtractionRequest,
    ExtractionResult,
    TargetField,
    parse_response,
)
from app.services.enrichment.providers import ManualProvider
from app.services.enrichment.vision import FakeVisionProvider, VisionRequest
from app.services.extractors import PyPdfExtractor
from app.services.scanning.codes import normalize_mpn
from tests import pdfs
from tests.factories import make_part

pytest.importorskip("pypdf", reason="the `datasheets` extra is not installed")

#: The one photograph in this repository: a DigiKey bag of Stackpole carbon-film
#: resistors, creased across the Data Matrix. `barcodes.fixture.test.ts` is the
#: other test that reads it, and it is where the part number below comes from.
PHOTO = (
    Path(__file__).parents[3]
    / "frontend"
    / "src"
    / "lib"
    / "capture"
    / "fixtures"
    / "digikey-creased-datamatrix.jpg"
)

#: What the Data Matrix actually carries, in DI `1P`.
MPN = "CF14JT100K"
MPN_NORM = normalize_mpn(MPN)
#: What tesseract read off the same label. Capital I for the digit 1 -- a real
#: recorded misreading, from `digikey-label-26.json`.
OCR_MISREAD = "CFI4JT100K"

VISION_FIXTURE = Path(__file__).parents[1] / "fixtures" / "vision" / "capture_identities.json"

RIGHT_URL = "https://stackpole.test/CF14.pdf"
WRONG_URL = "https://elsewhere.test/CFR25.pdf"


class _Fetched:
    def __init__(self, data: bytes | None, content_type: str | None = None) -> None:
        self.data = data
        self.content_type = content_type


class _Fetcher:
    """The open internet, as a dict. The only network in this test."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def fetch(self, url: str) -> _Fetched:
        self.asked.append(url)
        if url == RIGHT_URL:
            return _Fetched(
                pdfs.with_text(
                    [
                        f"Stackpole {MPN}\nResistance 100 kOhm\n"
                        "Tolerance 5%\nPower rating 0.25 W\nCarbon film, axial"
                    ]
                ),
                "application/pdf",
            )
        if url == WRONG_URL:
            # Real, parseable, genuine -- and for a different part. The gate.
            return _Fetched(
                pdfs.with_text(["Yageo CFR-25JB-52-100R 100 Ohm 5% 0.25W"]), "application/pdf"
            )
        return _Fetched(None)


class _Model:
    """An `ExtractionProvider` replaying one response through the real parser.

    Two fields, at deliberately different confidences, because the point of this
    test is the *asymmetry*: one clears the promotion bar unattended and one does
    not, and both are correct outcomes.
    """

    name = "fake-local"
    model = "qwen3:8b"

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        payload = {
            "variants": [
                {
                    "mpn": MPN,
                    "mpn_source_text": f"Stackpole {MPN}",
                    "fields": [
                        {
                            "template_name": "resistance",
                            "raw_value": "100 kOhm",
                            "confidence": 0.94,
                            "source_text": "Resistance 100 kOhm",
                        },
                        {
                            "template_name": "power_rating",
                            "raw_value": "0.25 W",
                            # Below AUTO_PROMOTE_CONFIDENCE on purpose: this is the
                            # field that must end up in front of a person.
                            "confidence": 0.55,
                            "source_text": "Power rating 0.25 W",
                        },
                    ],
                }
            ]
        }
        return parse_response(
            json.loads(json.dumps(payload)), request, provider=self.name, model=self.model
        )


def _fields() -> tuple[TargetField, ...]:
    return (
        TargetField(name="resistance", value_type=ValueType.NUMERIC, unit="ohm"),
        TargetField(name="power_rating", value_type=ValueType.NUMERIC, unit="W"),
    )


def _upload_photo(client: TestClient) -> str:
    """The real JPEG into the real blob store. Returns its sha256."""
    data = PHOTO.read_bytes()
    response = client.post(
        "/api/documents",
        content=data,
        params={"media_type": "image/jpeg", "kind": "photo", "filename": "capture.jpg"},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    sha256: str = response.json()["document"]["sha256"]
    assert sha256 == hashlib.sha256(data).hexdigest()
    return sha256


def _corners(x: int, y: int, w: int = 220, h: int = 40) -> list[dict[str, int]]:
    """Top-left, top-right, bottom-right, bottom-left -- `zxing-wasm`'s order."""
    return [
        {"x": x, "y": y},
        {"x": x + w, "y": y},
        {"x": x + w, "y": y + h},
        {"x": x, "y": y + h},
    ]


def _create_capture(client: TestClient, sha256: str) -> dict[str, object]:
    """The capture as the browser records it: a decoded symbol and OCR'd lines.

    The barcode payload is a real ECIA-format Data Matrix string with the part
    number in DI `1P`; the text regions are the printed lines tesseract returns,
    including the misread one.
    """
    response = client.post(
        "/api/captures",
        json={
            "sha256": sha256,
            "width_px": 1080,
            "height_px": 1080,
            "text_status": "ok",
            "regions": [
                {
                    "kind": "barcode",
                    "symbology": "DataMatrix",
                    "text": (
                        "[)>\x1e06\x1dPCF14JT100KCT-ND\x1d1PCF14JT100K"
                        "\x1dK81927431\x1d10K98220323\x1d9D2114"
                    ),
                    "corners": _corners(120, 120, 260, 260),
                },
                {
                    "kind": "text",
                    "text": "Part Number",
                    "confidence": 93,
                    "corners": _corners(120, 520),
                },
                {
                    "kind": "text",
                    "text": OCR_MISREAD,
                    "confidence": 66,
                    "corners": _corners(120, 570),
                },
                {
                    "kind": "text",
                    "text": "RES 100K OHM 5% 1/4W AXIAL",
                    "confidence": 81,
                    "corners": _corners(120, 620, 420),
                },
            ],
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def _park(client: TestClient, capture_id: int) -> dict[str, object]:
    """Set the capture aside, exactly as `ScanScreen` does.

    Note what is parked: the **barcode's** reading of the part number, because
    that is the only thing on this row a machine is allowed to have written. The
    vision model's opinion never goes here.
    """
    response = client.post(
        "/api/intake/pending",
        json={
            "client_op_id": "capture-dispatch-proving-slice",
            "raw_payload": MPN,
            "symbology": "DataMatrix",
            "capture_id": capture_id,
            "mpn": MPN,
        },
    )
    assert response.status_code == 201, response.text
    entry: dict[str, object] = response.json()["entry"]
    return entry


def _vision_request(client: TestClient, sha256: str, capture: dict[str, object]) -> VisionRequest:
    """Build the request the worker would, from what the capture already holds.

    The worker fetches the image over HTTP rather than reading the blob volume --
    ADR 0005, the same way `extract_datasheets.py` gets its PDFs.
    """
    image = client.get(f"/api/documents/{sha256}").content
    regions = capture["regions"]
    assert isinstance(regions, list)
    return VisionRequest(
        image=image,
        media_type="image/jpeg",
        document_sha256=sha256,
        barcode_texts=tuple(r["text"] for r in regions if r["kind"] == "barcode"),
        ocr_lines=tuple(r["text"] for r in regions if r["kind"] == "text"),
        # Anchored: the Data Matrix already said what this is, so the model is
        # asked to confirm rather than to guess among alternatives.
        max_candidates=1,
    )


def test_a_parked_photograph_becomes_a_part_awaiting_review(
    client: TestClient, db: Session
) -> None:
    """The headline claim, end to end, with nothing but a photograph to start.

    Every step is a real service. The three fakes are the network and the two
    models, which is the same boundary `test_e2e_autonomous.py` draws.
    """
    # Templates are curated content and are NOT seeded by migrations. Without
    # them the model's fields land in `unknown_templates` and are skipped --
    # correctly, since a model must never create a template -- and this test
    # would assert nothing at all.
    seed_categories(db)
    seed_parameter_templates(db)
    # Committed before the first request: this session and the API's share one
    # SQLite file, and an open write transaction here locks every route below.
    db.commit()

    sha256 = _upload_photo(client)
    capture = _create_capture(client, sha256)
    capture_id = capture["id"]
    assert isinstance(capture_id, int)
    entry = _park(client, capture_id)

    # --- the new link: a photograph proposes identities -------------------
    vision = FakeVisionProvider(VISION_FIXTURE)
    read = vision.read(_vision_request(client, sha256, capture))

    assert read.identified, "an anchored, legible label must yield a candidate"
    best = read.best
    assert best is not None
    # THE assertion. The OCR line in this capture says CFI4JT100K; the part the
    # pipeline goes on to define says CF14JT100K.
    assert best.mpn == MPN
    assert best.mpn != OCR_MISREAD
    # And it quoted what it read, which is what a reviewer checks instead of
    # taking the model's word.
    assert best.source_text

    # --- the identity becomes a stub, which is the same act as enqueuing it ---
    part = make_part(db, best.mpn, mpn=best.mpn, is_stub=True)
    db.commit()
    assert part.research_state is ResearchState.PENDING, (
        "creating a stub must be the same act as enqueuing it for research"
    )

    # --- the existing chain, unchanged ------------------------------------
    def store_document(part_id: int, data: bytes, source_url: str) -> str:
        response = client.post(
            f"/api/documents?media_type=application/pdf&part_id={part_id}"
            f"&role=datasheet&is_primary=true&source_url={source_url}",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text
        return str(response.json()["document"]["sha256"])

    result = pipeline.define_part_from_mpn(
        db,
        part=part,
        # The wrong sheet first, again: a run that stopped at the first parseable
        # PDF would build this part from a 100 ohm resistor's datasheet.
        providers=[ManualProvider({MPN_NORM: [WRONG_URL, RIGHT_URL]})],
        fetcher=_Fetcher(),
        extractor=PyPdfExtractor(),
        extraction=_Model(),
        fields=_fields(),
        store_document=store_document,
    )
    db.commit()

    assert result.research_state is ResearchState.RESOLVED
    assert result.document_sha256 is not None
    assert dict(result.rejections)[WRONG_URL] == "mpn_absent"

    # --- every field populated; the uncorroborated one awaits a glance ----
    assert "resistance" in result.promoted, (
        "a high-confidence field in an empty slot promotes unattended -- "
        "ADR 0017 says that asymmetry is the feature"
    )
    assert "power_rating" in result.queued_for_review

    # --- and it is genuinely in the queue a person opens ------------------
    recorded = set(
        db.scalars(
            select(ParameterTemplate.name)
            .join(
                ParameterValueCandidate,
                ParameterValueCandidate.template_id == ParameterTemplate.id,
            )
            .where(ParameterValueCandidate.part_id == part.id)
        ).all()
    )
    assert recorded >= {"resistance", "power_rating"}

    queue = client.get("/api/enrichment/candidates", params={"part_id": part.id})
    assert queue.status_code == 200, queue.text
    groups = [group for row in queue.json()["parts"] for group in row["fields"]]
    queued = {group["template_name"]: group for group in groups}
    assert "power_rating" in queued, "the field that did not clear the bar must be reviewable"
    # And it is reviewable in the sense that matters: the line of the datasheet
    # it was read from travels with it, so a person checks the source rather
    # than taking the model's word.
    assert any(
        "Power rating 0.25 W" in (row["note"] or "") for row in queued["power_rating"]["candidates"]
    ), "a candidate a reviewer cannot trace back to the document is not reviewable"

    # --- nothing accepted the identity ------------------------------------
    db.refresh(part)
    assert part.is_stub is True, "an unattended run never un-stubs a part"
    still_pending = client.get("/api/intake/pending").json()["entries"]
    assert any(row["id"] == entry["id"] and row["status"] == "pending" for row in still_pending), (
        "the intake entry must still be waiting for a person to resolve it"
    )
