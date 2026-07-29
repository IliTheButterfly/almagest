"""Enrichment writes candidates; the rules decide what becomes a value.

Every test here guards a decision whose failure mode is **silent**. A wrong
auto-promotion looks exactly like a right one, participates in every
substitution decision, and surfaces when a board does not work. A review-queue
item is merely work. So the suite is biased the same way the code is: it
asserts on refusals at least as hard as on promotions.

The load-bearing one is `test_promotion_writes_through_parameters_so_bounds_are_set`.
A numeric row with null `value_min`/`value_max` is invisible to every range
query and nothing raises — a 22 µF capacitor that simply never appears in a
search for 20–30 µF. That is the bug this whole write path exists to make
impossible, and it can only be caught by asserting the columns directly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import (
    CandidateReviewReason,
    CandidateStatus,
    PromotionOutcome,
    Provenance,
)
from app.models.parameter import ParameterTemplate, ParameterValue
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services import parameters
from app.services.enrichment import candidates
from app.services.enrichment.mpn_decoders import DecodedPart, decode
from tests.factories import make_part


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_categories(db)
    seed_parameter_templates(db)
    return db


@pytest.fixture
def part(seeded: Session) -> Part:
    return make_part(seeded, name="candidate test part")


def template(db: Session, name: str) -> ParameterTemplate:
    return db.execute(select(ParameterTemplate).where(ParameterTemplate.name == name)).scalar_one()


def stored(db: Session, part: Part, name: str) -> ParameterValue | None:
    return db.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id,
            ParameterValue.template_id == template(db, name).id,
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Rule 3: one confident source into an empty field
# ---------------------------------------------------------------------------


def test_a_single_high_confidence_candidate_into_an_empty_field_promotes(
    seeded: Session, part: Part
) -> None:
    decision = candidates.submit(
        seeded,
        part,
        template(seeded, "capacitance"),
        "100nF",
        source=Provenance.DATASHEET_TABLE,
        confidence=0.95,
    )

    assert decision.outcome is PromotionOutcome.PROMOTED
    assert decision.promoted is not None
    assert decision.promoted.status == CandidateStatus.PROMOTED
    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(100e-9)
    # Promotion copies the source into the value's provenance, which is why one
    # enum serves both columns.
    assert value.provenance == Provenance.DATASHEET_TABLE


def test_a_single_low_confidence_candidate_queues(seeded: Session, part: Part) -> None:
    """0.79 is not 0.8. The threshold is the design doc's, applied literally."""
    decision = candidates.submit(
        seeded,
        part,
        template(seeded, "capacitance"),
        "100nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.79,
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.LOW_CONFIDENCE
    assert stored(seeded, part, "capacitance") is None
    assert [row.id for row in candidates.pending(seeded, part=part)] == [decision.queued[0].id]


def test_the_same_candidate_into_an_occupied_field_does_not_promote(
    seeded: Session, part: Part
) -> None:
    """The field is already answered, and this candidate answers it differently.

    Confidence is irrelevant here — 0.99 does not buy the right to overwrite.
    """
    parameters.set_numeric(
        seeded,
        part,
        template(seeded, "capacitance"),
        "220nF",
        provenance=Provenance.DISTRIBUTOR_FREETEXT,
    )

    decision = candidates.submit(
        seeded,
        part,
        template(seeded, "capacitance"),
        "100nF",
        source=Provenance.MPN_DECODER,
        confidence=0.99,
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.FIELD_OCCUPIED
    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(220e-9)
    assert value.provenance == Provenance.DISTRIBUTOR_FREETEXT


# ---------------------------------------------------------------------------
# Rule 2: agreement
# ---------------------------------------------------------------------------


def test_two_agreeing_sources_promote_the_more_trusted_one(seeded: Session, part: Part) -> None:
    """`100 nF` and `0.1 uF` are the same number, spelled two ways.

    Both sources are below the single-source bar. Agreement between two
    independent sources is stronger evidence than either one's self-report, so
    this promotes and the 0.8 threshold does not apply.
    """
    capacitance = template(seeded, "capacitance")
    candidates.record(
        seeded,
        part,
        capacitance,
        "0.1uF",
        source=Provenance.DISTRIBUTOR_FREETEXT,
        confidence=0.5,
    )
    decision = candidates.submit(
        seeded,
        part,
        capacitance,
        "100nF",
        source=Provenance.DATASHEET_TABLE,
        confidence=0.6,
    )

    assert decision.outcome is PromotionOutcome.PROMOTED
    assert decision.promoted is not None
    assert decision.promoted.source == Provenance.DATASHEET_TABLE
    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(100e-9)
    # The other row is closed without a human ever seeing it: nothing to decide.
    other = _candidate(seeded, part, capacitance, Provenance.DISTRIBUTOR_FREETEXT)
    assert other.status == CandidateStatus.SUPERSEDED
    assert candidates.pending(seeded, part=part) == []


def test_a_tolerance_band_agrees_with_the_bare_nominal(seeded: Session, part: Part) -> None:
    """`100 nF` and `100 nF ±10%` assert the same value.

    They differ only in what the datasheet promises about it, and the more
    trusted source's tolerance is the one that gets stored — so the promoted
    interval is the band, not the point.
    """
    capacitance = template(seeded, "capacitance")
    candidates.record(
        seeded, part, capacitance, "100nF", source=Provenance.DISTRIBUTOR_FREETEXT, confidence=0.5
    )
    decision = candidates.submit(
        seeded, part, capacitance, "100nF ±10%", source=Provenance.DATASHEET_TABLE, confidence=0.5
    )

    assert decision.outcome is PromotionOutcome.PROMOTED
    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_min == pytest.approx(90e-9)
    assert value.value_max == pytest.approx(110e-9)


def test_two_disagreeing_sources_queue_and_write_nothing(seeded: Session, part: Part) -> None:
    """100 nF and 104 nF are 4% apart — inside a ±10% band, different values.

    `104` is the printed marking *for* 100 nF, so a source reporting 104 nF has
    most likely mis-read a multiplier, and letting a mis-read confirm a
    mis-extraction is the invisible corruption this table prevents. Both rows
    survive so a human can see the disagreement, most trusted first.
    """
    capacitance = template(seeded, "capacitance")
    candidates.record(
        seeded, part, capacitance, "104nF", source=Provenance.LLM_INFERRED, confidence=0.99
    )
    decision = candidates.submit(
        seeded, part, capacitance, "100nF", source=Provenance.DATASHEET_TABLE, confidence=0.99
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.SOURCES_DISAGREE
    assert stored(seeded, part, "capacitance") is None

    queue = candidates.pending(seeded, part=part)
    assert len(queue) == 2
    # Ordered by trust, so the obvious click is the right one.
    assert queue[0].source == Provenance.DATASHEET_TABLE
    assert queue[1].source == Provenance.LLM_INFERRED
    assert {row.review_reason for row in queue} == {CandidateReviewReason.SOURCES_DISAGREE}


def test_a_range_never_agrees_with_a_scalar_inside_it(seeded: Session, part: Part) -> None:
    """'somewhere between 20 and 30 µF' and '22 µF' are different assertions.

    Collapsing the first into the second would invent precision no source
    claimed.
    """
    capacitance = template(seeded, "capacitance")
    candidates.record(
        seeded, part, capacitance, "20-30uF", source=Provenance.DISTRIBUTOR_FREETEXT, confidence=0.9
    )
    decision = candidates.submit(
        seeded, part, capacitance, "22uF", source=Provenance.DATASHEET_TABLE, confidence=0.9
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.SOURCES_DISAGREE
    assert stored(seeded, part, "capacitance") is None


def test_agreement_is_not_a_tolerance_band_even_at_the_e96_step(
    seeded: Session, part: Part
) -> None:
    """The hard ceiling on the epsilon: adjacent E96 values are ~2.3% apart.

    10.0 kΩ and 10.2 kΩ are both real catalogue values. An epsilon wide enough
    to fold them together would make the agreement rule able to confirm the
    wrong one of two legitimate parts.
    """
    resistance = template(seeded, "resistance")
    candidates.record(
        seeded, part, resistance, "10k", source=Provenance.DATASHEET_TABLE, confidence=0.5
    )
    decision = candidates.submit(
        seeded, part, resistance, "10.2k", source=Provenance.MPN_DECODER, confidence=0.5
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.SOURCES_DISAGREE


def test_two_agreeing_sources_do_not_outvote_a_third(seeded: Session, part: Part) -> None:
    """There is no majority vote, deliberately.

    A dissent means something is wrong with one of three readings, and outvoting
    it throws away the evidence that made the field worth a human's attention.
    The sources are not independent enough for a vote to mean much anyway —
    distributor free text is frequently copied from the same datasheet the
    extractor read.
    """
    capacitance = template(seeded, "capacitance")
    candidates.record(
        seeded, part, capacitance, "100nF", source=Provenance.DATASHEET_TABLE, confidence=0.95
    )
    candidates.record(
        seeded, part, capacitance, "0.1uF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    decision = candidates.submit(
        seeded, part, capacitance, "220nF", source=Provenance.LLM_INFERRED, confidence=0.5
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.SOURCES_DISAGREE
    assert stored(seeded, part, "capacitance") is None
    assert len(candidates.pending(seeded, part=part)) == 3


# ---------------------------------------------------------------------------
# The rule that has no exceptions
# ---------------------------------------------------------------------------


def test_a_manual_value_is_never_overwritten_automatically(seeded: Session, part: Part) -> None:
    """Nothing a background job can reach may rewrite what a human typed.

    Belt and braces with the priority order — nothing outranks `MANUAL` in
    `PROVENANCE_PRIORITY` — because this is the guarantee that must survive a
    future reshuffle of those numbers.
    """
    capacitance = template(seeded, "capacitance")
    parameters.set_numeric(seeded, part, capacitance, "220nF", provenance=Provenance.MANUAL)

    for source in (
        Provenance.DATASHEET_TABLE,
        Provenance.MPN_DECODER,
        Provenance.DISTRIBUTOR_FREETEXT,
        Provenance.LLM_INFERRED,
    ):
        decision = candidates.submit(
            seeded,
            part,
            capacitance,
            "100nF",
            source=source,
            confidence=1.0,
            source_ref=str(source),
        )
        assert decision.outcome is PromotionOutcome.QUEUED, source
        assert decision.reason is CandidateReviewReason.FIELD_OCCUPIED, source

    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(220e-9)
    assert value.provenance == Provenance.MANUAL


def test_a_manual_value_is_not_even_reprovenanced_by_an_agreeing_source(
    seeded: Session, part: Part
) -> None:
    """An agreeing source may raise provenance — but never onto a manual row.

    There is nothing above `MANUAL` to raise it to, and the code refuses
    explicitly rather than relying on that arithmetic.
    """
    capacitance = template(seeded, "capacitance")
    parameters.set_numeric(seeded, part, capacitance, "100nF", provenance=Provenance.MANUAL)

    decision = candidates.submit(
        seeded, part, capacitance, "0.1uF", source=Provenance.DATASHEET_TABLE, confidence=1.0
    )

    assert decision.outcome is PromotionOutcome.ALREADY_SATISFIED
    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.provenance == Provenance.MANUAL
    assert value.raw_input == "100nF"


# ---------------------------------------------------------------------------
# The late higher-priority source
# ---------------------------------------------------------------------------


def test_a_late_higher_priority_source_that_disagrees_queues_rather_than_overwrites(
    seeded: Session, part: Part
) -> None:
    """The decision this phase exists to make.

    `datasheet_table` outranks `llm_inferred`, and the promoted value is already
    in `parameter_value`. It is still a review item: a silent overwrite is
    undetectable, while a queue item costs ten seconds — and disagreement between
    the two most trusted sources is the strongest signal in the system that a
    human should look.
    """
    capacitance = template(seeded, "capacitance")
    first = candidates.submit(
        seeded, part, capacitance, "100nF", source=Provenance.LLM_INFERRED, confidence=0.9
    )
    assert first.outcome is PromotionOutcome.PROMOTED

    later = candidates.submit(
        seeded, part, capacitance, "220nF", source=Provenance.DATASHEET_TABLE, confidence=0.95
    )

    assert later.outcome is PromotionOutcome.QUEUED
    assert later.reason is CandidateReviewReason.FIELD_OCCUPIED
    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(100e-9)
    assert value.provenance == Provenance.LLM_INFERRED

    # Nothing is lost — the reviewer's one click applies it through the same
    # `parameters` door, and the bounds come out populated.
    queued = candidates.pending(seeded, part=part)
    assert [row.source for row in queued] == [Provenance.DATASHEET_TABLE]
    promoted = candidates.promote(seeded, queued[0])
    assert promoted.value_nominal == pytest.approx(220e-9)
    assert promoted.value_min == pytest.approx(220e-9)
    assert promoted.value_max == pytest.approx(220e-9)
    assert promoted.provenance == Provenance.DATASHEET_TABLE
    assert candidates.pending(seeded, part=part) == []


def test_a_late_higher_priority_source_that_agrees_only_raises_provenance(
    seeded: Session, part: Part
) -> None:
    """Agreement is applied silently, because the value does not move."""
    capacitance = template(seeded, "capacitance")
    candidates.submit(
        seeded, part, capacitance, "100nF", source=Provenance.LLM_INFERRED, confidence=0.9
    )

    decision = candidates.submit(
        seeded, part, capacitance, "0.1uF", source=Provenance.DATASHEET_TABLE, confidence=0.95
    )

    assert decision.outcome is PromotionOutcome.PROMOTED
    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(100e-9)
    assert value.provenance == Provenance.DATASHEET_TABLE
    assert candidates.pending(seeded, part=part) == []


def test_a_reviewer_may_overrule_the_priority_order(seeded: Session, part: Part) -> None:
    """`promote(force=True)` is reachable only from a human decision.

    A reviewer who read both numbers may pick the lower-priority one;
    `parameters._existing_or_new` would otherwise refuse it as `LowerPrecedence`.
    No rule in `evaluate()` sets `force`, which is the whole distinction.
    """
    capacitance = template(seeded, "capacitance")
    parameters.set_numeric(
        seeded, part, capacitance, "220nF", provenance=Provenance.DATASHEET_TABLE
    )
    decision = candidates.submit(
        seeded, part, capacitance, "100nF", source=Provenance.MPN_DECODER, confidence=0.9
    )
    assert decision.outcome is PromotionOutcome.QUEUED

    value = candidates.promote(seeded, decision.queued[0], force=True)
    assert value.value_nominal == pytest.approx(100e-9)
    assert value.provenance == Provenance.MPN_DECODER


# ---------------------------------------------------------------------------
# The load-bearing assertion
# ---------------------------------------------------------------------------


def test_promotion_writes_through_parameters_so_bounds_are_set(seeded: Session, part: Part) -> None:
    """Asserted on the columns directly, because the failure is silent.

    Parametric search is an interval-overlap test (`value_min <= hi AND
    value_max >= lo`). A promoted row carrying only `value_nominal` raises
    nothing and is invisible to every range query — a 22 µF capacitor that never
    appears in a search for 20–30 µF. A promotion path that inserted its own row
    instead of going through `app.services.parameters` would reintroduce exactly
    that, for automated data only, which is the data nobody eyeballs.
    """
    decision = candidates.submit(
        seeded,
        part,
        template(seeded, "capacitance"),
        "22uF",
        source=Provenance.DATASHEET_TABLE,
        confidence=0.9,
    )
    assert decision.outcome is PromotionOutcome.PROMOTED

    value = stored(seeded, part, "capacitance")
    assert value is not None
    assert value.value_nominal == pytest.approx(22e-6)
    assert value.value_min == pytest.approx(22e-6)
    assert value.value_max == pytest.approx(22e-6)
    assert value.value_min is not None and value.value_max is not None

    # And the row is actually reachable by a range query, which is the property
    # the two columns exist to provide.
    hit = seeded.execute(
        select(ParameterValue.id).where(
            ParameterValue.template_id == template(seeded, "capacitance").id,
            ParameterValue.value_min <= 30e-6,
            ParameterValue.value_max >= 20e-6,
        )
    ).scalar_one_or_none()
    assert hit == value.id


def test_a_promoted_enum_facet_lands_as_a_choice(seeded: Session, part: Part) -> None:
    """Enum facets take the same path, via `choice_id`, so search, provenance
    and review keep one code path rather than three."""
    decision = candidates.submit(
        seeded,
        part,
        template(seeded, "package"),
        "1608",
        source=Provenance.MPN_DECODER,
        confidence=0.9,
    )
    assert decision.outcome is PromotionOutcome.PROMOTED
    value = stored(seeded, part, "package")
    assert value is not None
    assert value.choice_id is not None


def test_two_sources_using_different_package_conventions_agree(seeded: Session, part: Part) -> None:
    """`0603` and `1608` are one part in two conventions.

    Alias resolution collapses them onto one `parameter_choice`, so agreement
    between the two spellings is exact and no fuzziness is involved.
    """
    package = template(seeded, "package")
    candidates.record(
        seeded, part, package, "0603", source=Provenance.DISTRIBUTOR_FREETEXT, confidence=0.4
    )
    decision = candidates.submit(
        seeded, part, package, "1608", source=Provenance.MPN_DECODER, confidence=0.4
    )

    assert decision.outcome is PromotionOutcome.PROMOTED
    assert candidates.pending(seeded, part=part) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerunning_the_same_extraction_accumulates_nothing(seeded: Session, part: Part) -> None:
    """The uniqueness rule is per *observation*, not per field.

    Three nightly runs over the same datasheet are one row. If they were three,
    the agreement rule would see three agreeing "sources" and promote a value on
    the strength of one observation counted thrice.
    """
    capacitance = template(seeded, "capacitance")
    for _ in range(3):
        candidates.record(
            seeded,
            part,
            capacitance,
            "100nF",
            source=Provenance.DATASHEET_TABLE,
            confidence=0.5,
            source_ref="sha256:abc",
        )

    rows = _all_candidates(seeded, part, capacitance)
    assert len(rows) == 1
    # And one observation is still one source, so the 0.8 bar still applies.
    decision = candidates.evaluate(seeded, part, capacitance)
    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.LOW_CONFIDENCE


def test_two_sources_are_two_rows_even_for_the_same_field(seeded: Session, part: Part) -> None:
    capacitance = template(seeded, "capacitance")
    candidates.record(
        seeded, part, capacitance, "100nF", source=Provenance.DATASHEET_TABLE, confidence=0.5
    )
    candidates.record(
        seeded, part, capacitance, "104nF", source=Provenance.LLM_INFERRED, confidence=0.5
    )
    assert len(_all_candidates(seeded, part, capacitance)) == 2


def test_two_documents_from_one_source_are_two_rows(seeded: Session, part: Part) -> None:
    """`source_ref` is what makes two revisions of a datasheet comparable
    instead of one silently overwriting the other."""
    capacitance = template(seeded, "capacitance")
    candidates.record(
        seeded,
        part,
        capacitance,
        "100nF",
        source=Provenance.DATASHEET_TABLE,
        confidence=0.5,
        source_ref="sha256:rev-a",
    )
    candidates.record(
        seeded,
        part,
        capacitance,
        "220nF",
        source=Provenance.DATASHEET_TABLE,
        confidence=0.5,
        source_ref="sha256:rev-b",
    )
    assert len(_all_candidates(seeded, part, capacitance)) == 2


def test_a_dismissal_sticks_across_a_rerun(seeded: Session, part: Part) -> None:
    capacitance = template(seeded, "capacitance")
    row = candidates.record(
        seeded,
        part,
        capacitance,
        "1F",
        source=Provenance.LLM_INFERRED,
        confidence=0.99,
        source_ref="sha256:abc",
    )
    candidates.dismiss(seeded, row)

    candidates.record(
        seeded,
        part,
        capacitance,
        "1F",
        source=Provenance.LLM_INFERRED,
        confidence=0.99,
        source_ref="sha256:abc",
    )
    assert row.status == CandidateStatus.DISMISSED
    assert candidates.evaluate(seeded, part, capacitance).outcome is (
        PromotionOutcome.NOTHING_PENDING
    )
    assert stored(seeded, part, "capacitance") is None


def test_a_changed_value_from_the_same_source_reopens_the_row(seeded: Session, part: Part) -> None:
    """A dismissal attaches to a value, not to a source forever.

    The same wrong number must stay dismissed on every re-run; a genuinely
    different number — a re-extraction with a better model — deserves a look.
    """
    capacitance = template(seeded, "capacitance")
    row = candidates.record(
        seeded,
        part,
        capacitance,
        "1F",
        source=Provenance.LLM_INFERRED,
        confidence=0.99,
        source_ref="sha256:abc",
    )
    candidates.dismiss(seeded, row)

    candidates.record(
        seeded,
        part,
        capacitance,
        "100nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.99,
        source_ref="sha256:abc",
    )
    assert row.status == CandidateStatus.PENDING
    assert len(_all_candidates(seeded, part, capacitance)) == 1


# ---------------------------------------------------------------------------
# Refusals that are not errors
# ---------------------------------------------------------------------------


def test_an_unparseable_value_is_kept_as_a_queue_item(seeded: Session, part: Part) -> None:
    """Megafarads are not real, and the string is still the asset.

    The template's plausibility window rejects it, and the raw text is stored
    anyway: an unparseable value is a grammar gap, a unit misread or a bad
    extraction, and only diagnosable if the bytes survived.
    """
    decision = candidates.submit(
        seeded,
        part,
        template(seeded, "capacitance"),
        "1MF",
        source=Provenance.DISTRIBUTOR_FREETEXT,
        confidence=0.99,
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.UNPARSEABLE
    assert stored(seeded, part, "capacitance") is None
    queued = candidates.pending(seeded, part=part)
    assert queued[0].raw_value == "1MF"
    assert queued[0].note is not None


def test_an_unknown_choice_key_is_kept_as_a_queue_item(seeded: Session, part: Part) -> None:
    decision = candidates.submit(
        seeded,
        part,
        template(seeded, "package"),
        "TOTALLY-NOT-A-PACKAGE",
        source=Provenance.LLM_INFERRED,
        confidence=0.99,
    )
    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.UNPARSEABLE
    assert stored(seeded, part, "package") is None


def test_a_text_template_is_refused_rather_than_stranded(seeded: Session, part: Part) -> None:
    """`app.services.parameters` has no writer for text or bool values.

    Accepting a candidate that could never be promoted would strand rows in the
    queue forever, or tempt a second write path onto `parameter_value` — and a
    second write path is how the `value_min`/`value_max` guarantee stops being
    one.
    """
    text_template = ParameterTemplate(
        name="marking_text",
        display_name="Top marking",
        value_type="text",
    )
    seeded.add(text_template)
    seeded.flush()

    with pytest.raises(candidates.UnsupportedTemplateType):
        candidates.record(
            seeded,
            part,
            text_template,
            "LM317",
            source=Provenance.LLM_INFERRED,
            confidence=0.9,
        )


# ---------------------------------------------------------------------------
# Rule 1: what must never auto-promote
# ---------------------------------------------------------------------------


def test_a_marking_decode_never_auto_promotes(seeded: Session, part: Part) -> None:
    """`104` is 100 kΩ on a resistor and 100 nF on a capacitor.

    The digits cannot tell you which component is in your hand, so a marking
    decode is flagged and queued whatever its confidence — the same rule as
    "never auto-accept an OCR'd or model-read part number".
    """
    decoded = decode("104")
    assert decoded is not None
    assert decoded.is_marking

    recorded = candidates.record_decoded_part(seeded, part, decoded, confidence=1.0)
    assert recorded
    decision = candidates.evaluate(seeded, part, template(seeded, "resistance"))

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.REQUIRES_HUMAN
    assert stored(seeded, part, "resistance") is None
    assert recorded[0].requires_human is True
    assert recorded[0].note is not None
    assert "marking" in recorded[0].note


def test_a_real_mpn_decode_promotes_and_carries_its_bounds(seeded: Session, part: Part) -> None:
    """The decoders' whole value: a fresh install is useful with no provider.

    `GRM188R71H104KA93D` yields a capacitance, a voltage rating, a dielectric and
    a size from the string alone — and because the decoder's values carry their
    units, they land through `parameters.set_numeric` with bounds populated.
    """
    decoded = decode("GRM188R71H104KA93D")
    assert decoded is not None
    assert not decoded.is_marking

    recorded = candidates.record_decoded_part(seeded, part, decoded)
    assert {row.source for row in recorded} == {Provenance.MPN_DECODER}
    assert {row.source_ref for row in recorded} == {decoded.family}

    for row in recorded:
        candidate_template = seeded.get(ParameterTemplate, row.template_id)
        assert candidate_template is not None
        candidates.evaluate(seeded, part, candidate_template)

    capacitance = stored(seeded, part, "capacitance")
    assert capacitance is not None
    assert capacitance.value_nominal == pytest.approx(100e-9)
    # The tolerance letter K is ±10%, so the stored interval is the band.
    assert capacitance.value_min == pytest.approx(90e-9)
    assert capacitance.value_max == pytest.approx(110e-9)
    assert capacitance.provenance == Provenance.MPN_DECODER


def test_a_decoded_template_this_install_lacks_is_skipped(db: Session) -> None:
    """No templates seeded at all: the decode is dropped, not invented.

    Templates are user-curated content and a decoder must not create one. The
    decode is a pure function of the part number, so re-running it once the
    template exists loses nothing.
    """
    part = make_part(db)
    decoded = decode("GRM188R71H104KA93D")
    assert decoded is not None
    assert decoded.parameters

    assert candidates.record_decoded_part(db, part, decoded) == ()


def test_a_partial_decode_names_what_it_did_not_understand(seeded: Session, part: Part) -> None:
    """Showing *what* was not understood is the point of `unknown`."""
    decoded = DecodedPart(
        family="test_family",
        parameters={"capacitance": "100nF"},
        unknown=("dielectric", "voltage_rating"),
    )
    recorded = candidates.record_decoded_part(seeded, part, decoded, confidence=0.5)

    assert len(recorded) == 1
    assert recorded[0].note is not None
    assert "dielectric" in recorded[0].note
    assert "voltage_rating" in recorded[0].note


# ---------------------------------------------------------------------------
# The queue itself
# ---------------------------------------------------------------------------


def test_the_queue_is_exactly_the_pending_rows(seeded: Session, part: Part) -> None:
    capacitance = template(seeded, "capacitance")
    promoted = candidates.submit(
        seeded, part, capacitance, "100nF", source=Provenance.DATASHEET_TABLE, confidence=0.95
    )
    assert promoted.outcome is PromotionOutcome.PROMOTED
    assert candidates.pending(seeded) == []

    candidates.submit(
        seeded,
        part,
        template(seeded, "voltage_rating"),
        "50V",
        source=Provenance.LLM_INFERRED,
        confidence=0.2,
    )
    queue = candidates.pending(seeded)
    assert [row.template_id for row in queue] == [template(seeded, "voltage_rating").id]
    assert candidates.pending(seeded, limit=0) == []


def test_evaluating_a_field_with_no_candidates_does_nothing(seeded: Session, part: Part) -> None:
    decision = candidates.evaluate(seeded, part, template(seeded, "capacitance"))
    assert decision.outcome is PromotionOutcome.NOTHING_PENDING
    assert decision.promoted is None
    assert not decision.needs_review


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_candidates(
    db: Session, part: Part, tmpl: ParameterTemplate
) -> list[ParameterValueCandidate]:
    return list(
        db.execute(
            select(ParameterValueCandidate).where(
                ParameterValueCandidate.part_id == part.id,
                ParameterValueCandidate.template_id == tmpl.id,
            )
        ).scalars()
    )


def _candidate(
    db: Session, part: Part, tmpl: ParameterTemplate, source: Provenance
) -> ParameterValueCandidate:
    return db.execute(
        select(ParameterValueCandidate).where(
            ParameterValueCandidate.part_id == part.id,
            ParameterValueCandidate.template_id == tmpl.id,
            ParameterValueCandidate.source == source,
        )
    ).scalar_one()
