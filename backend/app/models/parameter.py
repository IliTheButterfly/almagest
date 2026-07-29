"""Parametric attributes — indexed EAV, one row per (part, template).

EAV rather than JSON, decided deliberately. At a few thousand parts this table
is 30–150k rows and an indexed 3–5 join query is sub-millisecond in SQLite.
`json_extract` plus generated columns does not remove the schema churn — a new
*filterable* field still needs a generated column and a partial index — it just
relocates it, and loses FK integrity to `parameter_template` on the way.
`parts.extra_specs_json` is reserved for provider fields that are never
filtered.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import Provenance, SubstitutionDirection, ValueType
from app.models.types import StrEnumType


class ParameterTemplate(Base):
    """The definition of one filterable attribute."""

    __tablename__ = "parameter_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    value_type: Mapped[str] = mapped_column(StrEnumType(ValueType), nullable=False)

    #: pint-parseable, and the key `elec-value-parser` is given as its quantity
    #: argument: 'ohm', 'farad', 'volt'. This is what makes a bare `1M`
    #: resolve to 1 MΩ under resistance and get rejected under capacitance.
    base_unit: Mapped[str | None] = mapped_column(String(64))

    applies_to_category: Mapped[str | None] = mapped_column(String(255))

    #: What satisfies a requirement when searching for a substitute:
    #: `higher_ok` for a voltage rating, `lower_ok` for tolerance,
    #: `range_overlap` for capacitance, `exact` for package. Substitution
    #: search is the same filter executor with a swapped operator table — there
    #: is no second query engine, and it stays deterministic.
    substitution_direction: Mapped[str] = mapped_column(
        StrEnumType(SubstitutionDirection),
        nullable=False,
        default=SubstitutionDirection.EXACT,
        server_default=SubstitutionDirection.EXACT.value,
    )

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Plausibility guard, independent of the parser's own. Belt and braces on
    #: the single most expensive class of error: a unit misread.
    plausible_min: Mapped[float | None] = mapped_column(Float)
    plausible_max: Mapped[float | None] = mapped_column(Float)

    #: Part of the shared definition library every install starts with, so
    #: `name`, `value_type` and `base_unit` are frozen — the MPN decoders, the
    #: datasheet extractors and the demo data all name `capacitance` and mean
    #: farads.
    #:
    #: **Deliberately not the clone-on-edit treatment `container_types` has.**
    #: `name` is globally UNIQUE, so a clone would have to be called something
    #: else — `capacitance-copy` — which no decoder, no saved search URL and no
    #: extractor refers to, while both rows would then appear side by side in
    #: every facet panel as two fields meaning the same thing. For a *type* a
    #: divergent copy is the point; for a *field definition* it is the failure.
    #: So a seed refuses the three identity-bearing edits and permits the rest
    #: (display name, ordering, plausibility window, substitution direction),
    #: none of which can invalidate a stored value.
    is_seed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )


class ParameterChoice(Base):
    """One option of an enum-typed template.

    Composite keys carry dual notation: `key='0603_1608'`,
    `label='0603 (imperial) / 1608 (metric)'`. Either spelling resolves to the
    same row, so the user is never asked which convention a source used.
    """

    __tablename__ = "parameter_choice"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("parameter_template.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    #: JSON array of alternative spellings that resolve to this choice.
    aliases_json: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("template_id", "key"),)


class ParameterValue(Base):
    """One attribute of one part.

    `UNIQUE(part_id, template_id)` is **load-bearing**, not hygiene: it
    guarantees each join contributes at most one row, so a multi-predicate
    parametric query is plain `JOIN`s that never fan out. Drop it and every
    search silently returns a cross product.

    Enum facets (dielectric, mounting, package) live in this same table via
    `choice_id` rather than in their own, so search, provenance and review have
    exactly one code path.
    """

    __tablename__ = "parameter_value"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("parameter_template.id", ondelete="CASCADE"), nullable=False
    )

    #: Exactly what was entered — '4k7', '20-30uF'. Kept verbatim so display is
    #: lossless and a re-parse after a grammar fix is always possible.
    raw_input: Mapped[str] = mapped_column(Text, nullable=False)

    value_nominal: Mapped[float | None] = mapped_column(Float)
    #: **Always populated for numeric values**, degenerate (min == max) for a
    #: plain scalar. Parametric search is an interval-overlap test, so a row
    #: with null bounds is invisible to every range query.
    value_min: Mapped[float | None] = mapped_column(Float)
    value_max: Mapped[float | None] = mapped_column(Float)
    value_typ: Mapped[float | None] = mapped_column(Float)
    tolerance_pct: Mapped[float | None] = mapped_column(Float)

    #: Engineering-notation components, so '4700 Ω' renders as '4.7 kΩ' without
    #: recomputing, and without storing a formatted string that cannot be
    #: re-unitised.
    display_mantissa: Mapped[float | None] = mapped_column(Float)
    display_si_prefix: Mapped[str | None] = mapped_column(String(8))
    display_unit_symbol: Mapped[str | None] = mapped_column(String(16))

    choice_id: Mapped[int | None] = mapped_column(
        ForeignKey("parameter_choice.id", ondelete="RESTRICT")
    )
    value_text: Mapped[str | None] = mapped_column(Text)
    value_bool: Mapped[bool | None] = mapped_column(Integer)

    provenance: Mapped[str] = mapped_column(
        StrEnumType(Provenance),
        nullable=False,
        default=Provenance.MANUAL,
        server_default=Provenance.MANUAL.value,
    )
    confidence: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("part_id", "template_id", name="uq_parameter_value_part_template"),
        # Partial indexes: a numeric template's rows have no choice_id and vice
        # versa, so a full index would be mostly NULLs on both.
        Index(
            "ix_pv_tmpl_num",
            "template_id",
            "value_nominal",
            sqlite_where=value_nominal.isnot(None),
        ),
        Index("ix_pv_tmpl_ch", "template_id", "choice_id", sqlite_where=choice_id.isnot(None)),
        # Serves the range-overlap predicate directly.
        Index(
            "ix_pv_tmpl_range",
            "template_id",
            "value_min",
            "value_max",
            sqlite_where=value_min.isnot(None),
        ),
    )
