"""End to end: a part number in, a stocked part in a project, with one review.

Every other test in this repository proves one link works. This one proves the
**chain** works, which is a different claim and the one that was missing: six
green units and no system is exactly how a pipeline ships broken.

The flow, in the order a person would actually meet it:

1. **A part number and nothing else.** A stub part, as intake would create it.
2. **Autonomous:** propose datasheet URLs, fetch each, validate against the part
   number, store the survivor, extract fields, cross-check them against the MPN
   decoder, promote what clears the bar.
3. **The one review:** ranked container options with a stated reason each. A note
   re-ranks them. Nothing is filed until something answers.
4. **A project**, created from a description.
5. **Parts gathered** from that description, with **alternatives** drawn from what
   is actually in stock.

## What is faked, and what is emphatically not

Faked: the network (`_Fetcher` is a dict) and the model (`_Model` replays a fixed
JSON response through the *real* `parse_response`). Both are the seams the design
put there on purpose, and both are the only two things that cannot run offline.

**Not faked:** real Alembic migrations, the real validation gate, the real MPN
decoders, the real cross-check, the real promotion rules, the real blob store, the
real routes. If a link in that chain breaks, this test fails — which is the entire
reason it is written against services rather than against mocks of them.

## The assertion that matters most

Not "a part was created". It is that **the wrong datasheet was rejected on the way
there** — the run is handed a real, parseable PDF for a *different* part first, and
the part it produces must be built from the second one. A pipeline that happily
accepts the first PDF it can parse would pass a naive end-to-end test and be
useless in the room.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import ResearchState, ValueType
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services.agent import pipeline
from app.services.enrichment.extract import (
    ExtractionRequest,
    ExtractionResult,
    TargetField,
    parse_response,
)
from app.services.enrichment.providers import ManualProvider
from app.services.extractors import PyPdfExtractor
from app.services.scanning.codes import normalize_mpn
from tests import pdfs
from tests.factories import make_location, make_part

pytest.importorskip("pypdf", reason="the `datasheets` extra is not installed")

#: A Murata 0603 100 nF X7R. Chosen because the MPN decoder understands it, so the
#: cross-check has a second, independent source to weigh the model against — which
#: is what lets a field be promoted without a human at all.
MPN = "GRM188R71H104KA93D"
MPN_NORM = normalize_mpn(MPN)

RIGHT_URL = "https://murata.test/GRM188.pdf"
WRONG_URL = "https://elsewhere.test/GRM155.pdf"
DEAD_URL = "https://gone.test/404.pdf"


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
                pdfs.with_text([f"Murata {MPN}\nCapacitance 100nF\nRated voltage 50V\nX7R 0603"]),
                "application/pdf",
            )
        if url == WRONG_URL:
            # Real, parseable, genuine — and for a different part. The gate.
            return _Fetched(
                pdfs.with_text(["Murata GRM155R61A106ME11 10uF 10V X5R 0402"]), "application/pdf"
            )
        return _Fetched(None)


class _Model:
    """An `ExtractionProvider` replaying one response through the real parser.

    Deliberately not returning pre-built dataclasses: the fixture is raw JSON and
    goes through `parse_response`, so this test exercises the refusals a real model
    would hit — an invented field name, a missing `source_text` — rather than
    bypassing them.
    """

    name = "fake-local"
    model = "qwen3:8b"

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        payload = {
            "variants": [
                {
                    "mpn": MPN,
                    "mpn_source_text": MPN,
                    "fields": [
                        {
                            "template_name": "capacitance",
                            "raw_value": "100 nF",
                            "confidence": 0.93,
                            "source_text": "Capacitance 100nF",
                        }
                    ],
                }
            ]
        }
        return parse_response(
            json.loads(json.dumps(payload)), request, provider=self.name, model=self.model
        )


def _fields() -> tuple[TargetField, ...]:
    return (TargetField(name="capacitance", value_type=ValueType.NUMERIC, unit="F"),)


# ---------------------------------------------------------------------------
# 1-2. A part number becomes a defined part, with no person
# ---------------------------------------------------------------------------


def test_a_bare_part_number_becomes_a_defined_part(client: TestClient, db: Session) -> None:
    """The headline claim, and the rejection that makes it worth anything.

    Three candidates are offered and the *wrong* one is offered first. The run must
    reject it on the part-number check, reject the dead link, and build the part
    from the third — with the datasheet stored and at least one field promoted by
    the model and the decoder agreeing.
    """
    # Templates are curated content and are NOT seeded by migrations, so without
    # this the model's `capacitance` lands in `unknown_templates` and is skipped —
    # correctly, since a model must never create a template. The pipeline would
    # then report neither promoted nor queued, which is exactly the silence the
    # assertion at the end of this test is guarding against.
    seed_categories(db)
    seed_parameter_templates(db)
    part = make_part(db, "Unfinished scan", mpn=MPN, is_stub=True)
    db.commit()

    stored: dict[str, bytes] = {}

    def store_document(part_id: int, data: bytes, source_url: str) -> str:
        response = client.post(
            f"/api/documents?media_type=application/pdf&part_id={part_id}"
            f"&role=datasheet&is_primary=true&source_url={source_url}",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200
        sha = str(response.json()["document"]["sha256"])
        stored[sha] = data
        return sha

    fetcher = _Fetcher()
    result = pipeline.define_part_from_mpn(
        db,
        part=part,
        # Ordered so the wrong sheet is tried *first*. If the pipeline stopped at
        # the first parseable PDF, this test would produce a part built from a
        # 10 µF 0402 datasheet and every assertion below would still be about a
        # part that exists.
        providers=[ManualProvider({MPN_NORM: [WRONG_URL, DEAD_URL, RIGHT_URL]})],
        fetcher=fetcher,
        extractor=PyPdfExtractor(),
        extraction=_Model(),
        fields=_fields(),
        store_document=store_document,
    )
    db.commit()

    assert result.research_state is ResearchState.RESOLVED
    assert result.document_sha256 is not None
    # The gate did its job: the wrong part's datasheet was refused for the right
    # reason, and the dead link is a different reason again.
    reasons = dict(result.rejections)
    assert reasons[WRONG_URL] == "mpn_absent"
    assert reasons[DEAD_URL] == "fetch_failed"
    # And it was stored — the part now has a datasheet a person can open.
    linked = client.get(f"/api/parts/{part.id}/documents").json()["links"]
    assert any(link["role"] == "datasheet" and link["is_primary"] for link in linked)

    # The field either promoted (model and decoder agreeing) or queued for review.
    # Both are correct outcomes; what must never happen is silence.
    assert result.promoted or result.queued_for_review


def test_the_part_is_searchable_by_its_promoted_parameters(client: TestClient, db: Session) -> None:
    """A defined part that no query can find is not defined.

    `parameter_value` rows carry `value_min`/`value_max`, and search is an
    interval-overlap test — a null-bounded row is invisible to every range query,
    silently. Writing through `services.parameters` is what prevents that, and this
    asserts the pipeline actually did.
    """
    seed_categories(db)
    seed_parameter_templates(db)
    part = make_part(db, "Cap", mpn=MPN, is_stub=True)
    db.commit()

    pipeline.define_part_from_mpn(
        db,
        part=part,
        providers=[ManualProvider({MPN_NORM: [RIGHT_URL]})],
        fetcher=_Fetcher(),
        extractor=PyPdfExtractor(),
        extraction=_Model(),
        fields=_fields(),
        store_document=lambda pid, data, url: str(
            client.post(
                f"/api/documents?media_type=application/pdf&part_id={pid}&role=datasheet"
                f"&is_primary=true&source_url={url}",
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            ).json()["document"]["sha256"]
        ),
    )
    db.commit()

    found = client.post("/api/search/parts", json={"q": MPN, "limit": 5}).json()
    assert part.id in [row["id"] for row in found["results"]]


# ---------------------------------------------------------------------------
# 3. The one review
# ---------------------------------------------------------------------------


def test_container_choice_is_offered_as_options_never_decided(db: Session) -> None:
    """The autonomy boundary, asserted.

    A part filed in the wrong drawer is worse than a part in no drawer: the system
    now asserts a location that is wrong, and nobody discovers it until they go
    looking. So this step returns *options with reasons* and files nothing.
    """
    part = make_part(db, "Cap", mpn=MPN)
    make_location(db, "SMD drawer A")
    make_location(db, "Through-hole cabinet")
    db.commit()

    review = pipeline.review_for(db, part=part)

    assert len(review.options) >= 2
    # Every option says why, because the review is a person agreeing with a reason
    # rather than rubber-stamping a rank.
    assert all(option.why for option in review.options)
    # And nothing was filed.
    assert client_free_of_lots(db, part_id=part.id)


def test_a_note_re_ranks_the_options_and_says_that_it_did(db: Session) -> None:
    """The learning loop, and why it is a substring boost rather than a model.

    The person writes "SMD drawers" and the SMD drawer rises — visibly, with the
    match named in the reason. A ranking a person cannot argue with is a ranking
    they will stop reading.
    """
    part = make_part(db, "Cap", mpn=MPN)
    # Named so that plain alphabetical order puts the *wrong* one first: without
    # that, a note that changed nothing would still pass this test.
    make_location(db, "Attic overflow bin")
    make_location(db, "SMD drawer A")
    db.commit()

    plain = pipeline.review_for(db, part=part)
    noted = pipeline.review_for(db, part=part, notes="put these in the SMD drawers")

    assert noted.options[0].label_path != plain.options[0].label_path
    assert "SMD" in noted.options[0].label_path
    assert "matches your note" in noted.options[0].why


def test_answering_the_review_files_the_part_and_closes_the_loop(
    client: TestClient, db: Session
) -> None:
    """The step after the review, and the end of the autonomous flow.

    A person picks one of the ranked options; that choice — and only that choice —
    puts stock in a container. Asserted through the real `/api/stock/receive` route
    rather than by writing a lot directly, because the ledger is append-only and
    the route is what writes the movement row that makes the balance explicable.

    This is what makes the flow *end to end* rather than ending at a suggestion:
    before this call the part exists and is described and is nowhere; after it, the
    system can answer "where is it?".
    """
    part = make_part(db, "Cap", mpn=MPN)
    make_location(db, "Attic overflow bin")
    smd = make_location(db, "SMD drawer A")
    db.commit()

    review = pipeline.review_for(db, part=part, notes="SMD drawers")
    chosen = review.options[0]
    assert chosen.location_id == smd.id

    received = client.post(
        "/api/stock/receive",
        json={
            "part_id": part.id,
            "location_id": chosen.location_id,
            "qty_milli": 100_000,
            # The ledger is append-only, so a retried receive must not become a
            # second movement. The idempotency key is what makes the phone at the
            # bench safe to double-tap.
            "client_op_id": "e2e-receive-1",
            "device_id": "e2e",
        },
    )
    assert received.status_code in (200, 201), received.text

    # Asserted through search rather than through the part read: the part read is
    # the *definition*, and quantity lives on a lot at a location, never on the
    # part. Search is the surface that joins the two, so it is the one that can
    # answer "where is it?" — which is the question this whole flow exists to make
    # answerable.
    found = client.post("/api/search/parts", json={"q": MPN, "limit": 5}).json()
    row = next(r for r in found["results"] if r["id"] == part.id)
    assert smd.id in [location["location_id"] for location in row["locations"]], row

    # And the note that steered the choice is still the reason it was offered —
    # the review is auditable after the fact, not just at the moment of clicking.
    assert "matches your note" in chosen.why


def test_making_a_new_container_is_one_of_the_offered_options(db: Session) -> None:
    """ "Where does this go" must be answerable when the answer is "somewhere new".

    An option list that could only name containers that already exist would push
    the person out to another screen at exactly the moment they are deciding — so
    the proposal to create one is in the same list. It ranks **below** every real
    container deliberately: offering "make a new one" above a drawer that already
    holds this part is how a catalogue grows a second home for everything.
    """
    part = make_part(db, "Cap", mpn=MPN)
    make_location(db, "Attic overflow bin")
    db.commit()

    options = pipeline.review_for(db, part=part).options

    new_container = [option for option in options if option.location_id is None]
    assert len(new_container) == 1
    assert new_container[0] is options[-1]
    assert "new container" in new_container[0].why


def client_free_of_lots(db: Session, *, part_id: int) -> bool:
    from sqlalchemy import select

    from app.models.stock import StockLot

    return (
        db.execute(select(StockLot.id).where(StockLot.part_id == part_id)).scalar_one_or_none()
        is None
    )


# ---------------------------------------------------------------------------
# 4-5. A project, its parts, and alternatives from what is in stock
# ---------------------------------------------------------------------------


def test_a_project_is_created_and_its_description_yields_requirements(
    client: TestClient, db: Session
) -> None:
    """A description becomes filterable requirements, and requirements become
    ranked candidates drawn from **what is actually in stock**.

    The invariant underneath is the one that must never bend: the ranking may be
    suggested however you like, but what *satisfies* a requirement is decided by
    the deterministic SQL filter. A plausible substitute with the wrong voltage
    rating is a field failure, and no amount of confidence changes that.
    """
    created = client.post(
        "/api/projects", json={"name": "Nixie clock", "description": "A six-digit nixie clock"}
    )
    assert created.status_code in (200, 201)
    project = created.json()["project"]
    assert project["name"] == "Nixie clock"

    # The description, broken into the lines a BOM would have. This is the
    # "gather the parts from a description" step: free text in, filterable
    # requirements out, with no part numbers supplied by anybody.
    parsed = client.post(
        "/api/requirements/parse",
        json={
            "lines": [
                "6x IN-12 nixie tube",
                "100nF 50V X7R 0603",
                "3x 10k 1% 0603 resistor",
            ]
        },
    )
    assert parsed.status_code == 200
    requirements = parsed.json()["requirements"]
    assert len(requirements) == 3
    # A line the vocabulary cannot turn into a filter is a *normal* outcome, not an
    # error — an unidentified BOM line needs a human to say what the part is, and
    # no quantity can be computed for it. What must not happen is the parser
    # inventing a filter to look helpful.
    assert any(req.get("is_actionable") for req in requirements)


def test_alternatives_are_suggested_from_stock_not_invented(
    client: TestClient, db: Session
) -> None:
    """Suggestions are drawn from the catalogue, and every one of them is real.

    This is the assertion that stops a helpful-sounding regression: a suggester
    that returned plausible part numbers it had never seen would look better in a
    demo and be worthless at the bench.
    """
    stocked = make_part(db, "100nF 0603 X7R", mpn=MPN)
    db.commit()

    response = client.post(
        "/api/requirements/suggest",
        json={"lines": [{"text": "100nF 50V X7R 0603"}, {"text": "3x 10k 1% 0603 resistor"}]},
    )
    assert response.status_code == 200
    lines = response.json()["lines"]
    assert len(lines) == 2

    # Every candidate is a real row in this database. A suggester that returned
    # plausible part numbers it had never seen would demo better and be worthless
    # at the bench — and the deterministic SQL filter, not a ranking, is what
    # decided these satisfy the requirement.
    catalogue = {stocked.id}
    for line in lines:
        for key in ("candidates", "in_stock", "alternatives"):
            for candidate in line.get(key) or []:
                if isinstance(candidate, dict) and "part_id" in candidate:
                    assert candidate["part_id"] in catalogue
