"""End to end: a photograph becomes a fully specified part, fields and category included.

`test_e2e_capture_dispatch.py` proves a parked photograph reaches a *reviewable*
part, and it stops there on purpose — nothing accepts the identity. `test_e2e_autonomous.py`
proves the chain works when the part number is already known. Both stop short of the
two steps that a real intake almost always needs, and that no test in this repository
covers together with the rest of the chain:

* **the vocabulary is incomplete.** The datasheet says something the install has no
  `parameter_template` for. That is not an error and not a licence to invent one — the
  field lands in `IngestReport.unknown_templates` and is skipped, and it stays skipped
  until a *person* authors the field. Then the same extraction promotes it.
* **the taxonomy is incomplete.** The part belongs in a category nobody has created
  yet, so a person creates it and files the part under it.

So this file is the whole pipeline in one run, through every real door:

1. **A photograph** into the blob store, recorded as a `capture` with the regions the
   browser decoded off it — one Data Matrix and three OCR'd lines, one of them wrong.
2. **Parked** into the intake queue, exactly as `ScanScreen` parks one.
3. **Dispatch requested.** Opt-in, because a read costs a GPU handover.
4. **The vision worker runs** over the real claim/submit routes and proposes an
   identity, minting a stub part for it.
5. **A person accepts it** through `POST /api/intake/pending/{id}/resolve` — the only
   door there is — and files the part under a category they have to create first.
6. **Research** finds the datasheet, having refused a real one for a different part.
7. **Extraction** reads the PDF's text through the queue, and a model reads fields
   out of that text.
8. **The missing field**, twice: refused, authored by hand, then accepted.
9. **Promotion**, with everything that did not clear the bar sitting in the review
   queue with the datasheet line it was read from.

## What is faked, and what is emphatically not

Faked: the network (`_Fetcher` is a dict), the vision model (`FakeVisionProvider`
replaying a fixture through the *real* `parse_response`) and the field-extraction model
(a fixed JSON response through the *real* `parse_response`). Those are the three things
that cannot run offline, and each is a seam the design put there.

**Not faked:** real Alembic migrations, the real blob store holding the real JPEG and
the real PDF, the real capture/intake/dispatch/research/extraction routes, the real
three worker loops, the real part-number validation gate, the real PDF text extractor,
the real cross-check, the real promotion rules, the real authoring routes.

The three worker adapters are `HttpApiClient` subclasses with `urllib` swapped for
`TestClient`, never hand-rolled Protocol implementations — the URLs and JSON bodies
under test are then the ones the real workers build, which a hand-rolled fake would
quietly replace with the test's own guesses.

## The assertions that make it worth running

**A model never wrote a decision.** Between the vision run and the human accept, the
intake entry is still `pending`, `resolved_part_id` is NULL, and `pending_intakes.mpn`
still says what the *barcode* said. Every stored reading's confidence is below
`candidates.AUTO_PROMOTE_CONFIDENCE`, compared against the constant rather than a
literal so a change to the bar cannot leave this passing by coincidence.

**A model never wrote vocabulary.** `tolerance` is a real property of this resistor,
printed on its datasheet, read correctly at 0.91 confidence — and it is dropped,
because templates are curated content. That refusal is the assertion; the recovery
(a person authors it, the same response now promotes) is the proof the refusal is a
gate rather than a dead end.
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

from app.models.catalog import Part, PartCategory
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import (
    CandidateStatus,
    DispatchState,
    PendingIntakeStatus,
    PromotionOutcome,
    ResearchState,
    ValueType,
)
from app.models.parameter import ParameterTemplate, ParameterValue
from app.scripts import dispatch_captures, extract_datasheets, research_datasheets
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services.enrichment import candidates, cross_check
from app.services.enrichment.extract import (
    ExtractionRequest,
    ExtractionResult,
    TargetField,
    parse_response,
)
from app.services.enrichment.providers import ManualProvider
from app.services.enrichment.vision import FakeVisionProvider
from app.services.extractors import PyPdfExtractor
from app.services.scanning.codes import normalize_mpn
from tests import pdfs

pytest.importorskip("pypdf", reason="the `datasheets` extra is not installed")

#: The one photograph in this repository: a DigiKey bag of Stackpole carbon-film
#: resistors, creased across the Data Matrix.
PHOTO = (
    Path(__file__).parents[3]
    / "frontend"
    / "src"
    / "lib"
    / "capture"
    / "fixtures"
    / "digikey-creased-datamatrix.jpg"
)

#: What the Data Matrix carries, in DI `1P`.
MPN = "CF14JT100K"
MPN_NORM = normalize_mpn(MPN)
#: What tesseract read off the same label — capital I for the digit 1. A recorded
#: misreading, from `digikey-label-26.json`, and the reason the OCR line is never
#: allowed to become the part number.
OCR_MISREAD = "CFI4JT100K"

#: A field this install has no `parameter_template` for, and a real property of the
#: part: every carbon-film resistor has one and this datasheet prints it. Chosen over
#: an invented name precisely so the refusal cannot be read as "the model made something
#: up" — the model is right, and it is still refused.
MISSING_FIELD = "tolerance"

#: A category the seed taxonomy does not have. `resistor` exists; this is the leaf a
#: person would file a carbon-film part under.
NEW_CATEGORY = "resistor-carbon-film"

RIGHT_URL = "https://stackpole.test/CF14.pdf"
WRONG_URL = "https://elsewhere.test/CFR25.pdf"

#: The datasheet's text, and every `source_text` below is a verbatim line of it. The
#: part number has to be in here or `datasheet_validation` refuses the PDF, which is
#: the gate `WRONG_URL` exists to exercise.
DATASHEET = (
    f"Stackpole Electronics {MPN}\n"
    "Resistance 100 kOhm\n"
    "Tolerance 5 %\n"
    "Power rating 0.25 W\n"
    "Carbon film, axial leaded"
)


# ---------------------------------------------------------------------------
# The three seams
# ---------------------------------------------------------------------------


class _Fetcher:
    """The open internet, as a dict. The only network in this test."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def fetch(self, url: str) -> research_datasheets.Fetched:
        self.asked.append(url)
        if url == RIGHT_URL:
            return research_datasheets.Fetched(pdfs.with_text([DATASHEET]), "application/pdf")
        if url == WRONG_URL:
            # Real, parseable, genuine — and for a different part. The gate.
            return research_datasheets.Fetched(
                pdfs.with_text(["Yageo CFR-25JB-52-100R 100 Ohm 5% 0.25W"]), "application/pdf"
            )
        return research_datasheets.Fetched(None, error="HTTP 404")


