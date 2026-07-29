"""Extraction, the MPN-decoder cross-check, and what a model is never allowed to do.

Two rules are on trial here, and both fail *silently* when they break, which is
why the assertions below lean on refusals harder than on successes:

1. **A disagreement between a model and the part number's own arithmetic never
   resolves itself.** The tempting bug is not "average them" — nobody writes
   that on purpose — it is *picking the confident one*, which is the same thing
   with a plausible justification attached. The model self-reports its
   confidence; a hallucinated 0.94 beats a correct 0.90 every time.
2. **A part number a model read is never accepted as an identity.** The variant
   the fixture reports for `GRM188R71H224KA12D` is exactly the shape of the
   dangerous case: a perfectly well-formed part number, from a real family,
   which this catalogue has never heard of. It might be a real sibling in the
   table or an invention, and nothing here can tell the difference.

The fixture's second variant is worth reading closely. The model reports 10 µF
for `GRM188R61A475KE15D` and quotes, as its evidence, the *neighbouring row* of
the variant table — a line whose part number is `GRM188R61A106ME15D`. That is
`docs/PLAN.md`'s documented failure mode of table extraction reproduced
faithfully: the value is real, correct for some part, and wrong for this one. No
confidence score can see it. The quoted line in the review note is what lets a
human see it in about a second.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import (
    CandidateReviewReason,
    CandidateStatus,
    CrossCheckVerdict,
    IdentityRefusal,
    PromotionOutcome,
    Provenance,
)
from app.models.parameter import ParameterTemplate, ParameterValue
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services.enrichment import candidates, cross_check
from app.services.enrichment.extract import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResponseError,
    FakeExtractionProvider,
    FixtureMiss,
    chunk,
    parse_response,
    request_for,
    schema_for,
    target_fields,
)
from tests.factories import make_part

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "extraction" / "datasheet_extractions.json"
)

#: The Murata GRM188 X7R/X5R variant table.
GRM_DOC = "sha256:3f1c9a2b7d4e6058c1a9f3b2d84e70615c2d9a8b4f6071e3d5c8a2b9f0e17364"
#: A part no decoder family claims, so nothing cross-checks the model.
TPS_DOC = "sha256:a71d4e0c93b28f6157d0e4a2b9c8371f5e6a0d2c4b8917e3f05a6c1d92b4837e"

GRM_100N = "GRM188R71H104KA93D"
GRM_4U7 = "GRM188R61A475KE15D"
#: In the datasheet's table, absent from this catalogue. Never accepted.
GRM_220N = "GRM188R71H224KA12D"
TPS = "TPS62840DLCR"

GRM_TEXT = """Murata GRM188 series, 0603 (1608M), X7R / X5R
Part number | Cap (uF) | Char | Vdc | Size
GRM188R71H104KA93D | 0.10 | X7R | 50 | 0603 (1608M)
GRM188R71H224KA12D | 0.22 | X7R | 50 | 0603 (1608M)
GRM188R61A475KE15D | 4.7 | X5R | 10 | 0603 (1608M)
GRM188R61A106ME15D | 10 | X5R | 10 | 0603 (1608M)
"""

TPS_TEXT = """TPS62840 750-mA step-down converter with ultra-low quiescent current
VIN Input voltage range 1.8 6.5 V (Recommended Operating Conditions)
"""

REQUESTED = (
    "capacitance",
    "voltage_rating",
    "current_rating",
    "dielectric",
    "package",
    "mounting_type",
    "capacitor_technology",
)


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_categories(db)
    seed_parameter_templates(db)
    return db


@pytest.fixture
def provider() -> FakeExtractionProvider:
    return FakeExtractionProvider(FIXTURE)


def template(db: Session, name: str) -> ParameterTemplate:
    return db.execute(select(ParameterTemplate).where(ParameterTemplate.name == name)).scalar_one()


def templates(db: Session) -> list[ParameterTemplate]:
    return [template(db, name) for name in REQUESTED]


def make_catalogue_part(db: Session, mpn: str) -> Part:
    return make_part(db, name=f"catalogue {mpn}", mpn=mpn)


def stored(db: Session, part: Part, name: str) -> ParameterValue | None:
    return db.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id,
            ParameterValue.template_id == template(db, name).id,
        )
    ).scalar_one_or_none()


def candidate(db: Session, part: Part, name: str, source: Provenance) -> ParameterValueCandidate:
    return db.execute(
        select(ParameterValueCandidate).where(
            ParameterValueCandidate.part_id == part.id,
            ParameterValueCandidate.template_id == template(db, name).id,
            ParameterValueCandidate.source == source,
        )
    ).scalar_one()


def run_grm_batch(db: Session, provider: ExtractionProvider) -> cross_check.IngestReport:
    """The whole flow: build one request for the document, call once, ingest."""
    request = request_for(
        db,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N, GRM_4U7),
        templates=templates(db),
    )
    return cross_check.ingest(db, provider.extract(request))


def run_tps(db: Session, provider: ExtractionProvider) -> cross_check.IngestReport:
    request = request_for(
        db,
        document_ref=TPS_DOC,
        document_text=TPS_TEXT,
        mpns=(TPS,),
        templates=templates(db),
    )
    return cross_check.ingest(db, provider.extract(request))


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


def test_decoder_agreement_raises_confidence_and_promotes(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """0.62 alone would have queued. Corroborated, it promotes at 0.96.

    This is the cross-check earning its place: the model was *unsure* about the
    capacitance and its answer was still right, and the thing that establishes
    that is a second source of a completely different shape — arithmetic on the
    `104` in the part number. Confidence here is confidence in the **value**, so
    the corroborated number is what lands in `parameter_value`, not the model's
    self-report about its own reading.
    """
    part = make_catalogue_part(seeded, GRM_100N)
    report = run_grm_batch(seeded, provider)

    check = report.check_for(GRM_100N)
    assert check is not None
    assert check.decoder_family == "murata_grm"
    capacitance = check.field("capacitance")
    assert capacitance is not None
    assert capacitance.verdict is CrossCheckVerdict.CONFIRMED
    assert capacitance.extracted.confidence == pytest.approx(0.62)
    # Noisy-OR of the model's 0.62 and the decoder's 0.9: 1 - 0.38*0.10.
    assert capacitance.confidence == pytest.approx(0.962)
    assert capacitance.confidence > capacitance.extracted.confidence
    assert not check.disagreements
    assert not check.needs_review

    assert report.decision_for(GRM_100N, "capacitance") is not None
    assert report.decision_for(GRM_100N, "capacitance").outcome is PromotionOutcome.PROMOTED

    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(100e-9)
    # Promoted through `parameters.set_numeric`, so the bounds exist: a null
    # bound is invisible to every range query and nothing raises.
    assert value.value_min is not None and value.value_max is not None
    # The decoder's row is what was promoted — it outranks — and it carries the
    # combined confidence, so corroboration is visible on the value itself.
    assert value.provenance == Provenance.MPN_DECODER
    assert value.confidence == pytest.approx(0.962)
    # The model's own row is closed as superseded, not left in the queue.
    assert candidate(seeded, part, "capacitance", Provenance.LLM_INFERRED).status == (
        CandidateStatus.SUPERSEDED
    )


def test_combining_two_agreeing_sources_never_reaches_certainty(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """Two sources at 0.9 combine to 0.99, not to 1.0.

    `voltage_rating` on the second variant is 0.9 from the model and 0.9 from the
    decoder, whose noisy-OR is exactly the ceiling. Nothing in this system is
    entitled to report certainty: the pair could share a cause nobody has thought
    of, and both could be right about the wrong row.
    """
    make_catalogue_part(seeded, GRM_4U7)
    report = run_grm_batch(seeded, provider)

    check = report.check_for(GRM_4U7)
    assert check is not None
    voltage = check.field("voltage_rating")
    assert voltage is not None
    assert voltage.verdict is CrossCheckVerdict.CONFIRMED
    assert voltage.confidence == pytest.approx(cross_check.CONFIRMATION_CEILING)
    assert voltage.confidence < 1.0
    assert cross_check.combine(0.999, 0.999) <= cross_check.CONFIRMATION_CEILING


# ---------------------------------------------------------------------------
# Disagreement
# ---------------------------------------------------------------------------


def test_a_disagreement_queues_however_confident_the_model_was(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """0.94 buys nothing. The model read the neighbouring row of the table.

    Note what is *not* asserted: that some blend of 10 µF and 4.7 µF was stored.
    There is no such value, no source ever asserted it, and no reviewer could
    trace it to anything.
    """
    part = make_catalogue_part(seeded, GRM_4U7)
    report = run_grm_batch(seeded, provider)

    check = report.check_for(GRM_4U7)
    assert check is not None
    capacitance = check.field("capacitance")
    assert capacitance is not None
    assert capacitance.verdict is CrossCheckVerdict.CONFLICT
    assert capacitance.extracted.confidence == pytest.approx(0.94)
    assert [row.template_name for row in check.disagreements] == ["capacitance"]
    assert check.needs_review
    assert report.needs_review

    decision = report.decision_for(GRM_4U7, "capacitance")
    assert decision is not None
    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.SOURCES_DISAGREE
    assert stored(seeded, part, "capacitance") is None

    # Both readings survive in the queue. A disagreement that kept only one side
    # would be an overwrite wearing a queue's clothes.
    queued = [
        row
        for row in candidates.pending(seeded, part=part)
        if row.template_id == template(seeded, "capacitance").id
    ]
    assert {row.source for row in queued} == {Provenance.MPN_DECODER, Provenance.LLM_INFERRED}


def test_the_decoder_wins_the_conflict(seeded: Session, provider: FakeExtractionProvider) -> None:
    """The winner is named by the priority table, never by the confidences.

    "Wins" means three things and deliberately not a fourth. It is reported as
    the winner; it sorts to the top of the review queue, so the obvious click is
    the right one; and it is the value that lands when that click happens. What
    it does **not** mean is that a background job writes it: the field is empty
    and two sources contradict each other, which is the one situation where
    picking either is a guess, and `candidates.evaluate()` refuses to guess.
    """
    part = make_catalogue_part(seeded, GRM_4U7)
    report = run_grm_batch(seeded, provider)

    check = report.check_for(GRM_4U7)
    assert check is not None
    capacitance = check.field("capacitance")
    assert capacitance is not None
    assert capacitance.winner is Provenance.MPN_DECODER
    assert capacitance.winning_value == "4.7 uF ±10%"
    assert capacitance.decoded_raw == "4.7 uF ±10%"

    # Queue order: the decoder first, despite the model's higher self-report.
    queued = [
        row
        for row in candidates.pending(seeded, part=part)
        if row.template_id == template(seeded, "capacitance").id
    ]
    assert queued[0].source == Provenance.MPN_DECODER
    assert queued[0].confidence > queued[1].confidence
    assert queued[1].source == Provenance.LLM_INFERRED

    # Nothing written until a human clicks; then it is the decoder's value.
    assert stored(seeded, part, "capacitance") is None
    value = candidates.promote(seeded, queued[0])
    assert value.value_nominal == pytest.approx(4.7e-6)
    assert value.provenance == Provenance.MPN_DECODER


def test_a_contradicted_model_reading_cannot_promote_even_if_the_decoder_is_dismissed(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """The clamp is what makes the refusal durable rather than momentary.

    Dismissing the decoder's row is a reasonable human act — sometimes *this
    repository's* transcription of a numbering table is what is wrong. But it
    removes the row whose presence was blocking the model's value, and without
    the clamp the next `evaluate()` would find one lone 0.94 source proposing
    into an empty field and promote it. The value a human just declined to accept
    would then appear anyway, with nobody having chosen it.
    """
    part = make_catalogue_part(seeded, GRM_4U7)
    run_grm_batch(seeded, provider)

    candidates.dismiss(seeded, candidate(seeded, part, "capacitance", Provenance.MPN_DECODER))
    decision = candidates.evaluate(seeded, part, template(seeded, "capacitance"))

    # Verified by mutation: drop the clamp in `cross_check` and this promotes.
    assert decision.outcome is PromotionOutcome.QUEUED
    assert stored(seeded, part, "capacitance") is None
    assert (
        candidate(seeded, part, "capacitance", Provenance.LLM_INFERRED).confidence
        <= cross_check.CONFLICT_CONFIDENCE_CEILING
    )


def test_the_conflict_ceiling_sits_below_the_auto_promote_bar() -> None:
    """The one load-bearing property of `CONFLICT_CONFIDENCE_CEILING`.

    Asserted as a relation between the two constants rather than as a literal, so
    neither can be edited into agreement with the other by accident — raising the
    ceiling to 0.8 or dropping the bar to 0.5 would each silently restore the
    auto-promotion the test above exists to prevent.
    """
    assert cross_check.CONFLICT_CONFIDENCE_CEILING < candidates.AUTO_PROMOTE_CONFIDENCE


def test_the_review_note_carries_the_quoted_source_text(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """A reviewer decides from the datasheet's own words, not from a score.

    The quoted line for the conflicting field names `GRM188R61A106ME15D` — a
    different part — which is the whole diagnosis, visible in the queue without
    opening a PDF.
    """
    part = make_catalogue_part(seeded, GRM_4U7)
    run_grm_batch(seeded, provider)

    note = candidate(seeded, part, "capacitance", Provenance.LLM_INFERRED).note
    assert note is not None
    assert "GRM188R61A106ME15D | 10 | X5R | 10 | 0603 (1608M)" in note
    assert "page 5" in note
    assert "DISAGREES" in note
    # And what it disagrees *with*, so the reviewer is not sent looking for it.
    assert "4.7 uF ±10%" in note
    assert "murata_grm" in note


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


def test_a_batch_of_variants_produces_per_variant_candidates(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """One document, one call, one candidate set per part.

    A whole variant table in one request is what makes the cost trivial, and the
    per-variant separation is what stops it being a liability: the batch that
    contains a wrong row still promotes the other part's fields, and the
    disagreement stays attached to the one part it concerns.
    """
    hundred_nano = make_catalogue_part(seeded, GRM_100N)
    four_seven = make_catalogue_part(seeded, GRM_4U7)

    report = run_grm_batch(seeded, provider)

    assert len(provider.calls) == 1
    assert provider.calls[0].mpns == (GRM_100N, GRM_4U7)
    assert {check.mpn for check in report.checks} == {GRM_100N, GRM_4U7}

    # Every model-side row is attributed to the document it was read from, which
    # is what makes a nightly re-run update its own rows and a *revised*
    # datasheet a second, comparable observation.
    for part in (hundred_nano, four_seven):
        rows = seeded.execute(
            select(ParameterValueCandidate).where(
                ParameterValueCandidate.part_id == part.id,
                ParameterValueCandidate.source == Provenance.LLM_INFERRED,
            )
        ).scalars()
        refs = {row.source_ref for row in rows}
        assert refs == {GRM_DOC}

    # The good variant promoted despite sharing a call with the bad one.
    assert stored(seeded, hundred_nano, "capacitance") is not None
    assert stored(seeded, four_seven, "capacitance") is None
    assert stored(seeded, four_seven, "voltage_rating") is not None


def test_re_ingesting_the_same_document_adds_no_rows(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """A nightly re-run is idempotent, because consensus is counted from rows.

    Two rows from one observation would be a manufactured second opinion.
    """
    make_catalogue_part(seeded, GRM_100N)
    make_catalogue_part(seeded, GRM_4U7)

    run_grm_batch(seeded, provider)
    before = seeded.execute(select(func.count(ParameterValueCandidate.id))).scalar_one()
    run_grm_batch(seeded, provider)
    after = seeded.execute(select(func.count(ParameterValueCandidate.id))).scalar_one()

    assert before == after


def test_chunk_splits_the_part_list_but_never_the_document(seeded: Session) -> None:
    """A 400-variant family needs several calls; the header must be in all of them.

    Slicing the text per chunk is the tempting other half of this, and it is how
    a table's rows get separated from the header that says which column is which.
    """
    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=tuple(f"GRM188R71H10{index}KA93D" for index in range(5)),
        templates=templates(seeded),
    )

    parts = list(chunk(request, size=2))

    assert [len(one.mpns) for one in parts] == [2, 2, 1]
    assert [mpn for one in parts for mpn in one.mpns] == list(request.mpns)
    assert {one.document_text for one in parts} == {GRM_TEXT}
    assert {one.document_ref for one in parts} == {GRM_DOC}


# ---------------------------------------------------------------------------
# The part number is never accepted
# ---------------------------------------------------------------------------


def test_a_part_number_the_catalogue_does_not_have_is_refused_outright(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """`GRM188R71H224KA12D` is well-formed, plausible, and unknown here.

    It might be a real sibling row the model helpfully noticed, or a number it
    assembled out of the family's pattern. Nothing available at this point can
    distinguish those, and a wrong-but-confident identity is worse than none — so
    no part is created, no candidate is written, and the number comes back with
    the text it was read from for a human to judge.
    """
    make_catalogue_part(seeded, GRM_100N)
    make_catalogue_part(seeded, GRM_4U7)
    parts_before = seeded.execute(select(func.count(Part.id))).scalar_one()

    report = run_grm_batch(seeded, provider)

    assert [(row.mpn, row.reason) for row in report.unclaimed] == [
        (GRM_220N, IdentityRefusal.NO_MATCH)
    ]
    refusal = report.unclaimed[0]
    assert "GRM188R71H224KA12D | 0.22 | X7R | 50 | 0603 (1608M)" in refusal.source_text
    assert refusal.document_ref == GRM_DOC
    # The values are handed back so accepting the variant later does not mean
    # re-running the extraction — but nothing was stored for them.
    assert [row.raw_value for row in refusal.fields] == ["0.22 uF"]

    assert seeded.execute(select(func.count(Part.id))).scalar_one() == parts_before
    assert report.needs_review
    # 220 nF appears nowhere in the candidate table.
    raws = seeded.execute(select(ParameterValueCandidate.raw_value)).scalars().all()
    assert "0.22 uF" not in raws


def test_an_ambiguous_part_number_is_refused_rather_than_guessed(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """Two catalogue rows normalise to one `mpn_norm`. Picking either is a coin flip.

    The sibling variant in the same batch still lands, which is the point of
    refusing per variant rather than per document: one ambiguous part number in a
    table does not cost the rest of it.
    """
    first = make_catalogue_part(seeded, GRM_100N)
    second = make_catalogue_part(seeded, "grm188-r71h104-ka93d")
    unambiguous = make_catalogue_part(seeded, GRM_4U7)

    report = run_grm_batch(seeded, provider)

    reasons = {row.mpn: row.reason for row in report.unclaimed}
    assert reasons[GRM_100N] is IdentityRefusal.AMBIGUOUS
    assert report.check_for(GRM_100N) is None
    for part in (first, second):
        assert (
            seeded.execute(
                select(func.count(ParameterValueCandidate.id)).where(
                    ParameterValueCandidate.part_id == part.id
                )
            ).scalar_one()
            == 0
        )
    assert stored(seeded, unambiguous, "voltage_rating") is not None


def test_identity_matching_survives_the_manufacturers_own_punctuation(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """A catalogue MPN written with hyphens still matches and still cross-checks.

    `mpn_norm` is the same key the bare-MPN scan resolver uses, so the model
    echoing the unhyphenated form of a number the catalogue stores hyphenated is
    a match — otherwise every hyphenated row would silently fall through to
    `IdentityRefusal.NO_MATCH` and the extraction would look like it found
    nothing.

    Note what this test does *not* prove. `ingest` decodes `part.mpn` rather than
    `variant.mpn` deliberately, but the two are provably identical here: a variant
    only reaches the decoder when its number normalises equal to the catalogue's,
    and the decoder normalises its input. Using the catalogue's string is
    defensive against a future looser match (fuzzy, alias, manufacturer-scoped),
    not against anything reachable today — and an unreachable guarantee cannot be
    asserted, only stated.
    """
    part = make_catalogue_part(seeded, "GRM188-R71H104-KA93D")

    report = run_grm_batch(seeded, provider)

    assert GRM_100N not in {row.mpn for row in report.unclaimed}
    check = report.check_for(GRM_100N)
    assert check is not None
    assert check.decoder_family == "murata_grm"
    # `source_ref` is the decoding family, so a second family claiming the number
    # later would be a comparable observation rather than an overwrite.
    assert candidate(seeded, part, "capacitance", Provenance.MPN_DECODER).source_ref == "murata_grm"


# ---------------------------------------------------------------------------
# Nothing to check against
# ---------------------------------------------------------------------------


def test_an_unchecked_field_faces_the_plain_confidence_bar(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """No decoder family claims `TPS62840DLCR`, so the 0.8 threshold decides alone.

    This is the honest limit of the cross-check: five passive families are
    covered and an IC is not, so most of the catalogue's interesting parts arrive
    with no second opinion available. `docs/PLAN.md`'s rule then applies
    literally — 0.86 promotes as `llm_inferred`, 0.7 queues — and the review
    queue is where the difference goes.
    """
    part = make_catalogue_part(seeded, TPS)
    report = run_tps(seeded, provider)

    check = report.check_for(TPS)
    assert check is not None
    assert check.decoder_family is None
    assert {row.verdict for row in check.fields} == {CrossCheckVerdict.UNCHECKED}
    # Unchecked means unadjusted: no confirmation to raise it, no conflict to clamp it.
    voltage = check.field("voltage_rating")
    current = check.field("current_rating")
    assert voltage is not None and current is not None
    assert voltage.confidence == pytest.approx(0.86)
    assert current.confidence == pytest.approx(0.7)
    # And the winner is the only source there was.
    assert voltage.winner is Provenance.LLM_INFERRED

    value = stored(seeded, part, "voltage_rating")
    assert value is not None
    assert value.provenance == Provenance.LLM_INFERRED
    assert value.value_min is not None and value.value_max is not None

    assert stored(seeded, part, "current_rating") is None
    decision = report.decision_for(TPS, "current_rating")
    assert decision is not None
    assert decision.reason is CandidateReviewReason.LOW_CONFIDENCE
    assert check.needs_review

    note = candidate(seeded, part, "current_rating", Provenance.LLM_INFERRED).note
    assert note is not None
    assert "no part-number decoder recognised this part number" in note
    assert "750-mA output current (typical, see Figure 8-3)" in note


# ---------------------------------------------------------------------------
# The interface itself
# ---------------------------------------------------------------------------


def test_the_schema_constrains_field_names_to_the_request(seeded: Session) -> None:
    """The model is offered this install's templates and nothing else."""
    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        templates=[template(seeded, "capacitance"), template(seeded, "dielectric")],
    )
    schema = schema_for(request)

    field_schema = schema["properties"]["variants"]["items"]["properties"]["fields"]["items"]
    assert field_schema["properties"]["template_name"]["enum"] == ["capacitance", "dielectric"]
    assert field_schema["required"] == [
        "template_name",
        "raw_value",
        "confidence",
        "source_text",
    ]
    assert field_schema["additionalProperties"] is False
    # Enum choices come from the database, aliases included, so a datasheet
    # written in the metric convention is not forced into the imperial one.
    guide = field_schema["properties"]["template_name"]["description"]
    assert "X7R" in guide


