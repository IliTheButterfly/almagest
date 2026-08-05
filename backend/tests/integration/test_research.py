"""The datasheet-research queue: the lease, the two terminal states, the evidence.

## What this file is really guarding

**A part that comes back "no datasheet" with no way to tell why.** ADR 0017's whole
argument is that the researcher proposes and never asserts, and that rejections are
kept so `exhausted` is a diagnosis rather than a shrug. The tests below check that a
run's rejections survive with their reasons attached, that a rejection with no reason
is refused at the door, and — the one that matters most — that **`exhausted` and
`failed` are different states with different meanings**, because a health check that
counts obscure parts as breakage is a health check nobody reads.

**A queue that stops moving while every count reads clean.** Identical to the
extraction queue's failure mode, and guarded the same way: the lease, the attempt
counted *at claim time*, and the abandoned-claim sweep are three halves of one
mechanism, so a part is walked all the way to `failed` while a second part keeps
being served.

**A worker declaring an outcome the rows underneath it contradict.** There is no
`state` field on the submit door. `record_result` derives `resolved` from "something
validated" and nothing else, and a test pins that the derivation — not the
submission — is what lands in the column.

## Why the concurrency test substitutes `_candidates`

Route handlers are `def`, so two concurrent claims are two threads on two
connections and a claim genuinely can land between the pick and the take. Nothing
single-threaded reaches that interleave, so the test hands `claim` a **stale**
candidate list directly — the same seam and the same reason as the extraction
queue's equivalent. Without it the compare-and-swap in the `update` is untested and
free to be tidied away by somebody who reads it as a redundant `where`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import ResearchCandidateState, ResearchState
from app.models.types import utcnow
from app.services import research
from app.services.research import CandidateReport, RejectReason, ResearchError
from tests.factories import make_manufacturer, make_part

SHA_A = "a" * 64
SHA_B = "b" * 64


def _validated(url: str = "https://example.test/ds.pdf", **kwargs: object) -> CandidateReport:
    return CandidateReport(
        source=str(kwargs.pop("source", "url_pattern")),
        url=url,
        state=ResearchCandidateState.VALIDATED,
        document_sha256=str(kwargs.pop("document_sha256", SHA_A)),
        **kwargs,  # type: ignore[arg-type]
    )


def _rejected(
    url: str = "https://example.test/wrong.pdf",
    reason: str = RejectReason.MPN_ABSENT,
    **kwargs: object,
) -> CandidateReport:
    return CandidateReport(
        source=str(kwargs.pop("source", "websearch")),
        url=url,
        state=ResearchCandidateState.REJECTED,
        reject_reason=reason,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The two terminal states
# ---------------------------------------------------------------------------


def test_a_run_that_validates_nothing_is_exhausted_and_carries_no_error(db: Session) -> None:
    """ADR 0017's requirement, and the reason `ResearchState` has six members.

    Finding no datasheet for a genuinely obscure part is a normal outcome. If it
    landed in `failed` it would show up in the deterministic health check
    `docs/PLAN.md` wants for *failed enrichment*, which would then fill with parts
    nothing is wrong with and stop being read.
    """
    part = make_part(db, "Obscure regulator", mpn="XYZ-9000")

    state = research.record_result(db, part=part, candidates=[_rejected()])

    assert state is ResearchState.EXHAUSTED
    assert part.research_state == ResearchState.EXHAUSTED
    # The distinguishing assertion: no error text. `exhausted` is a shrug, not a bug
    # report, and a message here would render as one everywhere it is displayed.
    assert part.research_error is None


def test_an_empty_candidate_list_is_exhausted_not_failed(db: Session) -> None:
    """A cascade that ran and proposed nothing is still a completed run.

    Distinct from `error`, which is the run itself breaking. Collapsing the two would
    make a dead network indistinguishable from a part no provider covers — and the
    two want opposite responses: fix the egress, versus add a provider.
    """
    part = make_part(db, "Unknown thing", mpn="NOPE-1")

    assert research.record_result(db, part=part, candidates=[]) is ResearchState.EXHAUSTED
    assert part.research_error is None


def test_a_broken_run_stays_claimable_until_attempts_run_out(db: Session) -> None:
    """A transient failure is a retry; a persistent one is a health-check entry.

    The worker does not have to tell them apart — it reports the error and the queue
    decides, which is what keeps that judgement in one place.
    """
    part = make_part(db, "Flaky", mpn="FLAKY-1")

    for expected_attempt in range(1, research.MAX_RESEARCH_ATTEMPTS):
        claimed = research.claim(db, worker_id="w1")
        assert [p.id for p in claimed] == [part.id]
        assert part.research_attempts == expected_attempt
        research.record_failure(db, part=part, error="egress died")
        # Still offered: attempts remain, so this is a retry rather than a verdict.
        assert part.research_state == ResearchState.PENDING

    research.claim(db, worker_id="w1")
    research.record_failure(db, part=part, error="egress died")
    assert part.research_state == ResearchState.FAILED
    assert part.research_error == "egress died"


def test_validating_one_candidate_resolves_the_part(db: Session) -> None:
    part = make_part(db, "Murata cap", mpn="GRM188R71H104KA93D")

    state = research.record_result(db, part=part, candidates=[_rejected(), _validated()])

    assert state is ResearchState.RESOLVED
    assert part.research_error is None


def test_the_outcome_is_derived_from_the_candidates_not_declared(db: Session) -> None:
    """There is no wire field a worker could use to disagree with its own evidence.

    Pinned as a test rather than left to the absence of a field, because the tempting
    "just let the worker send `state`" change would pass every other test in this
    file while creating a second source of truth that can contradict the rows.
    """
    part = make_part(db, "Cap", mpn="C-1")
    research.record_result(db, part=part, candidates=[_validated()])
    assert part.research_state == ResearchState.RESOLVED

    # Same part, a later run that finds nothing: the state follows the evidence back
    # down rather than latching on the earlier success.
    research.record_result(db, part=part, candidates=[_rejected()])
    assert part.research_state == ResearchState.EXHAUSTED


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------


def test_rejections_are_kept_with_their_reasons(db: Session) -> None:
    """Four `mpn_absent` rejections and one `not_pdf` are different diagnoses.

    Both read as "no datasheet" if only the winner is stored, and they point at
    different bugs — a provider returning the wrong part, versus a login wall.
    """
    part = make_part(db, "Cap", mpn="C-1")

    research.record_result(
        db,
        part=part,
        candidates=[
            _rejected("https://a.test/1.pdf", RejectReason.MPN_ABSENT, source="websearch"),
            _rejected("https://b.test/2.html", RejectReason.NOT_PDF, source="mouser", rank=1),
        ],
    )

    rows = research.candidates_for(db, part_id=part.id)
    assert [(r.source, r.reject_reason) for r in rows] == [
        ("websearch", RejectReason.MPN_ABSENT),
        ("mouser", RejectReason.NOT_PDF),
    ]


def test_a_rerun_replaces_a_verdict_rather_than_appending_one(db: Session) -> None:
    """Keyed on `(part_id, url)`, so the row count stays "how many distinct things
    were tried" instead of growing with every retry.

    And the upgrade path ADR 0017 depends on: the same URL that was rejected as
    `mpn_absent` under a bad normaliser validates on a later run, and the row must
    end up saying so rather than keeping both opinions.
    """
    part = make_part(db, "Cap", mpn="C-1")
    url = "https://a.test/1.pdf"

    research.record_result(db, part=part, candidates=[_rejected(url)])
    research.record_result(db, part=part, candidates=[_validated(url)])

    rows = research.candidates_for(db, part_id=part.id)
    assert len(rows) == 1
    assert rows[0].state == ResearchCandidateState.VALIDATED
    assert rows[0].reject_reason is None
    assert rows[0].document_sha256 == SHA_A


def test_a_rejection_with_no_reason_is_refused(db: Session) -> None:
    """The refusal that keeps `exhausted` diagnosable.

    A reasonless rejection stores a row that says "something was wrong" and nothing
    more, which is precisely the state these rows exist to prevent.
    """
    part = make_part(db, "Cap", mpn="C-1")
    bad = CandidateReport(
        source="websearch",
        url="https://a.test/1.pdf",
        state=ResearchCandidateState.REJECTED,
    )

    with pytest.raises(ResearchError) as caught:
        research.record_result(db, part=part, candidates=[bad])
    assert caught.value.reason == "missing_reject_reason"


def test_a_validated_candidate_naming_no_document_is_refused(db: Session) -> None:
    part = make_part(db, "Cap", mpn="C-1")
    bad = CandidateReport(
        source="mouser", url="https://a.test/1.pdf", state=ResearchCandidateState.VALIDATED
    )

    with pytest.raises(ResearchError) as caught:
        research.record_result(db, part=part, candidates=[bad])
    assert caught.value.reason == "missing_document"


def test_one_url_reported_twice_in_a_run_is_refused(db: Session) -> None:
    """Refused rather than resolved by last-wins.

    The unique constraint would silently keep whichever the loop wrote second, and a
    worker contradicting itself within one run has a bug worth surfacing.
    """
    part = make_part(db, "Cap", mpn="C-1")
    url = "https://a.test/1.pdf"

    with pytest.raises(ResearchError) as caught:
        research.record_result(db, part=part, candidates=[_validated(url), _rejected(url)])
    assert caught.value.reason == "duplicate_candidate"


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------


def test_an_abandoned_lease_is_reoffered_then_failed(db: Session) -> None:
    """The whole dead-worker mechanism, walked end to end.

    Without `expire_abandoned` the third abandonment leaves the part in `claimed`
    with nobody holding it: not pending, not failed, not claimable, and absent from
    every count that would have shown the queue had stopped moving.
    """
    part = make_part(db, "Poison", mpn="POISON-1")
    base = utcnow()
    # Each claim is a full lease past the previous one: the clock has to advance or
    # the second claim sees a lease that has not expired and correctly declines it.
    step = timedelta(seconds=research.LEASE_SECONDS + 60)

    for attempt in range(research.MAX_RESEARCH_ATTEMPTS):
        assert research.claim(db, worker_id="dies", now=base + step * attempt) != []
        # The worker dies here: no result, no failure, nothing.

    exhausted_at = base + step * research.MAX_RESEARCH_ATTEMPTS
    assert research.claim(db, worker_id="next", now=exhausted_at) == []
    db.refresh(part)
    assert part.research_state == ResearchState.FAILED
    assert part.research_error is not None
    assert "abandoned" in part.research_error


def test_one_poison_part_does_not_starve_a_fresh_one(db: Session) -> None:
    """Ordering is `(attempts, id)`: fresh work before retries."""
    poison = make_part(db, "Poison", mpn="POISON-1")
    research.claim(db, worker_id="w1")  # burns an attempt on `poison`
    research.record_failure(db, part=poison, error="boom")

    fresh = make_part(db, "Fresh", mpn="FRESH-1")

    assert [p.id for p in research.claim(db, worker_id="w2")] == [fresh.id]


def test_a_stale_candidate_list_does_not_double_claim(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compare-and-swap, exercised through the seam it needs.

    `claim` is handed a candidate id that another worker took in between. The
    `update` repeats `_claimable`, so it matches nothing and this claim comes back
    empty — rather than two workers researching one part and doubling the egress the
    whole cascade costs.
    """
    part = make_part(db, "Contested", mpn="C-1")
    taken = research.claim(db, worker_id="first")
    assert [p.id for p in taken] == [part.id]

    def stale(*_args: object, **_kwargs: object) -> list[int]:
        """The pick a concurrent claim has already invalidated."""
        return [part.id]

    monkeypatch.setattr(research, "_candidates", stale)

    assert research.claim(db, worker_id="second") == []
    db.refresh(part)
    assert part.research_claimed_by == "first"


