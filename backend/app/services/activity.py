"""One parked scan's whole story, stitched out of the seven tables that hold it.

The pipeline is deliberately built as workers that do not know about each other:
dispatch mints stub parts and stops, research finds a PDF and stops, extraction
proposes fields and stops. That decoupling is right — ADR 0021's GPU argument
depends on it — and it has one cost, which this module pays. **Nothing anywhere
says what happened to a photograph.** The intake panel shows the candidates, the
part screen shows the datasheet, the review queue shows the fields, and the
question a person actually asks — *I photographed a resistor, why did it come out
as `CFI4JT100K`* — is answered by none of them because the answer spans all three.

So this is a read, and only a read. It joins nothing new and stores nothing:

* the entry, its capture and the capture's regions — what the browser read;
* the dispatch queue's standing, and the `model_runs` rows behind it — what a
  model was told and what it said;
* the identity candidates — what it proposed;
* the part a **person** accepted, and then that part's research candidates, its
  documents' extraction standing, and its `parameter_value_candidate` rows.

## This module writes nothing, and that is a rule rather than an observation

`pending_intakes.mpn`, `resolved_part_id` and `status` are the three columns
ADR 0021 forbids anything automated from touching, and a diagnostic view is
exactly the sort of place a convenience write ("while we are here, promote the
best candidate") gets added. There is no session mutation in this file and no
route over it that takes a body.

## Where the chain stops, it says so rather than returning nothing

An entry nobody dispatched has no runs, and an accepted part nobody researched has
no candidates. Both are the *normal* state — dispatch is opt-in and the research
worker runs on its own schedule — so each section reports its own emptiness
explicitly instead of collapsing to an absent key. A UI cannot tell "no worker has
run" from "the worker ran and found nothing" out of an empty list, and those are
the two things a person looking at this screen is trying to distinguish.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.captures import Capture, CaptureRegion
from app.models.catalog import Part
from app.models.dispatch import IntakeIdentityCandidate
from app.models.documents import Document, DocumentLink
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import EntityType
from app.models.parameter import ParameterTemplate
from app.models.research import ResearchCandidate
from app.models.runs import ModelRun
from app.models.scanning import PendingIntake
from app.services import dispatch, runs

#: `parameter_value_candidate` rows carried per accepted part. A bound rather than
#: a page: this is a diagnostic panel, not the review queue (`/api/enrichment`,
#: which paginates properly), and a part with more proposed fields than this has a
#: problem the review queue is the place to look at.
MAX_FIELD_CANDIDATES = 200


@dataclass(frozen=True)
class CaptureStory:
    """The photograph, and every outline read off it in the browser."""

    capture: Capture
    document_sha256: str
    regions: list[CaptureRegion]


@dataclass(frozen=True)
class PartStory:
    """What became of the part a person accepted.

    Reached only through `pending_intakes.resolved_part_id`, which is a **person's**
    decision. A candidate's stub `part_id` is deliberately not followed here: it is
    a machine's proposal, and walking it would present three unaccepted stubs'
    research as though it were this entry's outcome.
    """

    part: Part
    research_candidates: list[ResearchCandidate]
    #: The part's attached documents. Each carries its own `extraction_state`, which
    #: is where the third queue's standing is legible.
    documents: list[Document]
    #: `(candidate, template)` pairs. The template rides along because a candidate
    #: identified only by `template_id` is unreadable, and one extra join here beats
    #: one lookup per row in the client.
    field_candidates: list[tuple[ParameterValueCandidate, ParameterTemplate]]


@dataclass(frozen=True)
class IntakeStory:
    """Everything known about one parked scan, in the order it happened."""

    entry: PendingIntake
    capture: CaptureStory | None
    model_runs: list[ModelRun]
    identity_candidates: list[IntakeIdentityCandidate]
    resolved: PartStory | None = None


def story_for(session: Session, *, entry: PendingIntake) -> IntakeStory:
    """Read the whole timeline for one entry. Never writes."""
    return IntakeStory(
        entry=entry,
        capture=None if entry.capture_id is None else _capture(session, entry.capture_id),
        model_runs=list(runs.runs_for(session, intake_id=entry.id)),
        identity_candidates=dispatch.candidates_for(session, intake_id=entry.id),
        resolved=(
            None if entry.resolved_part_id is None else _part(session, entry.resolved_part_id)
        ),
    )


def _capture(session: Session, capture_id: int) -> CaptureStory | None:
    """The capture and its regions, or None if it has been deleted.

    `pending_intakes.capture_id` is `SET NULL` on delete, so this cannot normally
    return None — but a `SET NULL` that has not been flushed into the identity map
    this session is holding could, and a diagnostic view refusing to render because
    a photograph was deleted would be the least useful possible failure.
    """
    row = session.execute(
        select(Capture, Document.sha256)
        .join(Document, Document.id == Capture.document_id)
        .where(Capture.id == capture_id)
    ).one_or_none()
    if row is None:  # pragma: no cover - see the docstring
        return None
    capture, sha256 = row
    regions = list(
        session.execute(
            select(CaptureRegion)
            .where(CaptureRegion.capture_id == capture_id)
            .order_by(CaptureRegion.order_index, CaptureRegion.id)
        )
        .scalars()
        .all()
    )
    return CaptureStory(capture=capture, document_sha256=str(sha256), regions=regions)


def _part(session: Session, part_id: int) -> PartStory | None:
    part = session.get(Part, part_id)
    if part is None:  # pragma: no cover - `resolved_part_id` is SET NULL on delete
        return None

    research_candidates = list(
        session.execute(
            select(ResearchCandidate)
            .where(ResearchCandidate.part_id == part_id)
            .order_by(ResearchCandidate.rank, ResearchCandidate.id)
        )
        .scalars()
        .all()
    )
    documents = list(
        session.execute(
            select(Document)
            .join(DocumentLink, DocumentLink.document_id == Document.id)
            .where(
                DocumentLink.entity_type == EntityType.PART,
                DocumentLink.entity_pk == part_id,
            )
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )
    field_candidates = [
        (candidate, template)
        for candidate, template in session.execute(
            select(ParameterValueCandidate, ParameterTemplate)
            .join(ParameterTemplate, ParameterTemplate.id == ParameterValueCandidate.template_id)
            .where(ParameterValueCandidate.part_id == part_id)
            .order_by(ParameterValueCandidate.id)
            .limit(MAX_FIELD_CANDIDATES)
        ).all()
    ]
    return PartStory(
        part=part,
        research_candidates=research_candidates,
        documents=documents,
        field_candidates=field_candidates,
    )