def test_target_fields_drops_templates_no_writer_exists_for(seeded: Session) -> None:
    """Asking for a value that could never be recorded only spends tokens.

    `candidates.record` refuses `text` and `bool` templates because
    `app.services.parameters` has no writer for them, so requesting one would
    manufacture review-queue items nobody can action.
    """
    from app.models.enums import ValueType

    text_template = ParameterTemplate(
        name="marking_code", display_name="Marking", value_type=ValueType.TEXT
    )
    seeded.add(text_template)
    seeded.flush()

    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        templates=[template(seeded, "capacitance"), text_template],
    )

    assert request.field_names == ("capacitance",)


def test_the_parser_refuses_a_field_the_request_did_not_ask_for(seeded: Session) -> None:
    """A model must never introduce a parameter this install did not define."""
    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        templates=[template(seeded, "capacitance")],
    )
    payload = {
        "variants": [
            {
                "mpn": GRM_100N,
                "mpn_source_text": "row 1",
                "fields": [
                    {
                        "template_name": "esr_at_100khz",
                        "raw_value": "50 mohm",
                        "confidence": 0.9,
                        "source_text": "ESR 50 mOhm",
                    }
                ],
            }
        ]
    }

    with pytest.raises(ExtractionResponseError, match="was not requested"):
        parse_response(payload, request, provider="p", model="m")


