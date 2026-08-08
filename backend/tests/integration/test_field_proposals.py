"""Proposed fields: the duplicate check, the routes, and the create path that is absent.

## What this file is really guarding

**A `parameter_template` created by something other than a person.** That is the one
thing this whole surface exists to prevent, and it is not prevented by a rule — it is
prevented by there being no call in the propose path that could do it. Three tests
below check that from three directions: a full worker pass leaves the template count
unchanged, `POST /{id}/decision` refuses `accepted` without a field that already
exists, and the modules' source is grepped for a writer.

**The duplicate check quietly stopping.** `nearest_existing_template` failing to find
`on_resistance` when `Rds(on)` is proposed raises nothing and looks like a clean run.
What it produces is two templates for one concept, and then parametric search reports
parts as having no on-resistance while their value sits under the other name — the
filter is a join on `template_id`. Nothing anywhere reports that. So the ladder has a
test per rung and the differently-spelled duplicate has its own.

**The seen-count inflating.** "How many datasheets said this" is the number a person
judges on, and a re-run that appended instead of upserting would turn one observation
into five. Two tests pin the two directions of the upsert.

**The quote becoming optional.** A proposal nobody can trace to a line of a datasheet
cannot be reviewed. Refused at the wire and in the service, and both are checked.

Everything here is offline: `FakeFieldDiscoveryProvider` replays a fixture through the
**real** parser, over real migrations and the real routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.documents import Document
from app.models.enums import (
    DocumentKind,
    FieldProposalMatch,
    SubstitutionDirection,
    ValueType,
)
from app.models.field_proposals import ParameterFieldProposal, ParameterFieldProposalEvidence
from app.models.parameter import ParameterTemplate
from app.scripts.propose_fields import HttpApiClient, run_once
from app.services import document_text, documents
from app.services.enrichment import cross_check
from app.services.enrichment.extract import (
    ExtractedField,
    ExtractedVariant,
    ExtractionResult,
)
from app.services.enrichment.field_discovery import FakeFieldDiscoveryProvider
from app.services.field_proposals import (
    EvidenceReport,
    FieldProposalError,
    nearest_existing_template,
    record_proposal,
    seen_count,
)
from tests import pdfs
from tests.factories import make_category, make_part

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "field_discovery" / "proposals.json"

MOSFET_SHEET = "sha256:mosfet-family-sheet"


def a_template(
    db: Session,
    name: str,
    *,
    display_name: str | None = None,
    value_type: ValueType = ValueType.NUMERIC,
    base_unit: str | None = "ohm",
) -> ParameterTemplate:
    template = ParameterTemplate(
        name=name,
        display_name=display_name or name.replace("_", " ").capitalize(),
        value_type=value_type,
        base_unit=base_unit,
        substitution_direction=SubstitutionDirection.RANGE_OVERLAP,
    )
    db.add(template)
    db.flush()
    return template


def template_count(db: Session) -> int:
    return int(db.execute(select(func.count()).select_from(ParameterTemplate)).scalar_one())


# ---------------------------------------------------------------------------
# The duplicate check — the safety mechanism
# ---------------------------------------------------------------------------


def test_a_differently_spelled_duplicate_comes_back_with_the_existing_field(
    db: Session,
) -> None:
    """`Rds(on)` proposed at an install that spells it `on_resistance`.

    **The case that earns the feature.** No normalisation connects a datasheet's
    symbol to an inventory's word — they share no stem and no spelling — so without
    the curated synonym list this returns `None`, the reviewer is offered "create
    `Rds(on)`", and the install ends up with two templates for one concept. Search
    then fragments silently: a MOSFET with 45 mΩ recorded under one name reads as
    having no on-resistance at all under the other, and the substitution rule
    decides on the half it can see.

    Verified by mutation: remove the `Rds(on)` group from `SYNONYM_GROUPS` and this
    fails.
    """
    existing = a_template(db, "on_resistance", display_name="On resistance")

    found = nearest_existing_template(db, "Rds(on)", base_unit="ohm")

    assert found is not None, "the duplicate check did not find the existing field"
    assert found.template.id == existing.id
    assert found.match is FieldProposalMatch.SYNONYM


def test_a_respelled_name_matches_exactly(db: Session) -> None:
    """`Voltage Rating` against `voltage_rating`: case and separators are noise."""
    existing = a_template(db, "voltage_rating", base_unit="volt")
    found = nearest_existing_template(db, "Voltage Rating", base_unit="volt")
    assert found is not None
    assert found.template.id == existing.id
    assert found.match is FieldProposalMatch.EXACT_NAME


def test_a_display_name_is_matched_too(db: Session) -> None:
    """One side spelled for a human is still the same concept."""
    existing = a_template(db, "esr_ohms", display_name="Equivalent series resistance")
    found = nearest_existing_template(db, "equivalent series resistance", base_unit="ohm")
    assert found is not None
    assert found.template.id == existing.id
    assert found.match is FieldProposalMatch.DISPLAY_NAME


def test_shared_words_and_a_compatible_unit_match_on_tokens(db: Session) -> None:
    existing = a_template(db, "channel_resistance", base_unit="ohm")
    found = nearest_existing_template(db, "channel_resistances", base_unit="ohm")
    assert found is not None
    assert found.template.id == existing.id
    assert found.match is FieldProposalMatch.TOKENS


def test_a_stated_unit_mismatch_blocks_a_token_match(db: Session) -> None:
    """Two fields measuring different quantities are two fields, whatever they share.

    The half of the check that is about `base_unit` rather than about names, and it
    matters as much: every `value_min`/`value_max` written against a template was
    computed under its quantity, so merging across one leaves numbers that keep
    answering range queries while meaning something else.
    """
    a_template(db, "channel_resistance", base_unit="ohm")
    assert nearest_existing_template(db, "channel_resistances", base_unit="farad") is None


def test_nothing_resembling_the_name_is_a_normal_answer(db: Session) -> None:
    """Most of what a datasheet states genuinely is a new field."""
    a_template(db, "capacitance", base_unit="farad")
    assert nearest_existing_template(db, "gate_charge", base_unit="coulomb") is None


def test_a_name_that_normalises_to_nothing_is_refused(db: Session) -> None:
    """An empty key is UNIQUE, so the first such name would claim it forever."""
    with pytest.raises(FieldProposalError) as error:
        nearest_existing_template(db, "---")
    assert error.value.reason == "empty_name"


# ---------------------------------------------------------------------------
# Recording, and the upsert in both directions
# ---------------------------------------------------------------------------


def test_the_same_field_from_five_documents_is_one_row_with_five_sightings(
    db: Session,
) -> None:
    """The count is the signal, so it has to be a count of documents.

    A heading in five unrelated sheets is a real field; the same heading five times
    from one sheet is one manufacturer's private vocabulary. Five proposal rows
    would put that distinction nowhere.
    """
    for index in range(5):
        record_proposal(
            db,
            name="on_resistance",
            evidence=[EvidenceReport(quote=f"Rds(on) ... {index}", document_ref=f"sha{index}")],
        )

    rows = list(db.execute(select(ParameterFieldProposal)).scalars())
    assert len(rows) == 1
    assert seen_count(db, rows[0]) == 5


def test_re_reading_one_document_does_not_inflate_the_count(db: Session) -> None:
    """The uniqueness key's whole purpose: a nightly re-run adds nothing.

    Verified by mutation: drop `document_ref` from
    `uq_field_proposal_evidence_observation` and the second pass appends.
    """
    for _ in range(3):
        record_proposal(
            db,
            name="on_resistance",
            evidence=[EvidenceReport(quote="Rds(on) 45 mOhm", document_ref="sha-one")],
        )
    proposal = db.execute(select(ParameterFieldProposal)).scalar_one()
    assert seen_count(db, proposal) == 1


def test_a_later_pass_refreshes_the_quote_rather_than_appending(db: Session) -> None:
    """A re-run under a better model is the case that matters, so the newest wins."""
    record_proposal(
        db, name="esr", evidence=[EvidenceReport(quote="first read", document_ref="sha-one")]
    )
    record_proposal(
        db,
        name="esr",
        evidence=[EvidenceReport(quote="corrected read", document_ref="sha-one")],
    )
    row = db.execute(select(ParameterFieldProposalEvidence)).scalar_one()
    assert row.quote == "corrected read"


def test_a_suggestion_is_filled_in_but_never_overwritten(db: Session) -> None:
    """The first source to read a table header usually knows the unit.

    A later pass that saw only the phrase must not be able to blank it out; a
    better suggestion is a person's edit in the create form.
    """
    record_proposal(
        db,
        name="on_resistance",
        suggested_base_unit="ohm",
        evidence=[EvidenceReport(quote="Rds(on)", document_ref="sha-one")],
    )
    record_proposal(
        db,
        name="on_resistance",
        suggested_base_unit=None,
        suggested_category="semiconductors",
        evidence=[EvidenceReport(quote="Rds(on)", document_ref="sha-two")],
    )
    proposal = db.execute(select(ParameterFieldProposal)).scalar_one()
    assert proposal.suggested_base_unit == "ohm"
    assert proposal.suggested_category == "semiconductors"


def test_an_empty_quote_is_refused_by_the_service(db: Session) -> None:
    """An empty quote defeats the requirement as thoroughly as a NULL would.

    Verified by mutation: delete the `if not quote` branch in `_record_evidence`
    and this fails — and so does the route test below, which is the point of having
    both.
    """
    with pytest.raises(FieldProposalError) as error:
        record_proposal(db, name="esr", evidence=[EvidenceReport(quote="   ")])
    assert error.value.reason == "empty_quote"


def test_a_proposal_with_no_evidence_at_all_is_refused(db: Session) -> None:
    with pytest.raises(FieldProposalError) as error:
        record_proposal(db, name="esr", evidence=[])
    assert error.value.reason == "no_evidence"


def test_recording_re_runs_the_match_so_a_new_template_is_picked_up(db: Session) -> None:
    """A stale "nothing resembles this" is worse than no answer: a reviewer trusts it."""
    record_proposal(
        db, name="Rds(on)", evidence=[EvidenceReport(quote="Rds(on)", document_ref="sha-one")]
    )
    proposal = db.execute(select(ParameterFieldProposal)).scalar_one()
    assert proposal.nearest_template_id is None

    existing = a_template(db, "on_resistance")
    record_proposal(
        db, name="Rds(on)", evidence=[EvidenceReport(quote="Rds(on)", document_ref="sha-two")]
    )
    db.refresh(proposal)
    assert proposal.nearest_template_id == existing.id


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def a_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "on_resistance",
        "display_name": "On resistance Rds(on)",
        "suggested_value_type": "numeric",
        "suggested_base_unit": "ohm",
        "evidence": [{"quote": "Rds(on) VGS = 10 V ... 45 mOhm", "document_ref": "sha-one"}],
    }
    body.update(overrides)
    return body


def test_proposing_over_http_stores_the_quote_and_the_count(client: TestClient) -> None:
    response = client.post("/api/field-proposals", json=a_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["seen_count"] == 1
    assert body["evidence"][0]["quote"].startswith("Rds(on)")


def test_the_route_refuses_a_proposal_with_no_quote(client: TestClient) -> None:
    response = client.post(
        "/api/field-proposals", json=a_body(evidence=[{"document_ref": "sha-one"}])
    )
    assert response.status_code == 422, response.text


def test_the_route_refuses_a_blank_quote(client: TestClient) -> None:
    response = client.post(
        "/api/field-proposals", json=a_body(evidence=[{"quote": "   ", "document_ref": "x"}])
    )
    assert response.status_code == 422, response.text


def test_the_list_puts_the_likeliest_duplicates_first(client: TestClient, db: Session) -> None:
    """Because the rows that must not be waved through are the dangerous ones.

    A proposal with no near match is genuinely new information and can wait; one
    that already has a field is a duplicate about to be created.
    """
    a_template(db, "on_resistance")
    db.commit()
    client.post("/api/field-proposals", json=a_body(name="gate_charge", suggested_base_unit=None))
    client.post("/api/field-proposals", json=a_body(name="Rds(on)"))

    rows = client.get("/api/field-proposals").json()["proposals"]
    assert [row["name"] for row in rows] == ["Rds(on)", "gate_charge"]
    assert rows[0]["nearest"]["name"] == "on_resistance"
    assert rows[0]["nearest"]["match"] == "synonym"
    assert rows[1]["nearest"] is None


def test_proposing_creates_no_parameter_template(client: TestClient, db: Session) -> None:
    """The invariant the whole feature is shaped around, checked at the route.

    Verified by mutation: call `parameter_fields.create_template` from
    `propose_field` and this fails.
    """
    before = template_count(db)
    assert client.post("/api/field-proposals", json=a_body()).status_code == 201
    assert template_count(db) == before


def test_accepting_without_a_field_that_already_exists_is_refused(client: TestClient) -> None:
    """Refused rather than treated as "create it for me".

    That branch is the one a later edit would grow into a second creation path, so
    its absence is asserted rather than described.
    """
    proposal_id = client.post("/api/field-proposals", json=a_body()).json()["id"]
    response = client.post(
        f"/api/field-proposals/{proposal_id}/decision", json={"status": "accepted"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "missing_field_id"


def test_accepting_a_field_that_does_not_exist_is_a_404(client: TestClient) -> None:
    proposal_id = client.post("/api/field-proposals", json=a_body()).json()["id"]
    response = client.post(
        f"/api/field-proposals/{proposal_id}/decision",
        json={"status": "accepted", "field_id": 99_999},
    )
    assert response.status_code == 404, response.text


def test_the_accept_flow_goes_through_the_existing_authoring_route(client: TestClient) -> None:
    """One door onto `parameter_template`, and the proposal only records the outcome.

    The same shape the intake queue keeps: a chosen identity becomes real through
    the `resolve` route that already existed, not through a second one on the
    queue's side.
    """
    proposal_id = client.post("/api/field-proposals", json=a_body()).json()["id"]

    created = client.post(
        "/api/parameter-fields",
        json={
            "name": "on_resistance",
            "display_name": "On resistance",
            "value_type": "numeric",
            "base_unit": "ohm",
            "substitution_direction": "lower_ok",
        },
    )
    assert created.status_code == 201, created.text
    field_id = created.json()["field"]["id"]

    decided = client.post(
        f"/api/field-proposals/{proposal_id}/decision",
        json={"status": "accepted", "field_id": field_id, "note": "yes, MOSFETs need this"},
    )
    assert decided.status_code == 200, decided.text
    proposal = decided.json()["proposal"]
    assert proposal["status"] == "accepted"
    assert proposal["created_template_id"] == field_id
    assert proposal["decided_at"] is not None


def test_dismissing_keeps_the_row(client: TestClient) -> None:
    """So the same phrase does not come back to the top of the list as though new."""
    proposal_id = client.post("/api/field-proposals", json=a_body()).json()["id"]
    decided = client.post(
        f"/api/field-proposals/{proposal_id}/decision",
        json={"status": "dismissed", "note": "manufacturer's own jargon"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["proposal"]["status"] == "dismissed"
    assert client.get(f"/api/field-proposals/{proposal_id}").status_code == 200
    assert client.get("/api/field-proposals").json()["proposals"] == []


def test_a_dismissal_naming_a_field_is_refused(client: TestClient) -> None:
    proposal_id = client.post("/api/field-proposals", json=a_body()).json()["id"]
    response = client.post(
        f"/api/field-proposals/{proposal_id}/decision",
        json={"status": "dismissed", "field_id": 1},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "field_id_on_dismissal"


def test_pending_is_not_a_decision(client: TestClient) -> None:
    proposal_id = client.post("/api/field-proposals", json=a_body()).json()["id"]
    response = client.post(
        f"/api/field-proposals/{proposal_id}/decision", json={"status": "pending"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "not_a_decision"


# ---------------------------------------------------------------------------
# The signal that already existed and was thrown away
# ---------------------------------------------------------------------------


def test_cross_check_keeps_the_evidence_for_a_field_it_has_no_template_for(
    db: Session,
) -> None:
    """`IngestReport.unknown_templates` used to be computed and dropped.

    What went with it was the useful half: which datasheet, which part, the value,
    and the line. This is the enabling step of the whole feature and it needs no
    model at all.
    """
    category = make_category(db, "Semiconductors")
    part = make_part(db, "IRLZ44N", mpn="IRLZ44N", category_id=category.id)
    db.flush()

    report = cross_check.ingest(
        db,
        ExtractionResult(
            provider="fake",
            model="hand-authored",
            document_ref="sha256:irlz44n",
            variants=(
                ExtractedVariant(
                    mpn="IRLZ44N",
                    mpn_source_text="IRLZ44N HEXFET Power MOSFET",
                    fields=(
                        ExtractedField(
                            template_name="on_resistance",
                            raw_value="22 mOhm",
                            confidence=0.9,
                            source_text="RDS(on) Static Drain-to-Source On-Resistance 0.022 Ohm",
                            page=2,
                        ),
                    ),
                ),
            ),
        ),
    )

    assert report.unknown_templates == ("on_resistance",)
    proposal = db.execute(select(ParameterFieldProposal)).scalar_one()
    assert proposal.name == "on_resistance"
    assert proposal.suggested_category == category.slug
    evidence = db.execute(select(ParameterFieldProposalEvidence)).scalar_one()
    assert evidence.document_ref == "sha256:irlz44n"
    assert evidence.part_id == part.id
    assert evidence.stated_value == "22 mOhm"
    assert "RDS(on)" in evidence.quote


def test_cross_check_still_creates_no_template(db: Session) -> None:
    """Recording a proposal is not creating a field, and the count says so."""
    make_part(db, "IRLZ44N", mpn="IRLZ44N")
    db.flush()
    before = template_count(db)
    cross_check.ingest(
        db,
        ExtractionResult(
            provider="fake",
            model="hand-authored",
            document_ref="sha256:irlz44n",
            variants=(
                ExtractedVariant(
                    mpn="IRLZ44N",
                    mpn_source_text="IRLZ44N",
                    fields=(
                        ExtractedField(
                            template_name="on_resistance",
                            raw_value="22 mOhm",
                            confidence=0.9,
                            source_text="RDS(on) ... 0.022 Ohm",
                        ),
                    ),
                ),
            ),
        ),
    )
    assert template_count(db) == before


# ---------------------------------------------------------------------------
# The worker pass, end to end and offline
# ---------------------------------------------------------------------------


class _TestClientApi(HttpApiClient):
    """`HttpApiClient` with `urllib` swapped for `TestClient`.

    A subclass rather than a second implementation, exactly as
    `test_dispatch_worker` does it: the URL paths, the JSON bodies and the response
    parsing are the ones the real worker uses, so a wrong path or a renamed field
    fails here. A hand-rolled client satisfying the Protocol would test the test.
    """

    def __init__(self, client: TestClient) -> None:
        super().__init__("http://testserver")
        self._client = client

    def _get_json(self, path: str) -> Any:
        response = self._client.get(f"/{path}")
        assert response.status_code == 200, f"GET {path} -> {response.status_code}: {response.text}"
        return response.json()

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"/{path}", json=payload)
        if response.status_code >= 400:
            raise AssertionError(f"POST {path} -> {response.status_code}: {response.text}")
        decoded: Any = response.json() if response.content else {}
        return decoded if isinstance(decoded, dict) else {}


class _Keyed(_TestClientApi):
    """`_TestClientApi` serving the recorded sheet's text under the fixture's own key.

    The fixture is keyed on `document_ref`, so a pass over a *stored* document would
    be a `FieldDiscoveryFixtureMiss` — which is the fake behaving correctly and would
    make these tests assert nothing. Only the text lookup is stubbed; every write and
    every vocabulary read still goes through the API.
    """

    def document_text(self, sha256: str) -> str | None:
        return "Static drain-source on-resistance Rds(on) VGS = 10 V, ID = 8 A ... 45 mOhm"


def _sheet_with_text(db: Session, text: str) -> Document:
    stored = documents.store_document(
        db,
        data=pdfs.with_text(["filler"]),
        media_type="application/pdf",
        kind=DocumentKind.DATASHEET,
    )
    db.flush()
    document_text.record_text(db, document=stored.document, extractor="pypdf", pages=[text])
    db.commit()
    return stored.document


def test_a_whole_pass_proposes_and_creates_nothing(client: TestClient, db: Session) -> None:
    """The worker's own guarantee, through the real routes and the real parser.

    The fixture is keyed on its own `document_ref`, so `_Keyed` serves the recorded
    sheet's text under that key while everything else — the vocabulary reads, the
    submissions, the dedup check — goes through the real API.

    Verified by mutation: have `process_one` post to `/api/parameter-fields` and the
    count assertion fails.
    """
    make_category(db, "Semiconductors")
    db.commit()
    before = template_count(db)

    results = run_once(_Keyed(client), FakeFieldDiscoveryProvider(FIXTURE), [MOSFET_SHEET])

    assert results[0].skipped_reason is None
    assert results[0].submitted == 2
    assert template_count(db) == before, "the propose path created a parameter_template"


def test_the_pass_submits_the_quote_and_the_document_it_came_from(
    client: TestClient, db: Session
) -> None:
    """Every proposal arrives reviewable: a quote, a document, and pending."""
    make_category(db, "Semiconductors")
    db.commit()

    run_once(_Keyed(client), FakeFieldDiscoveryProvider(FIXTURE), [MOSFET_SHEET])

    proposals = client.get("/api/field-proposals").json()["proposals"]
    assert {row["name"] for row in proposals} == {"on_resistance", "gate_threshold_voltage"}
    assert all(row["evidence"][0]["quote"] for row in proposals)
    assert all(row["evidence"][0]["document_ref"] == MOSFET_SHEET for row in proposals)
    assert all(row["status"] == "pending" for row in proposals)
    assert all(row["seen_count"] == 1 for row in proposals)


def test_a_second_pass_over_one_sheet_adds_no_sighting(client: TestClient, db: Session) -> None:
    """The upsert, end to end. A nightly re-run must not inflate the count."""
    make_category(db, "Semiconductors")
    db.commit()

    for _ in range(2):
        run_once(_Keyed(client), FakeFieldDiscoveryProvider(FIXTURE), [MOSFET_SHEET])

    proposals = client.get("/api/field-proposals").json()["proposals"]
    assert len(proposals) == 2
    assert all(row["seen_count"] == 1 for row in proposals)


def test_a_document_with_no_extracted_text_is_skipped_not_failed(
    client: TestClient, db: Session
) -> None:
    """The ordinary state of a fresh upload, and the extraction queue's business.

    Exactly the `EXHAUSTED`-is-not-`FAILED` distinction the other queues draw:
    calling this a failure would put "nobody has read the PDF yet" in the same
    bucket as "something is broken".
    """
    stored = documents.store_document(
        db,
        data=pdfs.with_text(["never extracted"]),
        media_type="application/pdf",
        kind=DocumentKind.DATASHEET,
    )
    db.commit()

    results = run_once(
        _TestClientApi(client), FakeFieldDiscoveryProvider(FIXTURE), [stored.document.sha256]
    )
    assert results[0].skipped_reason == "no_text"
    assert results[0].refused == 0


# ---------------------------------------------------------------------------
# Absence, read off the source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "app/services/field_proposals.py",
        "app/api/routes/field_proposals.py",
        "app/services/enrichment/field_discovery.py",
        "app/scripts/propose_fields.py",
    ],
)
def test_no_module_in_the_propose_path_can_create_a_field(module: str) -> None:
    """Read off the source, because absence is the guarantee.

    Every other test here checks what the propose path *does*. This checks that
    none of it so much as mentions the two ways a `parameter_template` gets made —
    which is what stops a future edit from adding a convenient shortcut and leaving
    every behavioural test above still passing. `parameter_fields.py` and
    `POST /api/parameter-fields` are the one door, and they stay it.
    """
    source = (Path(__file__).resolve().parents[2] / module).read_text(encoding="utf-8")
    lines = [
        line
        for line in source.splitlines()
        if ("create_template(" in line or "ParameterTemplate(" in line)
        # Prose about the rule is the point of the rule; a call is not.
        and not line.lstrip().startswith(("#", "*", '"', "'"))
    ]
    assert not lines, f"{module} can create a parameter_template: {lines}"
