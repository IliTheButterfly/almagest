"""The autonomous pipeline: a part number in, a defined part out.

This is the module that makes the separate pieces one *system*. Everything it
calls already existed and is tested on its own — the research queue, the
fetch-and-validate gate, the extraction provider, the MPN cross-check, the
candidate promotion rules. What was missing was something that ran them in order
without a person driving each step, and a test that proves the whole chain works
rather than proving six links do.

## The autonomy boundary, stated once

**Autonomous:** finding a datasheet, validating it against the part number,
extracting fields, cross-checking them against the MPN decoder, promoting the ones
that clear the existing bar, creating the part, and *proposing* where to put it.

**Not autonomous, ever:** which container the part actually goes in. That is the
one review this pipeline insists on, and it is not squeamishness — a part filed in
the wrong drawer is worse than a part in no drawer, because the system now asserts
a location that is wrong and nobody will check it until they go looking. So
`suggest_containers` returns *options*, ranked, with the reason for each, and
something else commits.

Everything else that could be wrong is recoverable by looking at it: a wrong field
is in the review queue with its source line, a wrong datasheet is one of several
candidates with its rejection reasons beside it.

## Notes steer the ranking, and are kept

`ContainerReview.notes` is free text a person adds — "these go in the SMD drawers,
not the through-hole cabinet" — and it is fed back into the next ranking rather
than being a one-off. That is the cheapest possible learning loop and it is
deliberately *not* a model fine-tune: the note is a filter and a boost applied by
`rank_containers`, so its effect is inspectable and it cannot silently decay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import ResearchCandidateState, ResearchState
from app.models.storage import Location
from app.services import research
from app.services.datasheet_validation import validate
from app.services.enrichment import cross_check
from app.services.enrichment.extract import ExtractionProvider, ExtractionRequest, TargetField
from app.services.enrichment.providers import DatasheetProvider, PartQuery, gather
from app.services.extractors import Extractor
from app.services.scanning.codes import normalize_mpn


class Fetcher(Protocol):
    """Whatever gets bytes from a URL. Substituted wholesale in tests."""

    def fetch(self, url: str) -> object: ...


@dataclass(frozen=True)
class DefinedPart:
    """What one autonomous run produced, and what it refused to decide."""

    part_id: int
    mpn: str
    #: The datasheet that survived validation, if any.
    document_sha256: str | None
    research_state: ResearchState
    #: Fields written to `parameter_value` because they cleared the existing
    #: promotion bar — empty and single-source at >= 0.8, or two sources agreeing.
    promoted: tuple[str, ...] = ()
    #: Fields that landed in the review queue instead. **Not a failure**: this is
    #: the never-auto-accept rule doing its job, and a run that promotes nothing is
    #: still a run that found and attached the right datasheet.
    queued_for_review: tuple[str, ...] = ()
    #: Rejected candidates, so "no datasheet" stays a diagnosis (ADR 0017).
    rejections: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ContainerOption:
    """One place this part could go, and why.

    `location_id` is None for a **proposed new container** — "make a Gridfinity
    1x1 in the second drawer" is a legitimate answer to "where does this go", and
    an option list that could only name places that already exist would force the
    person out to another screen exactly when they are deciding. Creating it is
    still reversible authoring, and it still only happens if this option is the one
    chosen (ADR 0018).
    """

    location_id: int | None
    label_path: str
    score: float
    #: Said in words, because the review exists for a person to agree or disagree
    #: with the *reason*, not to rubber-stamp a number.
    why: str


@dataclass
class ContainerReview:
    """The one thing this pipeline will not decide for itself."""

    part_id: int
    options: tuple[ContainerOption, ...]
    #: Free text from a person, carried into the next ranking. See the module
    #: docstring: a filter and a boost, not a fine-tune.
    notes: str = ""


def define_part_from_mpn(
    session: Session,
    *,
    part: Part,
    providers: list[DatasheetProvider],
    fetcher: Fetcher,
    extractor: Extractor,
    extraction: ExtractionProvider,
    fields: tuple[TargetField, ...],
    store_document: object = None,
) -> DefinedPart:
    """Research, validate, extract, cross-check, promote. One part, no person.

    Ordering is the whole content of this function, and each step is the module
    that already owned it:

    1. `providers.gather` proposes URLs — deterministic sources first.
    2. Each is fetched and put through `datasheet_validation.validate`, whose MPN
       check is what makes a hallucinated or merely wrong URL harmless.
    3. The first survivor is stored and its text goes to the `ExtractionProvider`.
    4. `cross_check.ingest` weighs the model's reading against the MPN decoder's
       arithmetic and writes candidates — never `parameter_value` directly.
    5. `candidates.evaluate` promotes only what clears the existing bar.

    **Nothing here relaxes a rule to make the run complete.** A part that comes out
    with every field in the review queue is a correct outcome, and the caller is
    told which fields those were rather than being handed a success that is really
    a deferral.
    """
    mpn = part.mpn or ""
    mpn_norm = part.mpn_norm or normalize_mpn(mpn)
    query = PartQuery(mpn=mpn, mpn_norm=mpn_norm, manufacturer=None, name=part.name)

    rejections: list[tuple[str, str]] = []
    accepted_sha: str | None = None
    accepted_text: str | None = None

    for candidate in gather(providers, query):
        fetched = fetcher.fetch(candidate.url)
        data = getattr(fetched, "data", None)
        if data is None:
            rejections.append((candidate.url, research.RejectReason.FETCH_FAILED))
            continue
        verdict = validate(
            data,
            mpn_norm=mpn_norm,
            extractor=extractor,
            content_type=getattr(fetched, "content_type", None),
        )
        if not verdict.accepted:
            rejections.append((candidate.url, verdict.reason or "unknown"))
            continue
        if accepted_sha is None and callable(store_document):
            accepted_sha = str(store_document(part.id, data, candidate.url))
            accepted_text = "\n".join(verdict.text.pages) if verdict.text else ""

    if accepted_sha is None:
        research.record_result(session, part=part, candidates=[])
        return DefinedPart(
            part_id=part.id,
            mpn=mpn,
            document_sha256=None,
            research_state=ResearchState.EXHAUSTED,
            rejections=tuple(rejections),
        )

    research.record_result(
        session,
        part=part,
        candidates=[
            research.CandidateReport(
                source="url_pattern",
                url="stored",
                state=ResearchCandidateState.VALIDATED,
                document_sha256=accepted_sha,
            )
        ],
    )

    result = extraction.extract(
        ExtractionRequest(
            document_ref=accepted_sha,
            document_text=accepted_text or "",
            mpns=(mpn,),
            fields=fields,
        )
    )
    # `ingest` records both sources and evaluates in one pass — deliberately, per
    # its own docstring: evaluating after the decoder but before the model would
    # promote the decoder's value into an empty field and leave the model's
    # contradicting reading to arrive at an occupied one. So the report it returns
    # is the authority on what was promoted, and re-deriving it here would be a
    # second opinion free to disagree with the rows.
    report = cross_check.ingest(session, result=result)

    promoted: list[str] = []
    queued: list[str] = []
    for decision in report.decisions:
        target = promoted if decision.decision.promoted is not None else queued
        target.append(decision.template_name)

    return DefinedPart(
        part_id=part.id,
        mpn=mpn,
        document_sha256=accepted_sha,
        research_state=ResearchState.RESOLVED,
        promoted=tuple(promoted),
        queued_for_review=tuple(queued),
        rejections=tuple(rejections),
    )


# ---------------------------------------------------------------------------
# The one review
# ---------------------------------------------------------------------------


def rank_containers(
    session: Session, *, part: Part, notes: str = "", limit: int = 5
) -> tuple[ContainerOption, ...]:
    """Rank places this part could live, best first, each with its reason.

    Deterministic and inspectable on purpose. A model ranking drawers would produce
    a plausible order nobody could argue with; this produces an order with a stated
    reason per row, which is what makes the review a decision rather than a
    formality.

    `notes` is applied as a **substring boost over the location's own path**, so
    "SMD drawers" lifts every drawer whose path says SMD. Crude, and deliberately
    so: the person can see exactly why the order changed, and a note that stops
    matching stops having an effect instead of decaying invisibly.
    """
    locations = list(
        session.execute(
            select(Location).where(Location.retired_at.is_(None), Location.is_staging.is_(False))
        )
        .scalars()
        .all()
    )

    hints = [word.lower() for word in notes.split() if len(word) > 2]
    options: list[ContainerOption] = []
    for location in locations:
        path = location.label_path or location.name
        score = 0.0
        why: list[str] = []

        matched = [hint for hint in hints if hint in path.lower()]
        if matched:
            score += 10.0 * len(matched)
            why.append(f"matches your note ({', '.join(matched)})")

        # A container that already holds this part is nearly always the answer, and
        # saying so is more useful than any similarity score.
        existing = getattr(location, "id", None)
        if existing is not None and _holds_part(session, location_id=location.id, part_id=part.id):
            score += 100.0
            why.append("already holds this part")

        if not why:
            why.append("empty slot in the tree")

        options.append(
            ContainerOption(
                location_id=location.id,
                label_path=path,
                score=score,
                why="; ".join(why),
            )
        )

    # A proposal to make somewhere new, offered when nothing existing is a good
    # match. Ranked *below* every real container on purpose: an existing drawer
    # that already holds this part is nearly always the right answer, and offering
    # "create a new one" above it is how a catalogue grows a second home for
    # everything.
    if not any(option.score >= 10.0 for option in options):
        options.append(
            ContainerOption(
                location_id=None,
                label_path="(new container)",
                score=-1.0,
                why="nothing existing matches — a new container can be created here",
            )
        )

    options.sort(key=lambda option: (-option.score, option.label_path))
    return tuple(options[:limit])


def _holds_part(session: Session, *, location_id: int, part_id: int) -> bool:
    from app.models.stock import StockLot

    return (
        session.execute(
            select(StockLot.id)
            .where(StockLot.location_id == location_id, StockLot.part_id == part_id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def review_for(session: Session, *, part: Part, notes: str = "") -> ContainerReview:
    """The review a person answers. The only step this pipeline will not take."""
    return ContainerReview(
        part_id=part.id, options=rank_containers(session, part=part, notes=notes), notes=notes
    )