def test_the_parser_refuses_a_value_with_no_source_text(seeded: Session) -> None:
    """An untraceable value cannot be reviewed, so it is not stored at all.

    The alternative is a row in the queue that a human can only accept or reject
    on the model's word — which is the one thing this pipeline is built not to
    ask of anybody.
    """
    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        templates=[template(seeded, "capacitance")],
    )
    payload = {
        "variants": [
            {
                "mpn": GRM_100N,
                "mpn_source_text": "row 1",
                "fields": [
                    {
                        "template_name": "capacitance",
                        "raw_value": "100 nF",
                        "confidence": 0.99,
                        "source_text": "   ",
                    }
                ],
            }
        ]
    }

    with pytest.raises(ExtractionResponseError, match="source_text"):
        parse_response(payload, request, provider="p", model="m")


def test_the_parser_refuses_an_out_of_range_confidence(seeded: Session) -> None:
    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        templates=[template(seeded, "capacitance")],
    )
    payload = {
        "variants": [
            {
                "mpn": GRM_100N,
                "mpn_source_text": "row 1",
                "fields": [
                    {
                        "template_name": "capacitance",
                        "raw_value": "100 nF",
                        "confidence": 1.4,
                        "source_text": "row 1",
                    }
                ],
            }
        ]
    }

    with pytest.raises(ExtractionResponseError, match="between 0 and 1"):
        parse_response(payload, request, provider="p", model="m")


