"""Validation, the provider cascade, and the worker that runs both (ADR 0017).

## What this file is really guarding

**A wrong part's datasheet, attached and looking authoritative.** This is the
failure the whole design exists to prevent, and it is silent: the PDF is real, it
parses, it opens in the viewer, and every parameter later extracted from it is
confidently wrong. The gate is `mpn_in_text`, and the tests below check it against
the four ways a candidate can be plausible and wrong — a login wall served as
`200 application/pdf`, a family sheet for the neighbouring series, a truncated
file, and a URL that simply is not there.

**A hallucinated URL treated as a fact.** ADR 0017's rule is that a provider — any
provider, including a model — proposes and never asserts. The cascade tests pin
that every proposal is a `Candidate` and nothing more, and the worker tests pin
that a proposal only becomes a `documents` row after the fetch and the four checks.

**A part that resolves without anybody being able to see why it nearly did not.**
The worker fetches *every* candidate rather than stopping at the first success, so
the rejections are recorded alongside it. That is the diagnostic value of the whole
candidate table and it is easy to "optimise" away.

Everything here is offline. The one thing that touches the network is `HttpFetcher`,
behind the `Fetcher` Protocol, and these tests substitute a dict for it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import ResearchState
from app.scripts.research_datasheets import Fetched, QueuedPart, process_one, run_once
from app.services import research
from app.services.datasheet_validation import (
    RejectReason,
    looks_like_pdf,
    mpn_in_text,
    validate,
)
from app.services.enrichment.providers import (
    Candidate,
    FakeProvider,
    ManualProvider,
    PartQuery,
    UrlPatternProvider,
    gather,
)
from app.services.extractors import ExtractedText, ExtractorUnavailable, PyPdfExtractor
from tests import pdfs
from tests.factories import make_part

pypdf = pytest.importorskip("pypdf", reason="the `datasheets` extra is not installed")

MPN = "GRM188R71H104KA93D"
MPN_NORM = "grm188r71h104ka93d"


def _extractor() -> PyPdfExtractor:
    return PyPdfExtractor()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_login_wall_served_as_a_pdf_is_refused() -> None:
    """The case a content-type check gets wrong.

    Manufacturer sites answer `200 application/pdf` with an HTML "please sign in"
    body often enough that trusting the header is how a text/html page becomes a
    stored datasheet. The magic bytes decide.
    """
    body = b"<!doctype html><title>Sign in</title>"

    verdict = validate(
        body, mpn_norm=MPN_NORM, extractor=_extractor(), content_type="application/pdf"
    )

    assert not verdict.accepted
    assert verdict.reason == RejectReason.NOT_PDF
    # The claimed type survives into the note, because "the server said PDF and
    # lied" is the thing worth seeing in the candidate list.
    assert "application/pdf" in (verdict.note or "")


def test_a_real_datasheet_for_the_wrong_part_is_refused() -> None:
    """**The load-bearing test.**

    A genuine, parseable manufacturer PDF — for something else. Nothing about it is
    malformed, no confidence score distinguishes it, and it is exactly what a web
    search or a model returns when it does not know. Only the part-number check
    catches it, and this is why that check is the gate rather than a heuristic.
    """
    other = pdfs.with_text(["Murata GRM155R61A106ME11 0402 10uF 10V X5R"])

    verdict = validate(other, mpn_norm=MPN_NORM, extractor=_extractor())

    assert not verdict.accepted
    assert verdict.reason == RejectReason.MPN_ABSENT


def test_the_right_datasheet_is_accepted_and_carries_its_text() -> None:
    """Accepted, and the parsed text comes back so the worker need not parse twice —
    the document is going into the extraction queue and has just been read."""
    good = pdfs.with_text([f"Murata {MPN} 0603 100nF 50V X7R"])

    verdict = validate(good, mpn_norm=MPN_NORM, extractor=_extractor())

    assert verdict.accepted
    assert verdict.reason is None
    assert verdict.text is not None
    assert verdict.text.page_count == 1


def test_the_part_number_matches_through_the_datasheet_s_own_typography() -> None:
    """Manufacturers break part numbers across a table with spaces and hyphens, and
    vary the case within one document. Both sides go through the catalogue's
    `normalize_mpn`, so the printed form cannot cause a miss."""
    text = ExtractedText(pages=("Part No.  GRM188 R71H 104K A93D  (0603)",))

    assert mpn_in_text(text, mpn_norm=MPN_NORM)


def test_a_part_number_is_never_manufactured_by_two_pages_abutting() -> None:
    """Pages are normalised individually. Joining first would let the tail of one
    page and the head of the next compose a part number that appears nowhere."""
    split = ExtractedText(pages=("...ends with GRM188R71H", "104KA93D begins..."))

    assert not mpn_in_text(split, mpn_norm=MPN_NORM)


def test_an_oversized_body_is_refused_before_a_parser_sees_it() -> None:
    verdict = validate(
        b"%PDF-" + b"x" * 500, mpn_norm=MPN_NORM, extractor=_extractor(), max_bytes=100
    )

    assert not verdict.accepted
    assert verdict.reason == RejectReason.TOO_LARGE


def test_a_truncated_pdf_is_a_verdict_not_a_crash() -> None:
    verdict = validate(b"%PDF-1.7\ntruncated", mpn_norm=MPN_NORM, extractor=_extractor())

    assert not verdict.accepted
    assert verdict.reason == RejectReason.PARSE_FAILED


def test_a_missing_extractor_is_raised_not_reported() -> None:
    """A deployment error, not a fact about this candidate.

    Reported per candidate it would blame every provider in turn for a broken image
    and burn the part's attempts doing it — the same escape, for the same reason, as
    `extract_datasheets.process_one`.
    """

    class Broken:
        name = "broken"

        def extract(self, data: bytes) -> ExtractedText:
            raise ExtractorUnavailable("no parser installed")

    with pytest.raises(ExtractorUnavailable):
        validate(pdfs.with_text(["anything"]), mpn_norm=MPN_NORM, extractor=Broken())


def test_a_pdf_signature_deep_inside_a_page_is_not_a_pdf() -> None:
    """An HTML page that mentions PDFs is an HTML page."""
    assert not looks_like_pdf(b"<html>" + b"." * 4000 + b"%PDF-1.7")
    assert looks_like_pdf(b"%PDF-1.7 ...")


# ---------------------------------------------------------------------------
# The cascade
# ---------------------------------------------------------------------------


def test_a_murata_part_number_builds_a_url_with_no_manufacturer_recorded() -> None:
    """The stub-part case, and the reason the pattern's own regex decides.

    `GRM188...` is a Murata part number whether or not anybody filled the
    manufacturer field in — and an unfinished stub with a scanned MPN is exactly
    what research exists for. Requiring the field would make the provider useless
    for its main case.
    """
    proposed = UrlPatternProvider().propose(PartQuery(mpn=MPN, mpn_norm=MPN_NORM))

    assert len(proposed) == 1
    assert proposed[0].url.endswith(f"{MPN}.pdf")
    assert proposed[0].source == "url_pattern"


def test_a_part_number_no_pattern_recognises_proposes_nothing() -> None:
    """An empty tuple is the normal answer for "I do not cover this", and is not an
    error. A provider that raised here would turn a narrow cascade into a failed run."""
    assert UrlPatternProvider().propose(PartQuery(mpn="WIDGET-7", mpn_norm="widget7")) == ()


def test_the_cascade_dedupes_by_url_and_keeps_the_better_proposer() -> None:
    """Two providers naming the same PDF is agreement: fetch it once, and attribute
    it to the more trustworthy of the two."""
    url = "https://example.test/ds.pdf"
    manual = ManualProvider({MPN_NORM: [url]})
    web = FakeProvider(name="websearch", responses={MPN_NORM: [url]})

    gathered = gather([web, manual], PartQuery(mpn=MPN, mpn_norm=MPN_NORM))

    assert len(gathered) == 1
    assert gathered[0].source == "manual"


def test_the_cascade_sorts_deterministic_sources_above_guesses() -> None:
    manual = ManualProvider({MPN_NORM: ["https://a.test/hand.pdf"]})
    web = FakeProvider(name="websearch", responses={MPN_NORM: ["https://b.test/found.pdf"]})

    gathered = gather([web, manual, UrlPatternProvider()], PartQuery(mpn=MPN, mpn_norm=MPN_NORM))

    assert [c.source for c in gathered] == ["manual", "url_pattern", "websearch"]


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class _Fetcher:
    """A dict standing in for the open internet."""

    def __init__(self, bodies: dict[str, Fetched]) -> None:
        self.bodies = bodies
        self.asked: list[str] = []

    def fetch(self, url: str) -> Fetched:
        self.asked.append(url)
        return self.bodies.get(url, Fetched(None, error="HTTP 404"))


class _Client:
    """An `ApiClient` that records rather than calls."""

    def __init__(self, claims: list[QueuedPart] | None = None) -> None:
        self.claims = claims or []
        self.uploaded: list[tuple[int, str, bool]] = []
        self.submitted: list[tuple[int, list[dict[str, object]]]] = []
        self.failures: list[tuple[int, str]] = []

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedPart]:
        out, self.claims = self.claims[:limit], self.claims[limit:]
        return out

    def upload_datasheet(
        self, *, part_id: int, data: bytes, source_url: str, is_primary: bool
    ) -> str:
        self.uploaded.append((part_id, source_url, is_primary))
        return f"{len(self.uploaded):064d}"

    def submit_candidates(self, *, part_id: int, candidates: object) -> None:
        self.submitted.append((part_id, list(candidates)))  # type: ignore[arg-type]

    def submit_failure(self, *, part_id: int, error: str) -> None:
        self.failures.append((part_id, error))


def _part(part_id: int = 1) -> QueuedPart:
    return QueuedPart(
        part_id=part_id, name="Cap", mpn=MPN, mpn_norm=MPN_NORM, manufacturer="Murata", attempts=1
    )


def test_the_worker_fetches_every_candidate_even_after_one_validates() -> None:
    """Not an inefficiency — the rejections are the diagnostic value.

    A part that resolves on its second candidate tells you nothing about why the
    first was wrong. Stopping early optimises the case that already worked at the
    expense of the case that did not.
    """
    good = "https://a.test/right.pdf"
    bad = "https://b.test/wrong.pdf"
    fetcher = _Fetcher(
        {
            good: Fetched(pdfs.with_text([f"{MPN} datasheet"]), "application/pdf"),
            bad: Fetched(pdfs.with_text(["some other part"]), "application/pdf"),
        }
    )
    providers = [
        ManualProvider({MPN_NORM: [good]}),
        FakeProvider(name="websearch", responses={MPN_NORM: [bad]}),
    ]
    client = _Client()

    assert process_one(client, fetcher, _extractor(), providers, _part()) is True

    assert set(fetcher.asked) == {good, bad}
    _, reported = client.submitted[0]
    assert {r["state"] for r in reported} == {"validated", "rejected"}
    assert [r["reject_reason"] for r in reported if r["state"] == "rejected"] == ["mpn_absent"]


def test_only_the_first_validated_candidate_becomes_the_primary() -> None:
    """Ranking already put the most trustworthy proposer first. A primary that
    depends on which fetch finished first is not reproducible."""
    first, second = "https://a.test/1.pdf", "https://b.test/2.pdf"
    body = Fetched(pdfs.with_text([f"{MPN} datasheet"]), "application/pdf")
    providers = [
        ManualProvider({MPN_NORM: [first]}),
        FakeProvider(name="websearch", responses={MPN_NORM: [second]}),
    ]
    client = _Client()

    process_one(client, _Fetcher({first: body, second: body}), _extractor(), providers, _part())

    assert [(url, primary) for _, url, primary in client.uploaded] == [
        (first, True),
        (second, False),
    ]


def test_a_url_that_404s_is_recorded_as_fetch_failed_not_as_a_bad_pdf() -> None:
    """Distinct from `not_pdf`: nothing was served, so no provider is accused of
    serving garbage. A wrong URL template and a login wall are different bugs."""
    client = _Client()
    providers = [ManualProvider({MPN_NORM: ["https://gone.test/x.pdf"]})]

    process_one(client, _Fetcher({}), _extractor(), providers, _part())

    _, reported = client.submitted[0]
    assert reported[0]["reject_reason"] == RejectReason.FETCH_FAILED
    assert client.uploaded == []


def test_a_part_with_no_mpn_is_exhausted_rather_than_failed() -> None:
    """Nothing to search on is not breakage. Marking it `failed` would put it in a
    health check meant to surface things worth fixing in the code."""
    client = _Client()
    nameless = QueuedPart(
        part_id=9, name="Mystery", mpn=None, mpn_norm=None, manufacturer=None, attempts=1
    )

    assert process_one(client, _Fetcher({}), _extractor(), [], nameless) is False
    assert client.submitted == [(9, [])]
    assert client.failures == []


def test_a_provider_that_explodes_is_reported_as_a_run_failure() -> None:
    """The run broke rather than the part being unfindable, so the queue retries it.
    That is the difference between a transient egress fault and an obscure part."""

    class Exploding:
        name = "boom"
        rank = 1

        def propose(self, query: PartQuery) -> list[Candidate]:
            raise RuntimeError("provider is on fire")

    client = _Client([_part()])

    run_once(client, _Fetcher({}), _extractor(), [Exploding()], worker_id="w1")

    assert client.submitted == []
    assert client.failures[0][0] == 1
    assert "provider is on fire" in client.failures[0][1]


def test_a_non_http_scheme_is_refused_before_urllib_sees_it() -> None:
    """A provider — or a model — proposing `file:///etc/passwd` must be stopped at
    the door. urllib would happily open it."""
    from app.scripts.research_datasheets import HttpFetcher

    assert HttpFetcher().fetch("file:///etc/passwd").error == "refused scheme 'file'"


# ---------------------------------------------------------------------------
# End to end, through the real routes
# ---------------------------------------------------------------------------


def test_a_researched_part_resolves_and_lands_in_the_extraction_queue(
    client: TestClient, db: Session
) -> None:
    """The chain ADR 0017 promises: research stores and links a PDF, and it arrives
    in the *extraction* queue as `pending` with neither worker knowing the other."""
    part = make_part(db, "Murata cap", mpn=MPN)
    db.commit()

    url = "https://murata.test/GRM188.pdf"
    body = pdfs.with_text([f"Murata {MPN} 0603 100nF"])

    claimed = client.post("/api/research/claims", json={"worker_id": "w1", "limit": 1}).json()
    assert claimed["claims"][0]["mpn_norm"] == MPN_NORM

    upload = client.post(
        f"/api/documents?media_type=application/pdf&part_id={part.id}"
        f"&role=datasheet&is_primary=true&source_url={url}",
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert upload.status_code == 200
    sha256 = upload.json()["document"]["sha256"]

    submitted = client.post(
        "/api/research/results",
        json={
            "part_id": part.id,
            "candidates": [
                {
                    "source": "url_pattern",
                    "url": url,
                    "state": "validated",
                    "document_sha256": sha256,
                    "rank": 2,
                }
            ],
        },
    )
    assert submitted.json()["part"]["state"] == ResearchState.RESOLVED

    # The handover: the stored PDF is now the extraction queue's problem.
    pending = client.post("/api/extraction/claims", json={"worker_id": "x1", "limit": 5}).json()
    assert sha256 in [c["sha256"] for c in pending["claims"]]

    db.expire_all()
    assert research.status_counts(db)[ResearchState.RESOLVED] == 1