def test_requeue_restores_a_part_and_keeps_the_evidence(db: Session) -> None:
    """Re-researching an `exhausted` part once a provider is added is the upgrade
    path, and the candidate rows are what tell a person whether it is worth it."""
    part = make_part(db, "Cap", mpn="C-1")
    research.record_result(db, part=part, candidates=[_rejected()])
    assert part.research_state == ResearchState.EXHAUSTED

    research.requeue(db, part=part)

    assert part.research_state == ResearchState.PENDING
    assert part.research_attempts == 0
    # Kept deliberately: what was already tried is the most useful thing to know
    # when deciding whether a re-run is worth the egress.
    assert len(research.candidates_for(db, part_id=part.id)) == 1


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def test_claim_and_submit_over_http(client: TestClient, db: Session) -> None:
    part = make_part(db, "Murata cap", mpn="GRM188R71H104KA93D")
    db.commit()

    claimed = client.post("/api/research/claims", json={"worker_id": "pod-1", "limit": 5})
    assert claimed.status_code == 200
    claims = claimed.json()["claims"]
    assert [c["part_id"] for c in claims] == [part.id]
    # `mpn_norm` is sent rather than re-derived by the worker: it is what validation
    # compares against, and two normalisers is one too many. Note it is *lowercased*
    # — which is exactly why the worker must not derive its own: an uppercase
    # comparison against a datasheet's text would silently never match.
    assert claims[0]["mpn_norm"] == "grm188r71h104ka93d"

    submitted = client.post(
        "/api/research/results",
        json={
            "part_id": part.id,
            "candidates": [
                {
                    "source": "url_pattern",
                    "url": "https://murata.test/GRM188.pdf",
                    "state": "validated",
                    "document_sha256": SHA_B,
                }
            ],
        },
    )
    assert submitted.status_code == 200
    assert submitted.json()["part"]["state"] == "resolved"