def test_the_parser_refuses_two_rows_for_one_part_number(seeded: Session) -> None:
    """They would collide on the candidate table's observation uniqueness.

    The second row would overwrite the first in place, so a response that
    actually contained a disagreement would be stored as a single opinion.
    """
    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        templates=[template(seeded, "capacitance")],
    )
    row = {
        "mpn": GRM_100N,
        "mpn_source_text": "row 1",
        "fields": [
            {
                "template_name": "capacitance",
                "raw_value": "100 nF",
                "confidence": 0.9,
                "source_text": "row 1",
            }
        ],
    }

    with pytest.raises(ExtractionResponseError, match="more than once"):
        parse_response({"variants": [row, dict(row)]}, request, provider="p", model="m")


def test_a_fixture_miss_raises_rather_than_extracting_nothing(
    seeded: Session, provider: FakeExtractionProvider
) -> None:
    """An empty result is indistinguishable from an empty datasheet.

    A fake that returned one on a miss would let a test pass while extracting
    nothing at all, which is the failure mode that makes fakes worthless.
    """
    request = ExtractionRequest(
        document_ref="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        fields=target_fields(seeded, templates(seeded)),
    )

    with pytest.raises(FixtureMiss):
        provider.extract(request)


def test_a_request_needs_a_document_ref_and_at_least_one_part(seeded: Session) -> None:
    """`document_ref` becomes `source_ref`, which is half the idempotency key."""
    fields = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N,),
        templates=templates(seeded),
    ).fields

    with pytest.raises(ValueError, match="document_ref"):
        ExtractionRequest(document_ref="", document_text="x", mpns=(GRM_100N,), fields=fields)
    with pytest.raises(ValueError, match="part number"):
        ExtractionRequest(document_ref=GRM_DOC, document_text="x", mpns=(), fields=fields)
    with pytest.raises(ValueError, match="target field"):
        ExtractionRequest(document_ref=GRM_DOC, document_text="x", mpns=(GRM_100N,), fields=())


