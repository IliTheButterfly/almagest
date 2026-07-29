"""What enrichment is allowed to write: candidates, never values.

`parameter_value` is what search reads and what substitution decides on, so
every automated source writes here instead and a promotion step
(`app.services.enrichment.candidates`) decides what, if anything, crosses over.
The whole mechanism exists because the failure it prevents is **invisible**: a
wrong value written straight into `parameter_value` looks exactly like a right
one, participates in every substitution decision, and is only discovered when a
board does not work. A row sitting in this table is merely work.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CandidateReviewReason, CandidateStatus, Provenance
from app.models.types import StrEnumType, UtcDateTime, utcnow


class ParameterValueCandidate(Base):
    """One source's proposal for one parameter of one part.

    ## Uniqueness is *not* `(part_id, template_id)`

    `parameter_value` carries `UNIQUE(part_id, template_id)` and that constraint
    is load-bearing for search — it is what lets a multi-predicate query use
    plain `JOIN`s that never fan out. **Copying it here would defeat the entire
    purpose of the table**, because two sources disagreeing about the same field
    is the single most important thing this table has to be able to represent.
    Both rows must survive so a human can see the disagreement.

    The analogous constraint is one row per *observation*:
    `UNIQUE(part_id, template_id, source, source_ref)`. Re-running the same
    extraction over the same datasheet therefore updates its own row in place
    instead of accumulating a new one every night — which is the idempotency
    the promotion rules need, since "how many sources agree" is counted from
    these rows and duplicate rows from one source would manufacture a consensus
    out of one observation.

    ### Why `source_ref` is NOT NULL with an empty-string default

    In SQLite (and in the SQL standard) NULLs compare distinct inside a UNIQUE
    index, so a nullable `source_ref` would let unlimited rows share the same
    `(part, template, source)` — exactly the duplicate accumulation the
    constraint exists to prevent, and silently, since the constraint would
    still *look* present. An empty string means "this source has only one
    observation per field", which is true of an MPN decoder and of a
    single-endpoint provider lookup.

    A source that genuinely has several distinct observations for one field —
    two rows of a variant table, two revisions of a datasheet — distinguishes
    them by putting the document hash or table row in `source_ref`. That is a
    deliberate act by the caller, not something a re-run does by accident.

    ## Values are normalised on write, not at promotion time

    `raw_value` is kept verbatim (it is the asset — a value the grammar cannot
    parse today is a grammar gap to fix tomorrow, but only if the string
    survived), and alongside it the parsed interval or resolved `choice_id` is
    stored. Two reasons. Agreement between sources is then a comparison of
    numbers rather than of strings, so `100 nF` and `0.1 uF` compare equal
    without either being re-parsed; and a value that cannot be parsed at all is
    caught at intake, when the source and the raw text are still at hand,
    rather than at promotion.

    Promotion still re-parses `raw_value` through `app.services.parameters`,
    which is the one door onto `parameter_value` and the only thing that
    guarantees `value_min`/`value_max` come out populated. The columns here are
    for comparison and for display in the queue; they are never copied across.
    """

    __tablename__ = "parameter_value_candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("parameter_template.id", ondelete="CASCADE"), nullable=False
    )

    #: Which source proposed it. Same enum as `parameter_value.provenance`, so
    #: promotion is a copy and `PROVENANCE_PRIORITY` orders both.
    source: Mapped[str] = mapped_column(StrEnumType(Provenance), nullable=False)

    #: Which *instance* of that source: a datasheet sha256, a provider response
    #: id, an MPN decoder family name. Empty means the source has exactly one
    #: observation per field. Part of the uniqueness key — see the class
    #: docstring for why it must not be nullable.
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )

    #: Between 0 and 1. The design doc's auto-promote threshold is 0.8, applied only to
    #: the single-source-into-an-empty-field case; agreement between sources
    #: promotes without reference to it, because two independent sources landing
    #: on the same value is stronger evidence than either one's self-report.
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    #: Exactly what the source said, kept lossless.
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)

    #: Normalised numeric form, for the agreement test. `value_nominal` is NULL
    #: for a range (`20-30uF`), which is why agreement falls back to comparing
    #: both interval endpoints in that case.
    value_nominal: Mapped[float | None] = mapped_column(Float)
    value_min: Mapped[float | None] = mapped_column(Float)
    value_max: Mapped[float | None] = mapped_column(Float)

    #: Normalised enum form. Alias resolution has already happened, so `0603`
    #: and `1608` are the same `choice_id` and agreement between two sources
    #: using different conventions is exact rather than approximate.
    choice_id: Mapped[int | None] = mapped_column(
        ForeignKey("parameter_choice.id", ondelete="RESTRICT")
    )

    status: Mapped[str] = mapped_column(
        StrEnumType(CandidateStatus),
        nullable=False,
        default=CandidateStatus.PENDING,
        server_default=CandidateStatus.PENDING.value,
    )

    #: Why it is still pending, or why it was closed without promotion. A
    #: `CandidateReviewReason`; NULL once promoted.
    review_reason: Mapped[str | None] = mapped_column(StrEnumType(CandidateReviewReason))

    #: Set when the observation must never auto-promote however confident it is:
    #: an OCR'd or model-read part number, or a printed marking whose meaning
    #: depends on which component is in your hand. A flag rather than a rule
    #: inferred from `source`, because the same source can produce both kinds —
    #: an MPN decoder handed a real part number is trustworthy, and handed a
    #: three-digit marking it is guessing which quantity you meant.
    requires_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    #: Free-text detail for the queue: which fields of a partial decode were not
    #: understood, which parse error fired. Never parsed, only shown.
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    #: When it stopped being pending. NULL while it is in the queue.
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        UniqueConstraint(
            "part_id",
            "template_id",
            "source",
            "source_ref",
            name="uq_parameter_value_candidate_observation",
        ),
        # The review queue's own query: pending rows, newest first.
        Index(
            "ix_pvc_pending",
            "status",
            "created_at",
            sqlite_where=status == CandidateStatus.PENDING.value,
        ),
        # Evaluating one field reads every candidate for it.
        Index("ix_pvc_part_template", "part_id", "template_id"),
    )