def _vision_fixture(tmp_path: Path, sha256: str) -> Path:
    """One recorded vision response, keyed by the photograph's own digest.

    The confidence is what the model reported on hardware for a legible label. It is
    above the promotion bar and it does not matter that it is: `dispatch` clamps a
    stored reading below `AUTO_PROMOTE_CONFIDENCE` on the way in, and the test asserts
    the clamp rather than trusting the fixture to stay polite.
    """
    path = tmp_path / "vision.json"
    path.write_text(
        json.dumps(
            {
                "provider": "local-ollama",
                "model": "qwen3-vl:8b",
                "responses": {
                    sha256: {
                        "label_kind": "bag",
                        "candidates": [
                            {
                                "mpn": MPN,
                                "manufacturer": "Stackpole Electronics",
                                "package": "axial",
                                "confidence": 0.95,
                                "source_text": f"MFR PART NO: {MPN}",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class _Model:
    """An `ExtractionProvider` replaying one response through the real parser.

    Three fields at three deliberately different confidences, because the point is the
    *asymmetry*: one promotes unattended, one goes in front of a person, and one is
    dropped for having nowhere to go. All three are correct outcomes.
    """

    name = "fake-local"
    model = "qwen3:8b"

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        payload = {
            "variants": [
                {
                    "mpn": MPN,
                    "mpn_source_text": f"Stackpole Electronics {MPN}",
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
                            # Below AUTO_PROMOTE_CONFIDENCE on purpose: the field that
                            # must end up in front of a person.
                            "confidence": 0.55,
                            "source_text": "Power rating 0.25 W",
                        },
                        {
                            "template_name": MISSING_FIELD,
                            "raw_value": "5 %",
                            # Comfortably above the bar, and read correctly. Nothing
                            # about *this* answer is why it gets dropped.
                            "confidence": 0.91,
                            "source_text": "Tolerance 5 %",
                        },
                    ],
                }
            ]
        }
        return parse_response(
            json.loads(json.dumps(payload)), request, provider=self.name, model=self.model
        )


def _target_fields() -> tuple[TargetField, ...]:
    """What the model was asked for — including the field no template backs.

    `extract.parse_response` refuses a `template_name` that was not requested, so the
    missing field has to be *asked for* to reach `cross_check.ingest` at all. That is
    the honest shape of the case: an install's prompt is built from the fields it wants,
    and `ingest` is the layer that discovers one of them is not authored yet.
    """
    return (
        TargetField(name="resistance", value_type=ValueType.NUMERIC, unit="ohm"),
        TargetField(name="power_rating", value_type=ValueType.NUMERIC, unit="watt"),
        TargetField(name=MISSING_FIELD, value_type=ValueType.NUMERIC, unit="percent"),
    )


# ---------------------------------------------------------------------------
# The three worker adapters
# ---------------------------------------------------------------------------


class _DispatchApi(dispatch_captures.HttpApiClient):
    """The capture-dispatch worker's client, over `TestClient`."""

    def __init__(self, client: TestClient) -> None:
        super().__init__("http://testserver")
        self._client = client

    def fetch_image(self, sha256: str) -> bytes:
        response = self._client.get(f"/api/documents/{sha256}")
        assert response.status_code == 200, response.text
        return response.content

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post(self._client, path, payload)


class _ResearchApi(research_datasheets.HttpApiClient):
    """The datasheet-research worker's client, over `TestClient`."""

    def __init__(self, client: TestClient) -> None:
        super().__init__("http://testserver")
        self._client = client

    def upload_datasheet(
        self, *, part_id: int, data: bytes, source_url: str, is_primary: bool
    ) -> str:
        response = self._client.post(
            "/api/documents",
            content=data,
            params={
                "media_type": "application/pdf",
                "part_id": part_id,
                "source_url": source_url,
                "role": "datasheet",
                "is_primary": str(is_primary).lower(),
            },
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text
        return str(response.json()["document"]["sha256"])

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post(self._client, path, payload)


class _ExtractionApi(extract_datasheets.HttpApiClient):
    """The PDF-text worker's client, over `TestClient`."""

    def __init__(self, client: TestClient) -> None:
        super().__init__("http://testserver")
        self._client = client

    def download(self, document: extract_datasheets.QueuedDocument) -> bytes:
        response = self._client.get(document.url)
        assert response.status_code == 200, response.text
        return response.content

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post(self._client, path, payload)


def _post(client: TestClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"/{path}", json=payload)
    if response.status_code >= 400:
        raise AssertionError(f"POST {path} -> {response.status_code}: {response.text}")
    decoded: Any = response.json() if response.content else {}
    return decoded if isinstance(decoded, dict) else {}


# ---------------------------------------------------------------------------
# Steps 1-3: a photograph, parked, and asked about
# ---------------------------------------------------------------------------


def _corners(x: int, y: int, w: int = 220, h: int = 40) -> list[dict[str, int]]:
    """Top-left, top-right, bottom-right, bottom-left — `zxing-wasm`'s order."""
    return [
        {"x": x, "y": y},
        {"x": x + w, "y": y},
        {"x": x + w, "y": y + h},
        {"x": x, "y": y + h},
    ]


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


def _create_capture(client: TestClient, sha256: str) -> int:
    """The capture as the browser records it: one decoded symbol and OCR'd lines."""
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
    return int(response.json()["id"])


def _park(client: TestClient, capture_id: int) -> int:
    """Set the capture aside, and note what goes on the row: the **barcode's** reading.

    That is the only part number on this entry a machine is allowed to have written,
    and every assertion below about `mpn` is about it staying that way.
    """
    response = client.post(
        "/api/intake/pending",
        json={
            "client_op_id": "full-pipeline-bag-1",
            "raw_payload": MPN,
            "symbology": "DataMatrix",
            "capture_id": capture_id,
            "mpn": MPN,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["entry"]["id"])


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def test_a_photograph_becomes_a_fully_specified_part(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    """The whole chain, including the two things somebody has to author on the way.

    Written as one test rather than several because the claim is about the *chain*:
    split into nine, each would need the previous eight rebuilt from fixtures, and the
    handovers between the queues — which is where a pipeline of green units breaks —
    would be the part nothing exercised.
    """
    # Templates and categories are curated content and are NOT seeded by migrations.
    # Seeded here so the *interesting* absence is the one the test is about: two of the
    # model's three fields have templates, one does not.
    seed_categories(db)
    seed_parameter_templates(db)
    # Committed before the first request: this session and the API's share one SQLite
    # file, and an open write transaction here locks every route below.
    db.commit()
    assert _template(db, MISSING_FIELD) is None, (
        f"this test is about {MISSING_FIELD!r} being unauthored; the seed now has it"
    )

    # --- 1-3. a photograph, parked, and a read asked for ------------------
    sha256 = _upload_photo(client)
    capture_id = _create_capture(client, sha256)
    intake_id = _park(client, capture_id)

    # Opt-in, unlike research's `PENDING` default: a read costs a GPU handover on an
    # exclusive card, so an entry sits at `not_requested` until somebody spends it.
    before = _entry(client, intake_id)
    assert before["dispatch_state"] == DispatchState.NOT_REQUESTED
    assert client.post("/api/dispatch/requests", json={"intake_id": intake_id}).status_code == 200

    # --- 4. the vision worker proposes an identity ------------------------
    vision = FakeVisionProvider(_vision_fixture(tmp_path, sha256))
    assert dispatch_captures.run_once(_DispatchApi(client), vision, worker_id="vision-1") == 1

    proposed = _entry(client, intake_id)
    assert proposed["dispatch_state"] == DispatchState.PROPOSED
    assert proposed["dispatch_label_kind"] == "bag"
    identities = proposed["identity_candidates"]
    assert [row["mpn"] for row in identities] == [MPN]
    assert [row["rank"] for row in identities] == [0]
    # A stub per candidate: a reading with no `parts` row is one research can never
    # test, which is how a wrong reading gets eliminated later rather than argued about.
    assert all(row["part_id"] is not None for row in identities)
    # And it quoted the label, which is what a reviewer checks instead of taking the
    # model's word for the characters.
    assert identities[0]["source_text"] == f"MFR PART NO: {MPN}"

    # THE invariant of this half. The fixture says 0.95; nothing stored may reach the
    # promotion bar, and the bar is named rather than spelled out because a change to
    # it must not leave this assertion passing by coincidence.
    assert all(row["confidence"] < candidates.AUTO_PROMOTE_CONFIDENCE for row in identities), (
        identities
    )
    # Nothing automated touched a person's decision — through the real read route, not
    # against a mock that could be wrong in the same direction as the code.
    assert proposed["status"] == PendingIntakeStatus.PENDING
    assert proposed["resolved_part_id"] is None
    assert proposed["mpn"] == MPN, "the barcode's reading, and no model may replace it"

    part_id = int(identities[0]["part_id"])
    part = db.get(Part, part_id)
    assert part is not None
    db.refresh(part)
    assert part.is_stub is True, "a model-proposed identity is exactly what `is_stub` is for"
    assert part.category_id is None, "nothing filed it, because nothing knows where it goes"

    # --- 5. a person accepts one, and has to invent a category to file it -
    resolved = client.post(
        f"/api/intake/pending/{intake_id}/resolve", json={"resolved_part_id": part_id}
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == PendingIntakeStatus.RESOLVED
    assert resolved.json()["resolved_part_id"] == part_id
    # The proposal stays attached, which is what makes the decision auditable later.
    assert resolved.json()["dispatch_state"] == DispatchState.PROPOSED

    resistors = db.execute(select(PartCategory).where(PartCategory.slug == "resistor")).scalar_one()
    created = client.post(
        "/api/part-categories",
        json={
            "name": "Carbon film",
            "slug": NEW_CATEGORY,
            "parent_id": resistors.id,
            "client_op_id": "full-pipeline-category",
        },
    )
    assert created.status_code == 201, created.text
    category = created.json()["part_category"]
    # The path cache is rebuilt on the way out, so the row comes back placeable in a
    # tree without the caller walking it — and a field authored on `resistor` below is
    # inherited here on the very next request rather than after some later job.
    assert category["label_path"].endswith("Carbon film")
    assert category["parent_id"] == resistors.id

    filed = client.patch(
        f"/api/parts/{part_id}",
        json={"category_id": category["id"], "is_stub": False},
    )
    assert filed.status_code == 200, filed.text
    assert filed.json()["category_id"] == category["id"]
    # Un-stubbed by the same human act, and only by it: the resolve route above records
    # the outcome and deliberately does not touch the part.
    assert filed.json()["is_stub"] is False

    # --- 6. research finds the datasheet, and refuses the wrong one -------
    db.expire_all()
    assert db.get(Part, part_id) is not None
    fetcher = _Fetcher()
    assert (
        research_datasheets.run_once(
            _ResearchApi(client),
            fetcher,
            PyPdfExtractor(),
            # The wrong sheet first: a run that stopped at the first parseable PDF would
            # build this resistor from a 100 ohm part's datasheet.
            [ManualProvider({MPN_NORM: [WRONG_URL, RIGHT_URL]})],
            worker_id="research-1",
        )
        == 1
    )
    assert fetcher.asked == [WRONG_URL, RIGHT_URL]

    db.expire_all()
    researched = db.get(Part, part_id)
    assert researched is not None
    # `==`, not `is`: `StrEnumType` is tolerant on read by design and hands back the
    # stored string, so an identity comparison against the member would fail here even
    # when the value is right.
    assert researched.research_state == ResearchState.RESOLVED
    linked = client.get(f"/api/parts/{part_id}/documents").json()["links"]
    primary = [row for row in linked if row["role"] == "datasheet" and row["is_primary"]]
    assert len(primary) == 1, linked
    datasheet_sha = str(primary[0]["document"]["sha256"])

    # --- 7. the extraction queue reads the PDF's text ---------------------
    assert (
        extract_datasheets.run_once(_ExtractionApi(client), PyPdfExtractor(), worker_id="text-1")
        == 1
    )
    text_body = client.get(f"/api/documents/{datasheet_sha}/text").json()
    assert text_body["extractor"] == "pypdf"
    assert text_body["low_confidence"] is False
    document_text = str(text_body["text"])
    assert MPN in document_text

    request = ExtractionRequest(
        document_ref=datasheet_sha,
        document_text=document_text,
        # The catalogue's part number, never the model's: a hallucinated character must
        # not reach the request and come back as a fact about a part.
        mpns=(MPN,),
        fields=_target_fields(),
    )
    result = _Model().extract(request)

    # --- 8a. the missing field is refused, and nothing is created for it --
    first = cross_check.ingest(db, result)
    db.commit()

    assert first.unknown_templates == (MISSING_FIELD,), (
        "a field with no template must be reported, not silently dropped"
    )
    assert _template(db, MISSING_FIELD) is None, (
        "templates are curated content and a model must not add one — this is the "
        "invariant the whole vocabulary depends on, since a search key invented by a "
        "model is one no saved search and no other install shares"
    )
    assert first.decision_for(MPN, MISSING_FIELD) is None
    # Nothing was recorded for it either. Asserted by enumerating what *was* recorded
    # rather than by looking for a `tolerance` row: with no template there is no name to
    # join on, so a search for one would come back empty whatever happened.
    assert _recorded_fields(db, part_id) == {"resistance", "power_rating"}

    # The two fields that *do* have templates went the two different right ways.
    resistance = first.decision_for(MPN, "resistance")
    assert resistance is not None and resistance.outcome is PromotionOutcome.PROMOTED
    power = first.decision_for(MPN, "power_rating")
    assert power is not None and power.outcome is PromotionOutcome.QUEUED
    assert _value(db, part_id, "power_rating") is None, (
        "single-source below AUTO_PROMOTE_CONFIDENCE must never reach parameter_value"
    )

    # --- 8b. a person authors the field, and the same reading now lands ---
    authored = client.post(
        "/api/parameter-fields",
        json={
            "name": MISSING_FIELD,
            "display_name": "Tolerance",
            "value_type": ValueType.NUMERIC,
            # A quantity name, not a unit symbol. The authoring route refuses anything
            # the value parser cannot parse against, so a field that accepts no value
            # cannot be created in the first place.
            "base_unit": "percent",
            # Not cosmetic: it is what a substitution search *means*. A tighter
            # tolerance satisfies a looser requirement, so `lower_ok`.
            "substitution_direction": "lower_ok",
            "applies_to_category": NEW_CATEGORY,
            "client_op_id": "full-pipeline-field",
        },
    )
    assert authored.status_code == 201, authored.text
    assert authored.json()["field"]["name"] == MISSING_FIELD
    assert authored.json()["reused"] is False, "it did not exist; nothing should be reused"

    second = cross_check.ingest(db, result)
    db.commit()

    assert second.unknown_templates == (), "the field exists now; nothing should be unknown"
    assert _recorded_fields(db, part_id) == {"resistance", "power_rating", MISSING_FIELD}
    tolerance = second.decision_for(MPN, MISSING_FIELD)
    assert tolerance is not None
    assert tolerance.outcome is PromotionOutcome.PROMOTED, (
        "the identical response, re-ingested, must now land — otherwise the refusal "
        "above is a dead end rather than a gate"
    )

    # --- 9. every field is either in the part or in front of a person -----
    stored = {
        name: _value(db, part_id, name) for name in ("resistance", "power_rating", MISSING_FIELD)
    }
    assert stored["resistance"] is not None
    assert stored[MISSING_FIELD] is not None
    # `value_min`/`value_max` are what make a row visible to a range query at all — a
    # null-bounded row is invisible to every parametric search, silently. Writing
    # through `services.parameters` is what prevents that, and this asserts it happened.
    for name in ("resistance", MISSING_FIELD):
        value = stored[name]
        assert value is not None
        assert value.value_min is not None and value.value_max is not None, name

    # And the one that did not clear the bar is reviewable in the sense that matters:
    # the datasheet line it was read from travels with it.
    assert stored["power_rating"] is None
    notes = _candidate_notes(db, part_id, "power_rating")
    assert any("Power rating 0.25 W" in note for note in notes), notes

    queue = client.get("/api/enrichment/candidates", params={"part_id": part_id})
    assert queue.status_code == 200, queue.text
    queued = {group["template_name"] for row in queue.json()["parts"] for group in row["fields"]}
    assert queued == {"power_rating"}, (
        "exactly the field that needed a human should be waiting for one"
    )

    # The part is now findable by what the pipeline learned about it, which is the whole
    # point of specifying it: a defined part no query can reach is not defined.
    found = client.post("/api/search/parts", json={"q": MPN, "limit": 5}).json()
    assert part_id in [row["id"] for row in found["results"]]


# ---------------------------------------------------------------------------
# Reads the assertions above lean on
# ---------------------------------------------------------------------------


def _entry(client: TestClient, intake_id: int) -> dict[str, Any]:
    """One intake row, through the route the PWA uses.

    Read over HTTP rather than off the session on purpose: the invariants about `mpn`,
    `status` and `resolved_part_id` are about what a client can observe, and a direct
    query would pass even if the read model stopped reporting one of them.
    """
    body = client.get("/api/intake/pending", params={"status": ["pending", "resolved"]})
    assert body.status_code == 200, body.text
    rows = [row for row in body.json()["entries"] if row["id"] == intake_id]
    assert len(rows) == 1, body.text
    entry: dict[str, Any] = rows[0]
    return entry


def _template(db: Session, name: str) -> ParameterTemplate | None:
    db.expire_all()
    return db.execute(
        select(ParameterTemplate).where(ParameterTemplate.name == name)
    ).scalar_one_or_none()


def _value(db: Session, part_id: int, template_name: str) -> ParameterValue | None:
    db.expire_all()
    return db.execute(
        select(ParameterValue)
        .join(ParameterTemplate, ParameterTemplate.id == ParameterValue.template_id)
        .where(ParameterValue.part_id == part_id, ParameterTemplate.name == template_name)
    ).scalar_one_or_none()


def _recorded_fields(db: Session, part_id: int) -> set[str]:
    """Every field this part has a candidate row for, promoted or pending alike."""
    db.expire_all()
    return set(
        db.execute(
            select(ParameterTemplate.name)
            .join(
                ParameterValueCandidate,
                ParameterValueCandidate.template_id == ParameterTemplate.id,
            )
            .where(ParameterValueCandidate.part_id == part_id)
        ).scalars()
    )


def _candidate_notes(db: Session, part_id: int, template_name: str) -> list[str]:
    db.expire_all()
    rows = db.execute(
        select(ParameterValueCandidate)
        .join(ParameterTemplate, ParameterTemplate.id == ParameterValueCandidate.template_id)
        .where(
            ParameterValueCandidate.part_id == part_id,
            ParameterValueCandidate.status == CandidateStatus.PENDING,
            ParameterTemplate.name == template_name,
        )
    ).scalars()
    return [row.note or "" for row in rows]