def test_the_fixture_is_still_shaped_like_a_real_response() -> None:
    """The fixture is the only ground truth here, so its shape is asserted directly.

    It is currently **hand-authored**, not recorded — there was no model to
    record from — and that is stated in the file itself. This test is what stops
    a later edit from quietly turning it into whatever makes a new assertion
    pass.
    """
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "HAND-AUTHORED" in body["_provenance"]
    assert set(body["responses"]) == {GRM_DOC, TPS_DOC}
    for response in body["responses"].values():
        for variant in response["variants"]:
            assert variant["mpn_source_text"]
            for field in variant["fields"]:
                assert set(field) <= {
                    "template_name",
                    "raw_value",
                    "confidence",
                    "source_text",
                    "page",
                }
                assert field["source_text"]
                assert 0.0 <= field["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# The live contract test
# ---------------------------------------------------------------------------


def _live_provider() -> ExtractionProvider | None:
    """The provider named by `ALMAGEST_EXTRACTION_PROVIDER`, as `module:factory`.

    An environment variable rather than a settings key because nothing in the API
    process reads it: per `docs/PLAN.md` the model runs in a worker, and the API
    must not grow a Docling or inference dependency. When a real provider lands
    it satisfies `ExtractionProvider` and this hook is how the live test reaches
    it — no other line of this file changes.
    """
    target = os.environ.get("ALMAGEST_EXTRACTION_PROVIDER")
    if not target:
        return None
    module_name, _, attribute = target.rpartition(":")
    factory = getattr(importlib.import_module(module_name), attribute)
    provider: ExtractionProvider = factory()
    return provider


@pytest.mark.live
def test_live_extraction_matches_the_recorded_contract(seeded: Session) -> None:
    """Catch upstream drift: a real provider still satisfies `parse_response`.

    Never runs in CI, and the assertions are deliberately about *shape* rather
    than about values. A model is not required to agree with the fixture — it is
    required to answer only with the fields it was asked for, to quote a source
    for each of them, and to report a confidence in range. Those are the
    properties the whole cross-check is built on, and a provider or model upgrade
    is exactly what silently breaks one of them.
    """
    provider = _live_provider()
    if provider is None:
        pytest.skip("set ALMAGEST_EXTRACTION_PROVIDER=module:factory to run this")

    request = request_for(
        seeded,
        document_ref=GRM_DOC,
        document_text=GRM_TEXT,
        mpns=(GRM_100N, GRM_4U7),
        templates=templates(seeded),
    )
    result = provider.extract(request)

    assert result.variants, "a real provider returned nothing for a table it can read"
    for variant in result.variants:
        assert variant.mpn_source_text
        for field in variant.fields:
            assert field.template_name in request.field_names
            assert field.source_text
            assert 0.0 <= field.confidence <= 1.0
    assert result.variant_for(GRM_100N) is not None


def test_the_live_contract_test_is_collected_and_skipped_by_default(
    request: pytest.FixtureRequest,
) -> None:
    """Nobody deletes the live test, and nobody lets CI dial a network.

    Two failure modes, opposite directions. The marker going missing means CI
    starts making network calls; the *test* going missing means upstream schema
    drift stops being detectable and nothing ever says so. Referencing the
    function by name is what makes the second one a collection error rather than
    a silent absence.
    """
    marks = {mark.name for mark in test_live_extraction_matches_the_recorded_contract.pytestmark}
    assert "live" in marks

    item = next(
        (
            candidate_item
            for candidate_item in request.session.items
            if candidate_item.name == "test_live_extraction_matches_the_recorded_contract"
        ),
        None,
    )
    if item is None:  # a filtered invocation; the assertion above still holds
        pytest.skip("the live test was not collected in this run")
    assert item.get_closest_marker("skip") is not None, (
        "conftest.pytest_collection_modifyitems must skip live tests unless -m live"
    )
