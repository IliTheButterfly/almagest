"""Fields a datasheet stated and this install has no template for. Never created.

`app.models.enrichment` is the pattern and its docstring is the argument: an
automated source writes a **candidate** and a promotion step decides. That table
does it for a part's *values*; this one does it one level up, for the *definition*
of a field, and the reason it needs its own table rather than a wider
`parameter_value_candidate` is that a proposal has no `part_id` and no
`template_id` — the template is precisely what does not exist yet.

## Why the risk here is larger than for a value, and shaped differently

A wrong `parameter_value_candidate` is one wrong number about one part, sitting in
a queue. A wrong `parameter_template` is shared by **every part in its category**,
and the failure is not a bad row but a **duplicate**: a second template for a
concept that already has one. `UNIQUE(part_id, template_id)` on `parameter_value`
is load-bearing for search fan-out, and it guarantees nothing useful once one
concept is spread over `Rds(on)` and `on_resistance` — the parametric filter then
reports parts as having no on-resistance while their value sits under the other
name, silently, and `parameter_template.substitution_direction` decides
substitutions on the half it can see. A wrong `base_unit` is the same shape from
the other end: every `value_min`/`value_max` ever written against the template is
then wrong while still answering range queries.

Neither of those is detectable after the fact, which is why the machine's whole
contribution is a row here plus the nearest existing template it could find, and
why `FieldProposalStatus` has no `created` member. `POST /api/parameter-fields`
remains the only door onto `parameter_template`; see
`app.services.parameter_fields`, whose four guards are invariants of the search
engine.

## The signal is "how many datasheets said this", so evidence is a second table

One row per proposed field, keyed on a normalised name, and one evidence row per
**document** that stated it. That split is the point of the schema rather than
normalisation for its own sake: a phrase appearing in five unrelated datasheets is
a real field, and the same phrase from one sheet is probably that manufacturer's
private vocabulary. Storing five proposal rows would put that count nowhere, and
storing one row with a counter column would put it somewhere unauditable — a
counter cannot be rebuilt and cannot be clicked through to the quote it came from.

## Every evidence row carries a verbatim quote, `NOT NULL`

Exactly `ExtractedField.source_text` and `IntakeIdentityCandidate.source_text`,
for exactly their reason: a proposal nobody can trace back to a line of a
datasheet cannot be reviewed, and an unreviewable assertion is what this whole
pipeline exists not to store. `app.services.field_proposals` refuses an empty
string, since an empty quote defeats the requirement as thoroughly as a NULL.

## No triggers, and no `CHECK` anywhere

This is a worklist, not a ledger — the same call `app.models.captures` makes. A
proposal is deleted or edited freely and nothing about the stock record depends on
it. Everything is `sa.String`/`Text`/`Float` with `StrEnumType`, so a new status
or a new match reason is one line and no table rebuild.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import FieldProposalMatch, FieldProposalStatus, Provenance
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: A proposed field name as stated, bounded to match `parameter_template.name`.
#: The same string may be copied straight into it when somebody accepts the
#: proposal, and two different widths would mean a name that fits here and is
#: truncated there.
MAX_FIELD_NAME_LENGTH = 128

#: A proposed display name, matching `parameter_template.display_name`.
MAX_DISPLAY_NAME_LENGTH = 255

#: A category slug, matching `parameter_template.applies_to_category`.
MAX_CATEGORY_LENGTH = 255

#: A quantity name, matching `parameter_template.base_unit`. Validated against the
#: value parser only when somebody actually creates the field — a suggestion the
#: parser does not recognise is a suggestion to correct, not a row to refuse.
MAX_BASE_UNIT_LENGTH = 64

#: The quoted characters. Matches `extract.schema_for`'s and
#: `field_discovery.schema_for`'s `maxLength` on the same field, so a response the
#: parser accepted cannot fail to store.
MAX_QUOTE_LENGTH = 500

#: The value the document stated alongside the heading — `45 mΩ`, `C0G`. Kept
#: verbatim and **never** written to `parameter_value`: there is no template to
#: write it against, and there will not be one until a person makes it. It is here
#: because it is the strongest hint about `value_type` and `base_unit` that exists,
#: and a reviewer reads it to check the suggestion.
MAX_STATED_VALUE_LENGTH = 255

#: A content hash, a URL fragment, or `mpn_decoder`-style source name. Wide enough
#: for a sha256 with room to spare.
MAX_DOCUMENT_REF_LENGTH = 255

MAX_PROVIDER_LENGTH = 64
MAX_MODEL_LENGTH = 128


class ParameterFieldProposal(Base):
    """One field something claimed a datasheet has and this install does not.

    The row is keyed on `name_norm`, so the same concept spelled three ways across
    three sheets is one proposal with three pieces of evidence rather than three
    proposals. See the module docstring for why that count is the thing a person
    judges on.
    """

    __tablename__ = "parameter_field_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The name as stated, verbatim, in whichever spelling was seen first. Kept
    #: lossless for the same reason `parameter_value_candidate.raw_value` is: it is
    #: the asset, and a normalisation rule that improves tomorrow can only be
    #: re-applied if the original string survived.
    name: Mapped[str] = mapped_column(String(MAX_FIELD_NAME_LENGTH), nullable=False)

    #: The dedup key — `app.services.field_proposals.normalise_field_name`.
    #: **UNIQUE**, which is what makes recording a proposal an upsert and makes a
    #: nightly re-run add evidence instead of rows.
    name_norm: Mapped[str] = mapped_column(
        String(MAX_FIELD_NAME_LENGTH), nullable=False, unique=True
    )

    #: What the field would be called in the filter panel. A suggestion.
    display_name: Mapped[str | None] = mapped_column(String(MAX_DISPLAY_NAME_LENGTH))

    #: Suggested `parameter_template.value_type`, as a bare string rather than a
    #: `StrEnumType(ValueType)`: a source may name something this install's enum
    #: does not have, and a proposal that could not be *stored* because its
    #: suggestion was unrecognisable would lose the evidence along with it. The
    #: create route validates; this column only carries.
    suggested_value_type: Mapped[str | None] = mapped_column(String(32))

    #: Suggested quantity name. Never trusted — see the module docstring on why a
    #: wrong `base_unit` is as bad as a duplicate.
    suggested_base_unit: Mapped[str | None] = mapped_column(String(MAX_BASE_UNIT_LENGTH))

    #: Suggested `part_categories.slug`. A model may not invent one:
    #: `field_discovery.schema_for` constrains it to an `enum` of slugs that exist,
    #: and `cross_check` fills it from the part's own category.
    suggested_category: Mapped[str | None] = mapped_column(String(MAX_CATEGORY_LENGTH))

    #: Suggested `substitution_direction`. Bare string for the reason
    #: `suggested_value_type` is, and deliberately **not** defaulted: the create
    #: route requires it with no default because defaulting a voltage rating to
    #: `exact` makes substitution wrong by construction, and a proposal must not
    #: launder that decision by arriving with one already filled in.
    suggested_substitution_direction: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(
        StrEnumType(FieldProposalStatus),
        nullable=False,
        default=FieldProposalStatus.PENDING,
        server_default=FieldProposalStatus.PENDING.value,
    )

    # -- the dedup check, stored ---------------------------------------------
    #
    # Recorded rather than recomputed per read, because it is evidence of what the
    # check *found when the proposal arrived* and because it is what the review
    # surface sorts and warns on. It is also cheap to refresh: recording another
    # piece of evidence re-runs the match, so a template created since the first
    # sighting is picked up on the next pass rather than lingering as a stale
    # "nothing resembles this".

    #: The closest existing template, or NULL when nothing resembled this.
    #: `SET NULL` rather than cascade: deleting a template must not delete the
    #: record of something having been proposed near it.
    nearest_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("parameter_template.id", ondelete="SET NULL")
    )

    #: Why it matched — a `FieldProposalMatch`. NULL exactly when
    #: `nearest_template_id` is NULL; the pair is maintained by
    #: `app.services.field_proposals.apply_nearest_match`, which is the only writer.
    nearest_match: Mapped[str | None] = mapped_column(StrEnumType(FieldProposalMatch))

    #: 0..1, from `MATCH_SCORES`. Stored so the list can order by "most likely to
    #: be a duplicate" without the reader knowing the ladder.
    nearest_score: Mapped[float | None] = mapped_column(Float)

    #: What accepting it produced — the template a person created or merged into.
    #: Written by the decision route from an id the **client** supplies after
    #: calling `POST /api/parameter-fields` itself, which is what keeps the number
    #: of doors onto `parameter_template` at one.
    created_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("parameter_template.id", ondelete="SET NULL")
    )

    #: Why a person dismissed it, for whoever sees it proposed again. Free text.
    decision_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    #: When the last piece of evidence arrived. Ordering key for "what turned up
    #: recently", which is not the same question as "what was first seen".
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    #: When it stopped being pending. NULL while it is in the queue, exactly as
    #: `parameter_value_candidate.decided_at` is.
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        # The review surface's own query: pending rows, most recently seen first.
        Index(
            "ix_field_proposals_pending",
            "status",
            "updated_at",
            sqlite_where=status == FieldProposalStatus.PENDING.value,
        ),
    )


class ParameterFieldProposalEvidence(Base):
    """One document that stated a proposed field, and the line it said it on."""

    __tablename__ = "parameter_field_proposal_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: `CASCADE`: evidence is meaningless without the proposal it supports, and
    #: neither is history. Same choice as `intake_identity_candidates.intake_id`.
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("parameter_field_proposals.id", ondelete="CASCADE"), nullable=False
    )

    #: Which kind of source said so. The same enum `parameter_value.provenance`
    #: uses, so `PROVENANCE_PRIORITY` orders these rows too and a reviewer reads
    #: `datasheet_table` and `llm_inferred` with the weight they already have.
    source: Mapped[str] = mapped_column(StrEnumType(Provenance), nullable=False)

    #: Which document — a datasheet sha256, normally.
    #:
    #: **`NOT NULL` with an empty-string default**, for the reason
    #: `ParameterValueCandidate.source_ref`'s docstring gives at length: NULLs
    #: compare distinct inside a UNIQUE index, so a nullable column here would let
    #: unlimited rows share one `(proposal, source)` while the constraint still
    #: *looked* present, and the seen-count would then grow with every re-run.
    document_ref: Mapped[str] = mapped_column(
        String(MAX_DOCUMENT_REF_LENGTH), nullable=False, default="", server_default=""
    )

    #: A part the field was stated for, when one is known. **Not part of the
    #: uniqueness key**, deliberately: one family datasheet covers many part
    #: numbers, and the count this table exists to produce is "how many
    #: *documents* said this", not "how many parts". So a second part from the same
    #: sheet refreshes this row rather than adding one, and this column ends up
    #: naming one of them rather than all of them. `SET NULL` for the same reason
    #: as `IntakeIdentityCandidate.part_id`.
    part_id: Mapped[int | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))

    #: The line, cell or heading the field name was read from, verbatim.
    #: **`NOT NULL`, and non-empty by service-level refusal** — see the module
    #: docstring. This is what a reviewer reads instead of taking the proposal's
    #: word for it.
    quote: Mapped[str] = mapped_column(String(MAX_QUOTE_LENGTH), nullable=False)

    #: The value printed under the heading, if any. The strongest available hint
    #: about type and unit, and never a value — see `MAX_STATED_VALUE_LENGTH`.
    stated_value: Mapped[str | None] = mapped_column(String(MAX_STATED_VALUE_LENGTH))

    page: Mapped[int | None] = mapped_column(Integer)

    #: Free text for the person reviewing: why this source thinks the field is
    #: worth having, what it was unsure about. Never parsed, only shown — the same
    #: role `IntakeIdentityCandidate.note` plays, and per sighting rather than per
    #: proposal so two sources' reservations do not overwrite each other.
    note: Mapped[str | None] = mapped_column(Text)

    #: Which reader produced it. Free text, per candidate rather than per proposal,
    #: so a second pass under a different model leaves the comparison visible in
    #: the rows — the same choice `IntakeIdentityCandidate` makes.
    provider: Mapped[str | None] = mapped_column(String(MAX_PROVIDER_LENGTH))
    model: Mapped[str | None] = mapped_column(String(MAX_MODEL_LENGTH))

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # One row per (proposal, source, document). This is what makes a re-run
        # idempotent: re-reading the same datasheet updates its own row in place
        # rather than appending a second sighting, so the count stays "how many
        # documents ever stated this" instead of growing with every pass. Exactly
        # `uq_parameter_value_candidate_observation`'s job, and the same trap
        # avoided — see `document_ref` on why it cannot be nullable.
        UniqueConstraint(
            "proposal_id",
            "source",
            "document_ref",
            name="uq_field_proposal_evidence_observation",
        ),
        # "Show me this proposal's evidence, newest first" — the review surface's
        # per-row query, and the count behind its headline number.
        Index("ix_field_proposal_evidence_proposal", "proposal_id", "created_at"),
    )
