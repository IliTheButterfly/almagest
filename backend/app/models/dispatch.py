"""What a vision model proposed a photograph might be, and every loser (ADR 0021).

The queue itself is not here — it is six columns and an index on `pending_intakes`,
for the reason `ExtractionState`'s docstring gives about the extraction queue. This
module holds the one genuinely tabular part of dispatch: the **identity candidates**,
of which there are zero or many per parked scan, so they cannot be columns on
anything.

Deliberately the same shape as `app.models.research`, whose docstring argues the
whole design and whose arguments transfer unchanged. What follows is only what
differs, and there are three things.

## The losers are kept because they are *supposed to compete*

`research_candidates` keeps its rejections for diagnostics: four `mpn_absent` rows
mean a provider is returning the wrong part, and that is unrecoverable if only the
winner is stored. The rows here are kept for a stronger reason than diagnosis —
**the second and third candidates are still live options.** ADR 0021's mechanism is
that `datasheet_validation` eliminates a wrong reading by failing to find its part
number in a PDF it actually fetched, which is arithmetic a reviewer can check rather
than an opinion they cannot. A schema that stored only the best guess would throw
away the alternatives before anything had a chance to test them, and the one
measured failure mode of this model is confidently naming the wrong string on a
label that has several.

So `rank` is meaningful and ordering is by it, and a row's absence means the model
never proposed it — never that it was pruned.

## `source_text` is `NOT NULL`, and that is the review mechanism

The characters the model claims it read, verbatim. Required at the schema level
because it is what a reviewer checks *instead of* taking the model's word, and
ADR 0021 records it catching the exact failure it was placed for: the wrong answer
quoted `'MODEL: MCQ-XBEE3'`, a line that is not on the label — which says
`MODEL: MICRO` and `FCC ID: MCQ-XBEE3` on separate lines. A person comparing the
quote against the photograph sees that immediately. A bare assertion would have
looked identical to the right answer.

An empty string would defeat it as thoroughly as a NULL, so
`app.services.dispatch` refuses one rather than storing it.

## `part_id` is the stub, and it is emphatically not `resolved_part_id`

A candidate may have a stub `parts` row minted for it — which, because
`Part.research_state` defaults to `PENDING`, is the same act as enqueuing it for
research, and is how the losers get tested. That id lives here, on the candidate.

**It must never be copied to `pending_intakes.resolved_part_id`** by anything
automated. That column is what a *person* decided, and this one is what a model
proposed; the whole propose-never-assert rule is the gap between them. Same
reasoning that already keeps `pending_intakes.part_id` (what the resolver matched)
apart from `resolved_part_id` (what the desk pass chose).

## No `CHECK`, so a new field is additive

Everything here is `sa.String`/`Text`/`Float` with no `CHECK` anywhere — see
`CLAUDE.md`. `package` and `label_kind`-adjacent vocabulary grow as more kinds of
label meet this reader, and none of that should need a table rebuild.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UtcDateTime, utcnow

#: A part number as printed, bounded to match `parts.mpn` and `pending_intakes.mpn`.
#: The same value may be copied into either, and two different widths would mean a
#: string that fits one column and is truncated in the next.
MAX_MPN_LENGTH = 128

#: A manufacturer as printed. Matches `pending_intakes.manufacturer`, same reasoning.
MAX_MANUFACTURER_LENGTH = 128

#: A package or case code — `0603`, `SOT-23-6`, `TQFP-100`. Generous, because a
#: model asked for a package will sometimes answer with a phrase.
MAX_PACKAGE_LENGTH = 64

#: The quoted characters. Matches `vision.schema_for`'s `maxLength` on the same
#: field, so a response the decoder accepted cannot fail to store.
MAX_SOURCE_TEXT_LENGTH = 500

#: Which model read it (`qwen3-vl:8b`) and which transport (`ollama_native`),
#: recorded per candidate. Free text: the whole point of `VisionProvider` is that
#: swapping the model is configuration, and a new one must not need a migration to
#: be able to say it was there.
MAX_PROVIDER_LENGTH = 64
MAX_MODEL_LENGTH = 128


class IntakeIdentityCandidate(Base):
    """One part this photograph might be of, as a model proposed it. Never an answer."""

    __tablename__ = "intake_identity_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: `CASCADE`: a candidate is meaningless without the parked scan it describes,
    #: and these rows are proposals rather than history — nothing in the ledger's
    #: append-only reasoning applies to them. Same choice as
    #: `research_candidates.part_id` for the same reason.
    intake_id: Mapped[int] = mapped_column(
        ForeignKey("pending_intakes.id", ondelete="CASCADE"), nullable=False
    )

    #: Best first, from the model's own ordering. Recorded rather than recomputed:
    #: which candidate the model preferred is a property of the run, and re-deriving
    #: it from `confidence` would silently reorder ties.
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: The part number as printed. A free string, necessarily — the space of part
    #: numbers is unbounded, which is exactly why it can never be asserted. The only
    #: thing that will confirm it is `datasheet_validation` finding it in the text of
    #: a PDF that was actually fetched.
    mpn: Mapped[str] = mapped_column(String(MAX_MPN_LENGTH), nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(String(MAX_MANUFACTURER_LENGTH))
    package: Mapped[str | None] = mapped_column(String(MAX_PACKAGE_LENGTH))

    #: 0..1, about **reading characters off a photograph**: focus, glare, a crease
    #: through the third digit. Not about whether the part is plausible, and not the
    #: same quantity as `parameter_value_candidate.confidence`, which is about
    #: whether a datasheet states a value.
    #:
    #: They share a range and nothing else. `app.services.dispatch` clamps this
    #: strictly below `candidates.AUTO_PROMOTE_CONFIDENCE` before storing it, so a
    #: number from here cannot be mistaken for one that would promote a field — see
    #: that module, and ADR 0021's measurement of 0.95 on a wrong answer.
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    #: The characters on the label this reading came from, verbatim. **`NOT NULL`,
    #: and non-empty by service-level refusal** — see the module docstring. This is
    #: what a reviewer checks instead of taking the model's word.
    source_text: Mapped[str] = mapped_column(String(MAX_SOURCE_TEXT_LENGTH), nullable=False)

    #: Free text for the person reviewing: what was illegible, which sibling variants
    #: this could equally be. Never parsed, only shown.
    note: Mapped[str | None] = mapped_column(Text)

    #: The stub `parts` row minted for this candidate, when one was. `SET NULL`
    #: rather than cascade: deleting a stub part someone created by mistake must not
    #: delete the record of the model having proposed it.
    #:
    #: **Not** `pending_intakes.resolved_part_id`. See the module docstring — that
    #: column is a person's decision and this one is a machine's proposal, and the
    #: gap between them is the whole rule.
    part_id: Mapped[int | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))

    #: Which reader produced it. Stored per candidate rather than per entry so a
    #: re-run under a different model leaves the comparison visible in the rows
    #: instead of overwriting the fact that a different model answered differently.
    provider: Mapped[str | None] = mapped_column(String(MAX_PROVIDER_LENGTH))
    model: Mapped[str | None] = mapped_column(String(MAX_MODEL_LENGTH))

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # One row per proposed part number per entry. This is what makes a re-run
        # idempotent: reading the same photograph again updates each candidate in
        # place rather than appending a second opinion, so the row count stays "how
        # many distinct identities were ever proposed" instead of growing with every
        # retry. Exactly `uq_research_candidates_part_url`'s job.
        #
        # Keyed on the **printed** string, not a normalised one, matching
        # `vision.parse_response`'s own deduplication: two readings differing only in
        # punctuation are two genuinely different readings, and which is right is
        # what the datasheet fetch settles.
        UniqueConstraint("intake_id", "mpn", name="uq_intake_identity_candidates_intake_mpn"),
        # "Show me this entry's candidates, best first" — the intake panel's query,
        # and the only one that runs per request.
        Index("ix_intake_identity_candidates_intake", "intake_id", "rank"),
    )
