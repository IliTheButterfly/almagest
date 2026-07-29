"""Regressions for the four defects adversarial review found in Phase 6.

Every one of them broke the same promise from two directions: **enrichment never
writes `parameter_value` unless a rule or a human said it could.** Not one would
have raised, logged, or reddened a test — the whole point of the candidate table
is that a wrong value in `parameter_value` is indistinguishable from a right one,
so a promotion that should not have happened is only discovered when a board does
not work.

Three shapes worth naming, because the next automated write path will have the
same opportunities:

* **a count of rows standing in for a count of independent facts.** Rule 2 —
  "agreement between sources promotes without consulting the 0.8 bar" — was
  implemented as `len(agreeing) > 1` over *rows*. Uniqueness on
  `parameter_value_candidate` is `(part, template, source, source_ref)`
  specifically so one source can hold several observations of a field, so a
  single `llm_inferred` reading at confidence 0.10 corroborated itself the moment
  a second datasheet revision produced it again, and wrote itself into the
  catalogue with no human anywhere in the loop.
* **a human's decision undone by a cosmetic difference.** A `DISMISSED` row
  reopened on `row.raw_value != raw_value`, a *string* comparison, so the same
  source respelling `100nF` as `0.1uF` — the exact equivalence this table
  normalises on write in order to see through — reopened the dismissal and
  handed the value straight back to the promotion rules. A "no" reverted with
  nobody present.
* **a null-bounded numeric row, which is the one failure this repo names in
  `CLAUDE.md`, `parameters.py` and `candidates.py` alike.** `>=50V` parses to a
  half-open interval; `set_numeric` stored it verbatim and the accept/correct
  routes admitted it because `value_min is not None`. The part then matched *no*
  range query at all while still displaying a voltage rating. Its mirror
  (`<=100nF`, which fills `value_max` instead) exposed two more bugs on the way:
  the usability check tested only `value_min`, so the two halves of one syntax
  were classed differently, and `_agree` was not reflexive on a one-sided value,
  so a lone candidate was partitioned as its own dissenter and reported to the
  reviewer as `sources_disagree` — two sources conflicting, where one existed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.catalog import Part
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import (
    CandidateReviewReason,
    CandidateStatus,
    PromotionOutcome,
    Provenance,
)
from app.models.parameter import ParameterTemplate
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services import parameters
from app.services.enrichment import candidates
from tests.factories import make_part


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_categories(db)
    seed_parameter_templates(db)
    return db


def _template(session: Session, name: str) -> ParameterTemplate:
    return session.execute(
        select(ParameterTemplate).where(ParameterTemplate.name == name)
    ).scalar_one()


def _value_rows(session: Session) -> list[tuple[object, ...]]:
    """`parameter_value` read with raw SQL.

    Deliberately not through `app.services.parameters` or the ORM's identity map:
    the defect these tests exist for is what ends up *in the table*, and a helper
    that re-derives anything could paper over a column that was never written.
    """
    return [
        tuple(row)
        for row in session.execute(
            text("SELECT raw_input, value_nominal, value_min, value_max FROM parameter_value")
        )
    ]


# ---------------------------------------------------------------------------
# 1. Rule 2 counted observations, not distinct sources
# ---------------------------------------------------------------------------


def test_one_source_reporting_twice_does_not_clear_the_confidence_bar(seeded: Session) -> None:
    """The defect: two `llm_inferred` rows under different `source_ref`s, both at
    confidence 0.10, auto-promoted into `parameter_value`.

    `evaluate()` partitioned `eligible` into `agreeing`/`dissenting` and then
    applied the 0.8 bar only when `len(agreeing) == 1`. Nothing checked that the
    agreeing rows came from different `source`. Two revisions of one datasheet, or
    a family PDF plus a package PDF, is a **designed-for** case — the uniqueness
    key includes `source_ref` for exactly that — so the nightly re-extraction was
    all it took: one model reading at 0.10 confidence became a catalogue value by
    turning up twice.

    Rule 2's own wording is "agreement between two **independent** sources". Two
    documents read by the same model are the correlated pair it rules out, so the
    count has to be over `{row.source}`.
    """
    part = make_part(seeded, name="two datasheet revisions", mpn="PHASE6-SELF-CORROBORATE")
    capacitance = _template(seeded, "capacitance")

    candidates.record(
        seeded,
        part,
        capacitance,
        "100nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.10,
        source_ref="sha256:revision-a",
    )
    decision = candidates.submit(
        seeded,
        part,
        capacitance,
        # The same number, respelled — so `_agree` says yes and the old code
        # counted two "agreeing sources".
        "0.1uF",
        source=Provenance.LLM_INFERRED,
        confidence=0.10,
        source_ref="sha256:revision-b",
    )

    assert decision.outcome is PromotionOutcome.QUEUED
    assert decision.reason is CandidateReviewReason.LOW_CONFIDENCE
    assert _value_rows(seeded) == []
    # Both rows stay pending and both carry the reason: a row left `PENDING` with
    # a NULL `review_reason` is unexplainable in the queue and matches no filter.
    pending = candidates.pending(seeded, part=part)
    assert len(pending) == 2
    assert {row.review_reason for row in pending} == {CandidateReviewReason.LOW_CONFIDENCE}


def test_two_genuinely_different_sources_agreeing_still_promote_below_the_bar(
    seeded: Session,
) -> None:
    """The counterweight, and the reason the fix is a `set` and not a ban on
    multiple rows.

    Rule 2 is the whole argument for enrichment being worth running: two sources
    that fail for unrelated reasons landing on the same number is stronger
    evidence than either one's self-report, so it promotes *without* consulting
    0.8. Narrowing the agreement count to distinct sources must not quietly turn
    rule 2 off — that would send every corroborated field to the queue and the
    queue is the thing that does not scale.
    """
    part = make_part(seeded, name="two independent readings", mpn="PHASE6-TWO-SOURCES")
    capacitance = _template(seeded, "capacitance")

    candidates.record(
        seeded,
        part,
        capacitance,
        "100nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.30,
    )
    decision = candidates.submit(
        seeded,
        part,
        capacitance,
        "0.1uF",
        source=Provenance.DISTRIBUTOR_FREETEXT,
        confidence=0.30,
    )

    assert decision.outcome is PromotionOutcome.PROMOTED
    assert decision.promoted is not None
    # The more trusted of the two is the one recorded, not the one that arrived
    # last.
    assert decision.promoted.source == Provenance.DISTRIBUTOR_FREETEXT
    (row,) = _value_rows(seeded)
    assert row[1] == pytest.approx(100e-9)


# ---------------------------------------------------------------------------
# 2. A dismissal was undone by a respelling
# ---------------------------------------------------------------------------


def test_a_dismissal_survives_the_same_value_spelled_differently(seeded: Session) -> None:
    """The defect: dismiss a `100nF` reading, and the same source resubmitting it
    as `0.1uF` reopened the row *and* auto-promoted it.

    The reopen test was `row.raw_value != raw_value or not _agree(...)`. The
    string half of that `or` fires on any cosmetic difference, and `100nF` versus
    `0.1uF` is precisely the case the module's own comments cite as the same
    number written two ways — it is why values are normalised on write at all.
    The existing `test_a_dismissal_sticks_across_a_rerun` passed only because it
    resent the byte-identical string, which is not what a re-extraction of a
    revised datasheet produces.

    A dismissal attaches to a *value* a human rejected. Reverting it with no
    human present is the worst version of this module's failure mode: the queue
    still looks worked, and the value nobody accepted is in the catalogue.
    """
    part = make_part(seeded, name="respelled after dismissal", mpn="PHASE6-RESPELL")
    capacitance = _template(seeded, "capacitance")

    row = candidates.record(
        seeded,
        part,
        capacitance,
        "100nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.95,
    )
    candidates.dismiss(seeded, row)
    assert row.status == CandidateStatus.DISMISSED

    decision = candidates.submit(
        seeded,
        part,
        capacitance,
        "0.1uF",
        source=Provenance.LLM_INFERRED,
        confidence=0.95,
    )

    assert row.status == CandidateStatus.DISMISSED
    assert decision.outcome is PromotionOutcome.NOTHING_PENDING
    assert _value_rows(seeded) == []


def test_a_genuinely_new_number_from_that_source_still_reopens(seeded: Session) -> None:
    """The other half of the rule, which the fix must not lose.

    A dismissal is not a permanent ban on the `(part, template, source)` triple:
    a source that comes back with a *different* number deserves a fresh look,
    because what was rejected was one reading and not the source. Only the
    spelling stopped counting as a difference.
    """
    part = make_part(seeded, name="corrected upstream", mpn="PHASE6-NEW-NUMBER")
    capacitance = _template(seeded, "capacitance")

    row = candidates.record(
        seeded,
        part,
        capacitance,
        "100nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.95,
    )
    candidates.dismiss(seeded, row)

    candidates.record(
        seeded,
        part,
        capacitance,
        "220nF",
        source=Provenance.LLM_INFERRED,
        confidence=0.95,
    )
    assert row.status == CandidateStatus.PENDING
    assert row.review_reason is None


def test_an_unparseable_dismissal_does_not_reopen_on_a_rerun(seeded: Session) -> None:
    """The case the string comparison was masking, and why the fix is not simply
    `not _agree(...)`.

    `_agree` is deliberately `False` for two rows that normalised to nothing —
    "I could not compare these" must never read as "these are the same". So a
    dismissed *unparseable* row would reopen on every nightly re-run of the
    identical extraction, resurrecting the same dismissal forever. When neither
    side normalised, the raw text is the only evidence there is, so that is what
    decides.
    """
    part = make_part(seeded, name="unparseable rerun", mpn="PHASE6-UNPARSEABLE-RERUN")
    capacitance = _template(seeded, "capacitance")

    row = candidates.record(
        seeded,
        part,
        capacitance,
        "about a tenth of a microfarad",
        source=Provenance.LLM_INFERRED,
        confidence=0.95,
    )
    assert row.review_reason == CandidateReviewReason.UNPARSEABLE
    candidates.dismiss(seeded, row)

    candidates.record(
        seeded,
        part,
        capacitance,
        "about a tenth of a microfarad",
        source=Provenance.LLM_INFERRED,
        confidence=0.95,
    )
    assert row.status == CandidateStatus.DISMISSED


# ---------------------------------------------------------------------------
# 3. A one-sided limit stored a null bound, and vanished from range search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template_name", "raw_value"),
    [
        ("voltage_rating", ">=50V"),
        ("voltage_rating", "<100V"),
        ("capacitance", ">1uF"),
        ("capacitance", "<=100nF"),
        ("resistance", ">=1k"),
    ],
)
def test_set_numeric_refuses_a_one_sided_limit(
    seeded: Session, template_name: str, raw_value: str
) -> None:
    """The defect: `set_numeric` stored `>=50V` as `(value_min=50.0,
    value_max=NULL)` and returned happily.

    This is the exact failure `CLAUDE.md`, `parameters.py`'s module docstring and
    `candidates.py`'s all name by name, reached through a supported grammar
    instead of through a bug: `ParsedValue.to_interval()` returns the half-open
    interval for `kind == "comparison"` on purpose, because a comparison is a
    perfectly good *query*. It is not a value a part *has*.

    Refused in `set_numeric` rather than at each caller, because that is the one
    door and this is the invariant the door exists for. The check reads the
    interval, not `kind`, so a future grammar addition that yields a half-open
    interval is refused the day it lands rather than the day someone notices a
    part missing from a search.

    Both halves are parametrized: the earlier usability check tested only
    `value_min`, so `>=50V` and `<=100nF` were treated as different kinds of
    thing for no reason beyond which column the parser happened to fill.
    """
    part = make_part(seeded, name=f"one-sided {raw_value}", mpn=f"PHASE6-ONESIDED-{raw_value}")
    template = _template(seeded, template_name)

    with pytest.raises(parameters.UnboundedValue):
        parameters.set_numeric(seeded, part, template, raw_value)

    # And nothing half-written: the refusal happens before the row is created, so
    # the caller's next flush cannot emit a blank `ParameterValue`.
    seeded.flush()
    assert _value_rows(seeded) == []


def test_a_one_sided_candidate_queues_with_a_reason_and_never_promotes(seeded: Session) -> None:
    """The defect, at the promotion layer: a lone `>=50V` candidate was queued as
    `SOURCES_DISAGREE`, and its mirror `<=100nF` was queued with **no reason at
    all**.

    Three bugs met here. `_agree` required all four endpoints to be non-NULL, so
    a one-sided value did not agree with *itself*: `evaluate()` computed
    `agreeing = []`, `dissenting = [best]`, and took the disagreement branch —
    telling the reviewer two sources conflicted when one existed, on the reason
    the enum documents as "the single strongest signal that a human should look".
    The usability check tested `value_min` only, so `<=100nF` was instead classed
    unusable and fell through to `_first_reason([])` → NULL, in a column
    documented as "why it is still pending" and meant to be filterable.

    Now: both halves are unusable, symmetrically, both carry
    `ONE_SIDED_LIMIT`, and the note tells the reviewer to type a value or a range
    rather than merely that the row was refused.
    """
    part = make_part(seeded, name="lone one-sided reading", mpn="PHASE6-LONE-LIMIT")

    for template_name, raw_value in (("voltage_rating", ">=50V"), ("capacitance", "<=100nF")):
        template = _template(seeded, template_name)
        decision = candidates.submit(
            seeded,
            part,
            template,
            raw_value,
            source=Provenance.DATASHEET_TABLE,
            confidence=0.95,
        )
        assert decision.outcome is PromotionOutcome.QUEUED
        assert decision.reason is CandidateReviewReason.ONE_SIDED_LIMIT
        (queued,) = [row for row in decision.queued if row.raw_value == raw_value]
        assert queued.status == CandidateStatus.PENDING
        assert queued.review_reason == CandidateReviewReason.ONE_SIDED_LIMIT
        assert queued.note is not None and "range" in queued.note
        assert not candidates.is_promotable(queued)

    assert _value_rows(seeded) == []


def test_a_one_sided_value_agrees_with_itself(seeded: Session) -> None:
    """`compare_raw` is the documented single entry point to the agreement rule,
    and it said `>=50V` differed from `>=50V`.

    Not reachable through a decoder today — none in `mpn_decoders/` emits `>=` —
    but `cross_check` turns a `False` here into `CrossCheckVerdict.CONFLICT` and
    a confidence ceiling, so the first decoder that transcribes an "at least"
    from a datasheet would have its own agreement recorded as a conflict.
    Reflexivity is also what makes "a lone candidate is never `SOURCES_DISAGREE`"
    structural rather than a case someone remembered to handle: `best` is always
    in `agreeing`, so `dissenting` can only be non-empty when a second row
    exists.

    A missing endpoint matches only a missing endpoint, so `>=50V` still does not
    agree with `50V` or with `<=50V` — treating NULL as a wildcard is how a bound
    would come to confirm a value.
    """
    voltage = _template(seeded, "voltage_rating")

    assert candidates.compare_raw(seeded, voltage, ">=50V", ">=50V") is True
    assert candidates.compare_raw(seeded, voltage, ">=50V", "50V") is False
    assert candidates.compare_raw(seeded, voltage, ">=50V", "<=50V") is False
    assert candidates.compare_raw(seeded, voltage, ">=50V", ">=60V") is False
    # Unparseable stays `None`, not `True`: "could not compare" and "the same"
    # want opposite handling downstream.
    assert candidates.compare_raw(seeded, voltage, "fifty-ish volts", "fifty-ish volts") is None


# ---------------------------------------------------------------------------
# 3b. …reached through the routes a reviewer actually clicks
# ---------------------------------------------------------------------------


def _seeded_part_via_api(name: str, mpn: str) -> tuple[Session, Part]:
    """A committed part in the database `client` reads, the shape
    `test_enrichment_api.py` uses."""
    session = get_session_factory()()
    seed_categories(session)
    seed_parameter_templates(session)
    part = make_part(session, name=name, mpn=mpn)
    session.commit()
    return session, part


def test_accept_refuses_a_one_sided_candidate_instead_of_storing_a_null_bound(
    client: TestClient,
) -> None:
    """The defect end-to-end: `POST /accept` returned 200 and wrote
    `(value_min=50.0, value_max=NULL)`; `POST /bulk-accept` reported
    `accepted: true` for the same row.

    The route gate was a private copy of the usability check (`value_min is not
    None`), which admitted every `>=`-shaped reading. It now delegates to
    `candidates.is_promotable`, so there is one predicate rather than two to
    drift apart — the drift would have shown up as a 500 from a button this
    screen offered.
    """
    session, part = _seeded_part_via_api("accept a limit", "PHASE6-ACCEPT-LIMIT")
    row = candidates.record(
        session,
        part,
        _template(session, "voltage_rating"),
        ">=50V",
        source=Provenance.LLM_INFERRED,
        confidence=0.40,
    )
    session.commit()
    candidate_id = row.id

    response = client.post(f"/api/enrichment/candidates/{candidate_id}/accept")
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "one_sided_limit"

    bulk = client.post(
        "/api/enrichment/candidates/bulk-accept", json={"candidate_ids": [candidate_id]}
    )
    assert bulk.status_code == 200
    assert bulk.json()["results"] == [
        {"candidate_id": candidate_id, "accepted": False, "reason": "one_sided_limit"}
    ]

    session.expire_all()
    assert _value_rows(session) == []
    assert session.get(ParameterValueCandidate, candidate_id).status == CandidateStatus.PENDING


def test_correct_refuses_a_one_sided_replacement_and_leaves_no_manual_row(
    client: TestClient,
) -> None:
    """The same defect through the correction box, which is where a human is
    *most* likely to type it: `FilterIn.value`'s own docstring advertises `>=`
    syntax, for searching. `POST /correct` accepted `>=50V` and stored it as a
    `manual` value at confidence 1.0 — the most trusted row in the system,
    invisible to every range query.

    The refusal must also leave nothing behind. `/correct` writes the `manual`
    candidate *before* it can check whether the value is storable, so the 422 has
    to take the request's rollback with it or the queue accumulates a phantom
    `manual` row per rejected keystroke.
    """
    session, part = _seeded_part_via_api("correct with a limit", "PHASE6-CORRECT-LIMIT")
    row = candidates.record(
        session,
        part,
        _template(session, "voltage_rating"),
        "16V",
        source=Provenance.LLM_INFERRED,
        confidence=0.40,
    )
    session.commit()
    candidate_id = row.id
    before = session.execute(select(ParameterValueCandidate)).scalars().all()

    response = client.post(
        f"/api/enrichment/candidates/{candidate_id}/correct", json={"raw_value": ">=50V"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "one_sided_limit"
    assert "range" in detail["message"]

    session.expire_all()
    assert _value_rows(session) == []
    assert len(session.execute(select(ParameterValueCandidate)).scalars().all()) == len(before)

    # A two-sided correction of the same field goes through, so the refusal is a
    # redirection and not a dead end.
    ok = client.post(
        f"/api/enrichment/candidates/{candidate_id}/correct", json={"raw_value": "50V"}
    )
    assert ok.status_code == 200
    session.expire_all()
    (stored,) = _value_rows(session)
    assert stored[2] == pytest.approx(50.0)
    assert stored[3] == pytest.approx(50.0)


def test_an_accepted_value_stays_visible_to_the_range_query_that_should_find_it(
    client: TestClient,
) -> None:
    """The consequence the other tests are protecting against, asserted against
    the real query builder rather than reasoned about.

    With `>=50V` storable, two parts — one accepted as `50V`, one as `>=50V` —
    both displayed a voltage rating and only one appeared in a search for
    40–100 V. That is the silent half of the defect: nothing is missing from any
    part page, only from every result set.
    """
    session, part = _seeded_part_via_api("scalar 50V", "PHASE6-SEARCH-SCALAR")
    voltage = _template(session, "voltage_rating")
    row = candidates.record(
        session,
        part,
        voltage,
        "50V",
        source=Provenance.DATASHEET_TABLE,
        confidence=0.40,
    )
    session.commit()

    assert client.post(f"/api/enrichment/candidates/{row.id}/accept").status_code == 200

    found = client.post(
        "/api/search/parts",
        json={"filters": [{"template": "voltage_rating", "value": "40-100V"}]},
    ).json()
    assert [result["mpn"] for result in found["results"]] == ["PHASE6-SEARCH-SCALAR"]
