"""What the researcher tried, and what became of each attempt (ADR 0017).

The queue itself is not here — it is five columns and an index on `parts`, for the
reason `ExtractionState`'s docstring gives about the extraction queue. This module
holds the one genuinely tabular part of research: the **candidates**, of which
there are zero or many per part, so they cannot be columns on anything.

## Why rejections are stored rather than dropped

ADR 0017's rule is that the researcher proposes and never asserts: a URL from any
source — a distributor API, a manufacturer URL pattern, a web search, the model
itself — is fetched and validated before it is believed, and validation is
arithmetic (is it a PDF, does it parse, does the normalised MPN appear in its
text) rather than a judgement.

The tempting implementation records only the winner. That loses the distinction
that matters most when something goes wrong:

* a part with **no candidate rows** was never looked at, or nothing proposed
  anything — a gap in provider coverage;
* a part with **four rejected rows, all `mpn_absent`** was looked at hard and every
  source returned the wrong part's datasheet — a bug in a provider, or an MPN that
  needs normalising differently;
* a part with **one rejected row, `not_pdf`** hit a manufacturer login wall.

All three read as "no datasheet" if only the winner is stored, and they have
nothing in common. Storing the rejections is what makes `EXHAUSTED` a diagnosis
instead of a shrug.

## No `CHECK`, and therefore a new reject reason is a one-line change

`state` and `reject_reason` are `sa.String` plus `StrEnumType`, never `sa.Enum` —
a test greps `sqlite_master` for `CHECK` and SQLite cannot alter one. That is not
ceremony here: reject reasons are exactly the kind of thing that grows every time
a new provider meets a new way of being unhelpful.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ResearchCandidateState
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: A proposed URL, bounded. Long enough for the query-string monsters distributor
#: search endpoints emit, short enough that a provider looping cannot store a
#: megabyte per row.
MAX_URL_LENGTH = 2048

#: A provider's self-declared name (`jlcparts`, `mouser`, `url_pattern`,
#: `websearch`, `model`). Free text on purpose: providers are added often and a
#: new one must not need a migration to be able to record what it found.
MAX_SOURCE_LENGTH = 64


class ResearchCandidate(Base):
    """One datasheet URL somebody proposed for one part, and its verdict."""

    __tablename__ = "research_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: `CASCADE`: a candidate is meaningless without its part, and these rows are
    #: diagnostics rather than history — nothing in the ledger's append-only
    #: reasoning applies to them.
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False)
    #: Which provider proposed it. Recorded even for a rejected candidate, because
    #: "which provider keeps returning the wrong part" is the question these rows
    #: exist to answer.
    source: Mapped[str] = mapped_column(String(MAX_SOURCE_LENGTH), nullable=False)
    url: Mapped[str] = mapped_column(String(MAX_URL_LENGTH), nullable=False)
    state: Mapped[str] = mapped_column(StrEnumType(ResearchCandidateState), nullable=False)
    #: Why it was refused. NULL unless `state` is `rejected`. Free text rather than
    #: an enum column: see the module docstring — the vocabulary grows per provider,
    #: and `app.services.research.RejectReason` documents the ones in use.
    reject_reason: Mapped[str | None] = mapped_column(String(64))
    #: The stored blob, once validated. NULL otherwise. Deliberately **not** a
    #: foreign key to `documents.sha256`: a document may be pruned or replaced
    #: without invalidating the historical fact that this URL once validated to it,
    #: and a dangling reference here is a diagnostic curiosity rather than a
    #: correctness problem.
    document_sha256: Mapped[str | None] = mapped_column(String(64))
    #: Provider order, lowest first — the cascade position from ADR 0017, so the
    #: deterministic sources sort above the model's suggestions. Ranking is
    #: recorded rather than recomputed, because which providers ran is a property
    #: of the run and changes as providers are added.
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Verbatim provider detail — an HTTP status, a parser message, the byte size
    #: that blew the ceiling. For a human; nothing branches on it.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # One verdict per URL per part. This is what makes a re-run idempotent:
        # researching a part again updates each candidate's verdict in place rather
        # than appending a second opinion, so the row count stays the number of
        # distinct things tried instead of growing with every retry.
        UniqueConstraint("part_id", "url", name="uq_research_candidates_part_url"),
        # "Show me this part's candidates, best first" — the part screen's query and
        # the only one that runs per-request.
        Index("ix_research_candidates_part", "part_id", "rank"),
    )