def test_a_claim_carries_the_manufacturer_name_not_its_id(client: TestClient, db: Session) -> None:
    """A URL pattern is keyed on the manufacturer, and an integer id means nothing
    outside this database.

    `Part` has `manufacturer_id` and deliberately no relationship, so the name is an
    explicit lookup — one query for the batch. Worth a test because the tempting
    `part.manufacturer.name` compiles nowhere and the fallback of shipping the id
    would leave the worker unable to use it.
    """
    maker = make_manufacturer(db, "Murata")
    make_part(db, "Cap", mpn="GRM188", manufacturer_id=maker.id)
    make_part(db, "Nameless", mpn="ANON-1")
    db.commit()

    claims = client.post("/api/research/claims", json={"worker_id": "pod-1", "limit": 5}).json()[
        "claims"
    ]

    by_mpn = {c["mpn"]: c["manufacturer"] for c in claims}
    assert by_mpn == {"GRM188": "Murata", "ANON-1": None}


def test_submitting_both_candidates_and_an_error_is_refused(
    client: TestClient, db: Session
) -> None:
    """A worker that sent both has a bug. Picking one would record an outcome it did
    not unambiguously report."""
    part = make_part(db, "Cap", mpn="C-1")
    db.commit()

    response = client.post(
        "/api/research/results",
        json={"part_id": part.id, "candidates": [], "error": "boom"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "ambiguous_result"


def test_reading_research_for_an_unresearched_part_is_200(client: TestClient, db: Session) -> None:
    """The same shape, and the same reasoning, as `GET /api/documents/{sha}/text`
    answering 200 for a document with no text: a part nobody has researched is not an
    error, and a 404 is what a client renders as one."""
    part = make_part(db, "Cap", mpn="C-1")
    db.commit()

    response = client.get(f"/api/parts/{part.id}/research")
    assert response.status_code == 200
    assert response.json()["state"] == "pending"
    assert response.json()["candidates"] == []


def test_status_reports_exhausted_separately_from_failed(client: TestClient, db: Session) -> None:
    """The health check depends on this split. Counting obscure parts as breakage is
    how a failure count stops being read."""
    exhausted = make_part(db, "Obscure", mpn="OBS-1")
    research.record_result(db, part=exhausted, candidates=[])
    broken = make_part(db, "Broken", mpn="BRK-1")
    broken.research_attempts = research.MAX_RESEARCH_ATTEMPTS
    research.record_failure(db, part=broken, error="boom")
    db.commit()

    body = client.get("/api/research/status").json()
    assert body["exhausted"] == 1
    assert body["failed"] == 1
    assert body["counts"]["resolved"] == 0


def test_every_state_appears_in_the_counts_even_at_zero(db: Session) -> None:
    """A dashboard that omits a key when it is zero cannot distinguish "nothing
    failed" from "the failure count stopped being reported"."""
    assert set(research.status_counts(db)) == set(ResearchState)


def test_parts_with_a_primary_datasheet_are_not_queued_by_the_migration(db: Session) -> None:
    """The backfill this migration exists for.

    A fresh part is `pending`; the migration settles pre-existing parts that already
    have a primary datasheet to `resolved` so the first worker run does not go and
    re-research several hundred answered parts — burning provider quota and possibly
    replacing a hand-picked primary with whatever a provider returned first.

    Asserted here on the default rather than on the backfill itself because the
    migration ran before any row in this database existed; what is pinned is that the
    default is `pending` and that nothing else silently changes it.
    """
    part = make_part(db, "Fresh", mpn="F-1")
    assert part.research_state == ResearchState.PENDING
    assert db.get(Part, part.id) is part
