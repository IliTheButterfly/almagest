"""`/api/enrichment/candidates` — the review queue screen's routes.

The screen's whole reason for existing is that `app.services.enrichment
.candidates` refuses to guess. So this suite is biased toward the same failure
mode as `test_candidates.py`: a wrong write here is silent everywhere else,
so it asserts on refusals and on evidence surviving the trip through the wire
at least as hard as on the happy path.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.catalog import Part
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import CandidateStatus, Provenance
from app.models.parameter import ParameterTemplate, ParameterValue
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services.enrichment import candidates
from tests.factories import make_part


def _session() -> Session:
    return get_session_factory()()


def _seeded_part(name: str = "review test part") -> tuple[Session, Part]:
    """A fresh session, seeded and holding one part — the shape
    `test_search_api.py` uses so writes made here are visible through `client`,
    which reads the same underlying database file via its own session."""
    session = _session()
    seed_categories(session)
    seed_parameter_templates(session)
    part = make_part(session, name=name)
    session.commit()
    return session, part


def _template(session: Session, name: str) -> ParameterTemplate:
    return session.execute(
        select(ParameterTemplate).where(ParameterTemplate.name == name)
    ).scalar_one()


def _stored_value(session: Session, part: Part, template_name: str) -> ParameterValue | None:
    template = _template(session, template_name)
    return session.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id, ParameterValue.template_id == template.id
        )
    ).scalar_one_or_none()


def test_queue_groups_by_part_and_shows_the_source_and_evidence(client: TestClient) -> None:
    session, part = _seeded_part()
    candidates.submit(
        session,
        part,
        _template(session, "capacitance"),
        "100nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.6,  # below the auto-promote bar, so it stays queued
        source_ref="sha256:" + "0" * 64,
        note='vendor.model read "100nF ±10%, page 5" (self-reported confidence 0.60)',
    )
    session.commit()
    session.close()

    body = client.get("/api/enrichment/candidates").json()
    assert body["total_candidates"] == 1
    assert body["total_parts"] == 1
    [group] = body["parts"]
    assert group["part_id"] == part.id
    assert group["part_name"] == "review test part"
    [field] = group["fields"]
    assert field["template_name"] == "capacitance"
    [item] = field["candidates"]
    assert item["source"] == "llm_inferred"
    assert item["source_ref"] == "sha256:" + "0" * 64
    # The quoted line is the whole reason this row is reviewable rather than a
    # bare confidence score to take on faith.
    assert "100nF ±10%, page 5" in item["note"]
    assert item["requires_human"] is False


def test_disagreement_shows_both_values_and_names_the_priority_orders_pick(
    client: TestClient,
) -> None:
    session, part = _seeded_part()
    template = _template(session, "capacitance")
    candidates.record(
        session,
        part,
        template,
        "4.7uF",
        source=Provenance.MPN_DECODER,
        confidence=0.9,
    )
    candidates.record(
        session,
        part,
        template,
        "10uF",
        source=Provenance.LLM_INFERRED,
        confidence=0.94,
    )
    candidates.evaluate(session, part, template)
    session.commit()
    session.close()

    body = client.get("/api/enrichment/candidates").json()
    [group] = body["parts"]
    [field] = group["fields"]
    assert field["existing_raw_input"] is None
    values = {row["source"]: row["raw_value"] for row in field["candidates"]}
    assert values == {"mpn_decoder": "4.7uF", "llm_inferred": "10uF"}
    # mpn_decoder outranks llm_inferred in PROVENANCE_PRIORITY, whatever the
    # confidences say (0.94 > 0.9 here, and the decoder still wins).
    recommended_id = field["recommended_candidate_id"]
    winner = next(row for row in field["candidates"] if row["id"] == recommended_id)
    assert winner["source"] == "mpn_decoder"


def test_accept_promotes_with_the_sources_own_provenance_and_closes_the_field(
    client: TestClient,
) -> None:
    session, part = _seeded_part()
    template = _template(session, "capacitance")
    candidates.record(
        session, part, template, "4.7uF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    candidates.record(
        session, part, template, "10uF", source=Provenance.LLM_INFERRED, confidence=0.94
    )
    candidates.evaluate(session, part, template)
    session.commit()
    winner_id = next(
        row.id for row in candidates.pending(session, part=part) if row.source == "mpn_decoder"
    )
    loser_id = next(
        row.id for row in candidates.pending(session, part=part) if row.source == "llm_inferred"
    )
    session.close()

    response = client.post(f"/api/enrichment/candidates/{winner_id}/accept")
    assert response.status_code == 200, response.text
    field = response.json()
    assert field["candidates"] == []  # both rows closed: winner promoted, loser dismissed

    check = _session()
    value = _stored_value(check, part, "capacitance")
    assert value is not None
    assert value.provenance == Provenance.MPN_DECODER  # accept keeps the source's own provenance
    assert value.value_nominal == 4.7e-6

    loser_row = check.get(ParameterValueCandidate, loser_id)
    assert loser_row is not None
    assert loser_row.status == CandidateStatus.DISMISSED  # not hidden, just closed
    check.close()


def test_accepting_an_unparseable_candidate_is_refused(client: TestClient) -> None:
    session, part = _seeded_part()
    template = _template(session, "capacitance")
    row = candidates.record(
        session,
        part,
        template,
        "not-a-value",
        source=Provenance.DISTRIBUTOR_FREETEXT,
        confidence=0.9,
    )
    session.commit()
    row_id = row.id
    session.close()

    response = client.post(f"/api/enrichment/candidates/{row_id}/accept")
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unparseable"


def test_correct_records_a_manual_candidate_that_outranks_everything(client: TestClient) -> None:
    session, part = _seeded_part()
    template = _template(session, "capacitance")
    wrong = candidates.record(
        session, part, template, "10uF", source=Provenance.LLM_INFERRED, confidence=0.85
    )
    candidates.evaluate(session, part, template)  # promotes the lone confident reading
    session.commit()
    wrong_id = wrong.id
    session.close()

    response = client.post(
        f"/api/enrichment/candidates/{wrong_id}/correct",
        json={"raw_value": "4.7uF", "note": "checked the physical part, it is a 475 marking"},
    )
    assert response.status_code == 200, response.text
    field = response.json()
    assert field["existing_raw_input"] == "4.7uF"
    assert field["existing_provenance"] == "manual"

    check = _session()
    value = _stored_value(check, part, "capacitance")
    assert value is not None
    assert value.provenance == Provenance.MANUAL
    assert value.value_nominal == 4.7e-6

    original = check.get(ParameterValueCandidate, wrong_id)
    assert original is not None
    # The llm_inferred row is superseded by evaluate() at write time (its own
    # value agreed with itself when it was promoted); either way it must not
    # still be pending once a human has corrected the field.
    assert original.status != CandidateStatus.PENDING
    check.close()


def test_dismiss_closes_one_row_without_touching_its_siblings(client: TestClient) -> None:
    session, part = _seeded_part()
    template = _template(session, "capacitance")
    candidates.record(
        session, part, template, "4.7uF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    other = candidates.record(
        session, part, template, "10uF", source=Provenance.LLM_INFERRED, confidence=0.94
    )
    candidates.evaluate(session, part, template)
    session.commit()
    other_id = other.id
    session.close()

    response = client.post(f"/api/enrichment/candidates/{other_id}/dismiss")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "dismissed"

    body = client.get("/api/enrichment/candidates").json()
    [group] = body["parts"]
    [field] = group["fields"]
    # The sibling (mpn_decoder) is untouched and still pending — dismissing one
    # row must not resolve the field by itself.
    assert [row["source"] for row in field["candidates"]] == ["mpn_decoder"]


def test_dismissing_an_already_dismissed_candidate_is_a_conflict(client: TestClient) -> None:
    session, part = _seeded_part()
    template = _template(session, "capacitance")
    row = candidates.record(
        session, part, template, "10uF", source=Provenance.LLM_INFERRED, confidence=0.5
    )
    session.commit()
    row_id = row.id
    session.close()

    first = client.post(f"/api/enrichment/candidates/{row_id}/dismiss")
    assert first.status_code == 200
    second = client.post(f"/api/enrichment/candidates/{row_id}/dismiss")
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "not_pending"


def test_bulk_accept_applies_the_selected_set_and_reports_failures_without_aborting(
    client: TestClient,
) -> None:
    session, part_a = _seeded_part("family part A")
    part_b = make_part(session, name="family part B")
    template = _template(session, "capacitance")
    row_a = candidates.record(
        session, part_a, template, "4.7uF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    row_b = candidates.record(
        session, part_b, template, "4.7uF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    session.commit()
    ids = [row_a.id, row_b.id, 999_999]  # last one does not exist
    session.close()

    response = client.post("/api/enrichment/candidates/bulk-accept", json={"candidate_ids": ids})
    assert response.status_code == 200, response.text
    results = {row["candidate_id"]: row for row in response.json()["results"]}
    assert results[row_a.id]["accepted"] is True
    assert results[row_b.id]["accepted"] is True
    assert results[999_999]["accepted"] is False
    assert results[999_999]["reason"] == "not_found"

    check = _session()
    assert _stored_value(check, part_a, "capacitance") is not None
    assert _stored_value(check, part_b, "capacitance") is not None
    check.close()


def test_bulk_accept_only_touches_the_selected_ids(client: TestClient) -> None:
    """Two parts each have a pending candidate; only one id is submitted."""
    session, part_a = _seeded_part("selected part")
    part_b = make_part(session, name="unselected part")
    template = _template(session, "capacitance")
    row_a = candidates.record(
        session, part_a, template, "4.7uF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    candidates.record(
        session, part_b, template, "4.7uF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    session.commit()
    row_a_id = row_a.id
    session.close()

    client.post("/api/enrichment/candidates/bulk-accept", json={"candidate_ids": [row_a_id]})

    check = _session()
    assert _stored_value(check, part_a, "capacitance") is not None
    assert _stored_value(check, part_b, "capacitance") is None  # not in the selected set
    check.close()


def test_no_two_response_models_collide_with_this_module(client: TestClient) -> None:
    """The generic guard already exists in `test_schema_invariants.py`; this
    pins that adding this module did not introduce the collision it guards."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert not any("__" in name for name in schemas)
