"""Proposed fields: recording them, and the duplicate check that makes them safe.

`app.services.parameter_fields` is the only writer of `parameter_template` and
this module is deliberately not a second one — **nothing here imports
`create_template` and nothing here constructs a `ParameterTemplate`**, which
`tests/integration/test_field_proposals.py` asserts by grepping. A proposal is a
worklist entry; a person presses a button; `POST /api/parameter-fields` does the
writing it always did.

## The dedup check is the feature, not a nicety

The dangerous failure of letting anything propose fields is not a bad row. It is a
**duplicate**: `Rds(on)` proposed while `on_resistance` already exists. Two
templates for one concept and:

* parametric search fragments — a part with a value under one name reads as having
  no value at all under the other, silently, because the filter is a join on
  `template_id`;
* `parameter_template.substitution_direction` decides substitutions on whichever
  half it can see, and "a plausible substitute with the wrong rating is a field
  failure";
* `UNIQUE(part_id, template_id)` on `parameter_value` still holds and guarantees
  nothing useful, because the fan-out it prevents was never the problem.

None of that surfaces as an error. So `nearest_existing_template` runs before a
proposal is ever shown, and the review surface offers *merge into the existing
field* ahead of *create this one*.

### The ladder, strongest first

`EXACT_NAME` -> `DISPLAY_NAME` -> `SYNONYM` -> `TOKENS`, and nothing below that.
The first three are string arithmetic over a normalisation that throws away case,
punctuation and separators, so `Voltage Rating`, `voltage-rating` and
`VOLTAGE_RATING` are one name. `SYNONYM` is the one rung that encodes electronics
knowledge, and it is **curated content in this file** rather than anything
inferred: `SYNONYM_GROUPS` is a list somebody maintains, and a wrong entry there
merges two real fields, which is why it is short and conservative.

`TOKENS` requires **both** a shared word stem and a compatible quantity, and the
unit half is not decoration. `rise_time` and `rise_rate` share a stem and are
different fields; seconds versus volts-per-second is what says so. A proposal with
no suggested unit is treated as compatible with everything, because "unknown" must
not read as "different" — the cost of a false near-match is a person reading one
extra line, and the cost of a missed one is the duplicate this module exists to
prevent.

## Recording is an upsert, twice over

By `name_norm` for the proposal, and by `(proposal, source, document_ref)` for the
evidence. A nightly pass over the same datasheets therefore adds nothing the second
time, which matters because the count of evidence rows **is** the signal a person
judges on: a heading seen in five unrelated sheets is a real field, and the same
heading five times from one sheet is one observation. See
`app.models.field_proposals`.

Recording also **re-runs the match**. A template created since the first sighting
turns a proposal from "nothing resembles this" into "merge into that one" on the
next pass, rather than leaving a stale answer behind that a reviewer would trust.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import (
    FieldProposalMatch,
    FieldProposalStatus,
    Provenance,
    ValueType,
)
from app.models.field_proposals import (
    MAX_QUOTE_LENGTH,
    ParameterFieldProposal,
    ParameterFieldProposalEvidence,
)
from app.models.parameter import ParameterTemplate
from app.models.types import utcnow
from app.services.search.value_parser import canonical_quantity

#: How strong each rung of the ladder is. Ordering only — nothing compares these to
#: a threshold and no automatic action is taken at any value, because the whole
#: design is that a person decides. They exist so the review surface can put the
#: likeliest duplicates at the top of the list.
MATCH_SCORES: dict[FieldProposalMatch, float] = {
    FieldProposalMatch.EXACT_NAME: 1.0,
    FieldProposalMatch.DISPLAY_NAME: 0.95,
    FieldProposalMatch.SYNONYM: 0.9,
    FieldProposalMatch.TOKENS: 0.6,
}

#: Word stems that carry no distinguishing information, dropped before the token
#: comparison. Kept tiny: an over-eager stop list makes `max_voltage` and
#: `min_voltage` the same field, which is a merge nobody would notice being wrong.
_TOKEN_NOISE = frozenset({"the", "of", "a", "value"})

#: How much of the smaller token set must be shared for `TOKENS` to fire. Two
#: thirds, so a single accidental shared word between two two-word names is not
#: enough on its own — `rise_time` and `fall_time` share `time` and are a third
#: each, well under the bar.
TOKEN_OVERLAP_THRESHOLD = 0.66

#: Names of one concept, curated by hand.
#:
#: **This is content, and it is the only place in this module where electronics
#: knowledge lives.** It exists because the duplicate that actually happens is not
#: a spelling variant a normaliser catches — it is a datasheet's symbol against an
#: inventory's word: `Rds(on)` against `on_resistance`, `ESR` against
#: `equivalent_series_resistance`, `Vf` against `forward_voltage`. No amount of
#: string comparison connects those, and a model must not be asked to, because a
#: model deciding that two fields are the same concept is the assertion this whole
#: feature refuses to accept.
#:
#: Short and conservative on purpose. A wrong entry here silently merges two real
#: fields, which is the same class of harm as the duplicate it prevents; every
#: group is a pair or triple somebody can check at a glance. Entries are given in
#: human spelling and normalised at import, so `Rds(on)` may be written as it is
#: printed.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("Rds(on)", "on_resistance", "drain_source_on_resistance"),
    ("ESR", "equivalent_series_resistance"),
    ("Vf", "forward_voltage"),
    ("Vgs(th)", "gate_threshold_voltage"),
    ("Ids", "continuous_drain_current"),
    ("Vceo", "collector_emitter_voltage"),
    ("hFE", "dc_current_gain"),
    ("Iq", "quiescent_current"),
    ("PSRR", "power_supply_rejection_ratio"),
    ("GBW", "gain_bandwidth_product"),
    ("Rth(j-a)", "thermal_resistance_junction_ambient"),
    ("tolerance", "value_tolerance"),
    ("tempco", "temperature_coefficient"),
    ("Vdrop", "voltage_drop"),
)


class FieldProposalError(ValueError):
    """A proposal that cannot be recorded, with a reason code.

    Same shape as `parameter_fields.AuthoringError` and for its reason: the route
    maps the code to a status and the UI routes on it, so a test asserts the reason
    rather than wording it would then have to keep in step with.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalise_field_name(name: str) -> str:
    """The dedup key: lowercase, alphanumerics only, everything else dropped.

    `Voltage Rating`, `voltage-rating`, `VOLTAGE_RATING` and `Voltage  rating.` all
    become `voltagerating`. Separators are removed rather than collapsed to one
    character because the spellings that collide in practice disagree about
    *whether* there is a separator at all — `Rds(on)` and `RdsOn` — and a rule that
    kept them would put those in different buckets.

    Digits survive: `voltage_2` and `voltage_3` are different fields, and a
    normaliser that dropped the number would silently merge them.
    """
    return _NON_ALNUM.sub("", name.strip().casefold())


def _tokens(name: str) -> frozenset[str]:
    """Word stems, for the weakest rung of the ladder.

    Split on the separators `normalise_field_name` throws away plus a
    lower-to-upper camel-case boundary, so `RdsOn` and `rds_on` produce the same
    two tokens. Trailing plural `s` is dropped so `resistances` and `resistance`
    match; nothing more aggressive, because stemming `rating` to `rate` would merge
    a voltage rating with a slew rate.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    parts = [part for part in _NON_ALNUM.sub(" ", spaced.casefold()).split() if part]
    stems = {part[:-1] if len(part) > 3 and part.endswith("s") else part for part in parts}
    return frozenset(stem for stem in stems if stem not in _TOKEN_NOISE)


_SYNONYM_LOOKUP: dict[str, frozenset[str]] = {}
for _group in SYNONYM_GROUPS:
    _normed = frozenset(normalise_field_name(member) for member in _group)
    for _member in _normed:
        _SYNONYM_LOOKUP[_member] = _normed


def are_synonyms(left: str, right: str) -> bool:
    """Whether two names sit in the same curated `SYNONYM_GROUPS` entry."""
    group = _SYNONYM_LOOKUP.get(normalise_field_name(left))
    return group is not None and normalise_field_name(right) in group


def units_compatible(left: str | None, right: str | None) -> bool:
    """Whether two `base_unit` suggestions could describe one field.

    An unknown unit on either side is compatible with everything. That asymmetry is
    deliberate: `cross_check` records proposals with no suggested unit at all, and
    treating "unknown" as "different" would suppress exactly the near-matches this
    check exists to surface. A *stated* mismatch — ohms against farads — is a real
    difference and is the thing that stops `TOKENS` firing on two fields that share
    a word.

    Compared canonically, so `OHM` and `ohm` are one quantity. A spelling the
    parser does not recognise falls back to a casefolded string comparison rather
    than being treated as unknown: two sources agreeing on `siemens` before this
    install has the quantity are still agreeing.
    """
    if not left or not right:
        return True
    return (canonical_quantity(left) or left.casefold()) == (
        canonical_quantity(right) or right.casefold()
    )


# ---------------------------------------------------------------------------
# The dedup check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NearestMatch:
    """The closest existing template to a proposed name, and why."""

    template: ParameterTemplate
    match: FieldProposalMatch
    score: float


def nearest_existing_template(
    session: Session,
    name: str,
    *,
    base_unit: str | None = None,
    exclude_id: int | None = None,
) -> NearestMatch | None:
    """The existing `parameter_template` a proposal is most likely a duplicate of.

    **The safety mechanism of this whole feature**, and a named function rather than
    an inline comparison for exactly that reason: it has one test per rung and the
    one that matters is a differently-spelled duplicate coming back with the
    existing template attached. See the module docstring for the ladder and for why
    `TOKENS` needs the unit as well as the words.

    Returns `None` when nothing resembles the name, which is a normal and common
    answer — most of what a datasheet states genuinely is a new field.
    """
    wanted = normalise_field_name(name)
    if not wanted:
        raise FieldProposalError(
            f"{name!r} normalises to nothing, so it can be neither matched nor stored: "
            "a field name needs at least one letter or digit.",
            reason="empty_name",
        )
    wanted_tokens = _tokens(name)

    best: NearestMatch | None = None
    for template in session.execute(select(ParameterTemplate)).scalars():
        if exclude_id is not None and template.id == exclude_id:
            continue
        match = _match_for(template, wanted, wanted_tokens, base_unit)
        if match is None:
            continue
        score = MATCH_SCORES[match]
        if best is None or score > best.score:
            best = NearestMatch(template=template, match=match, score=score)
        if best.score >= MATCH_SCORES[FieldProposalMatch.EXACT_NAME]:
            # Nothing can beat an exact name, and `parameter_template.name` is
            # UNIQUE, so there is no second one to find.
            break
    return best


def _match_for(
    template: ParameterTemplate,
    wanted: str,
    wanted_tokens: frozenset[str],
    base_unit: str | None,
) -> FieldProposalMatch | None:
    if normalise_field_name(template.name) == wanted:
        return FieldProposalMatch.EXACT_NAME
    if normalise_field_name(template.display_name) == wanted:
        return FieldProposalMatch.DISPLAY_NAME
    if _SYNONYM_LOOKUP.get(wanted, frozenset()) & {
        normalise_field_name(template.name),
        normalise_field_name(template.display_name),
    }:
        return FieldProposalMatch.SYNONYM
    if not units_compatible(base_unit, template.base_unit):
        # A stated unit mismatch ends it here. Two fields measuring different
        # quantities are two fields however much their names overlap, and merging
        # across that is the `base_unit`-shaped half of the harm this check exists
        # to prevent.
        return None
    theirs = _tokens(template.name) | _tokens(template.display_name)
    shared = wanted_tokens & theirs
    smaller = min(len(wanted_tokens), len(theirs))
    if shared and smaller and len(shared) / smaller >= TOKEN_OVERLAP_THRESHOLD:
        return FieldProposalMatch.TOKENS
    return None


def apply_nearest_match(session: Session, proposal: ParameterFieldProposal) -> None:
    """Refresh the recorded match. The only writer of the three `nearest_*` columns.

    All three move together or the row lies: a `nearest_match` without a template,
    or a score left over from a template that has since been deleted, is worse than
    no answer because a reviewer reads it as one.
    """
    found = nearest_existing_template(
        session, proposal.name, base_unit=proposal.suggested_base_unit
    )
    proposal.nearest_template_id = None if found is None else found.template.id
    proposal.nearest_match = None if found is None else found.match
    proposal.nearest_score = None if found is None else found.score


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def evidence_of(
    session: Session, proposal: ParameterFieldProposal
) -> list[ParameterFieldProposalEvidence]:
    return list(
        session.execute(
            select(ParameterFieldProposalEvidence)
            .where(ParameterFieldProposalEvidence.proposal_id == proposal.id)
            .order_by(
                ParameterFieldProposalEvidence.created_at.desc(),
                ParameterFieldProposalEvidence.id.desc(),
            )
        ).scalars()
    )


def seen_count(session: Session, proposal: ParameterFieldProposal) -> int:
    """How many documents stated this field. **Derived, never stored.**

    The headline number a person judges on, and a counter column would be the one
    thing here that cannot be rebuilt from anything — `CLAUDE.md`'s rule about
    reservations, applied to a much smaller number. It is a `COUNT` over a handful
    of rows behind an index; the read is not worth caching.
    """
    return int(
        session.execute(
            select(func.count())
            .select_from(ParameterFieldProposalEvidence)
            .where(ParameterFieldProposalEvidence.proposal_id == proposal.id)
        ).scalar_one()
    )


def find_by_name(session: Session, name: str) -> ParameterFieldProposal | None:
    return session.execute(
        select(ParameterFieldProposal).where(
            ParameterFieldProposal.name_norm == normalise_field_name(name)
        )
    ).scalar_one_or_none()


def list_proposals(
    session: Session,
    *,
    status: FieldProposalStatus | None = None,
    limit: int = 100,
) -> list[ParameterFieldProposal]:
    """The review surface's list: likeliest duplicates first, then most recent.

    Ordered by `nearest_score` descending with NULLs last, so the rows a person
    must *not* wave through — the ones that already have a field — are the ones they
    see first. A proposal with no near match is new information and can wait.
    """
    query = select(ParameterFieldProposal)
    if status is not None:
        query = query.where(ParameterFieldProposal.status == status)
    rows = list(session.execute(query).scalars())
    rows.sort(
        key=lambda row: (
            -(row.nearest_score or 0.0),
            -(row.updated_at.timestamp() if row.updated_at else 0.0),
            row.id,
        )
    )
    return rows[:limit]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceReport:
    """One document's sighting of a proposed field, as the service receives it.

    A plain dataclass rather than the route's Pydantic model, so the service does
    not import the wire types — the direction `parameter_fields` and
    `layout_authoring` both keep.
    """

    quote: str
    document_ref: str = ""
    source: Provenance = Provenance.LLM_INFERRED
    part_id: int | None = None
    stated_value: str | None = None
    page: int | None = None
    note: str | None = None
    provider: str | None = None
    model: str | None = None


def record_proposal(
    session: Session,
    *,
    name: str,
    evidence: Sequence[EvidenceReport],
    display_name: str | None = None,
    suggested_value_type: ValueType | str | None = None,
    suggested_base_unit: str | None = None,
    suggested_category: str | None = None,
    suggested_substitution_direction: str | None = None,
) -> ParameterFieldProposal:
    """Upsert a proposal and its evidence, and re-run the duplicate check.

    Idempotent in both directions — see the module docstring. The suggestions are
    **filled in only where the row has nothing**, never overwritten: the first
    source to state a unit is usually the one that read a table header, and a later
    pass that saw only the phrase should not be able to blank it out. A better
    suggestion is a person's edit in the create form, which is where it belongs.

    Nothing here validates `suggested_base_unit` against the parser. That check
    lives in `parameter_fields.validated_base_unit` and fires when the field is
    actually created; refusing to *store* an unrecognised suggestion would throw
    away the quote along with it, and the quote is the part worth keeping.
    """
    if not evidence:
        raise FieldProposalError(
            "a proposal needs at least one piece of evidence: a quote from the document "
            "that stated the field. A proposal nobody can trace to a datasheet cannot "
            "be reviewed.",
            reason="no_evidence",
        )
    norm = normalise_field_name(name)
    if not norm:
        raise FieldProposalError(
            f"{name!r} normalises to nothing: a field name needs at least one letter or digit.",
            reason="empty_name",
        )

    proposal = find_by_name(session, name)
    now = utcnow()
    if proposal is None:
        proposal = ParameterFieldProposal(
            name=name.strip(),
            name_norm=norm,
            display_name=display_name,
            suggested_value_type=_as_str(suggested_value_type),
            suggested_base_unit=suggested_base_unit,
            suggested_category=suggested_category,
            suggested_substitution_direction=suggested_substitution_direction,
            status=FieldProposalStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        session.add(proposal)
        session.flush()
    else:
        proposal.display_name = proposal.display_name or display_name
        proposal.suggested_value_type = proposal.suggested_value_type or _as_str(
            suggested_value_type
        )
        proposal.suggested_base_unit = proposal.suggested_base_unit or suggested_base_unit
        proposal.suggested_category = proposal.suggested_category or suggested_category
        proposal.suggested_substitution_direction = (
            proposal.suggested_substitution_direction or suggested_substitution_direction
        )
        proposal.updated_at = now

    for report in evidence:
        _record_evidence(session, proposal, report, now=now)

    apply_nearest_match(session, proposal)
    session.flush()
    return proposal


def _record_evidence(
    session: Session,
    proposal: ParameterFieldProposal,
    report: EvidenceReport,
    *,
    now: datetime,
) -> None:
    quote = report.quote.strip()
    if not quote:
        # The requirement `ExtractedField.source_text` and
        # `IntakeIdentityCandidate.source_text` both carry, for their reason: an
        # empty quote defeats it as thoroughly as a NULL would, so it is refused
        # here rather than stored and discovered by a reviewer with nothing to read.
        raise FieldProposalError(
            "every piece of evidence needs a verbatim quote from the document. If you "
            "cannot quote the line that stated this field, you did not read it.",
            reason="empty_quote",
        )
    if len(quote) > MAX_QUOTE_LENGTH:
        raise FieldProposalError(
            f"a quote is at most {MAX_QUOTE_LENGTH} characters; this one is {len(quote)}. "
            "Quote the line, not the page.",
            reason="quote_too_long",
        )

    existing = session.execute(
        select(ParameterFieldProposalEvidence).where(
            ParameterFieldProposalEvidence.proposal_id == proposal.id,
            ParameterFieldProposalEvidence.source == report.source,
            ParameterFieldProposalEvidence.document_ref == report.document_ref,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            ParameterFieldProposalEvidence(
                proposal_id=proposal.id,
                source=report.source,
                document_ref=report.document_ref,
                part_id=report.part_id,
                quote=quote,
                stated_value=report.stated_value,
                page=report.page,
                note=report.note,
                provider=report.provider,
                model=report.model,
                created_at=now,
            )
        )
        return
    # A second sighting from the same document is the same observation seen again,
    # so it overwrites in place — the uniqueness key's whole purpose. The newest
    # reading wins: a re-run under a better model is the case that matters, and it
    # is the one whose quote should be on the screen.
    existing.part_id = report.part_id or existing.part_id
    existing.quote = quote
    existing.stated_value = report.stated_value or existing.stated_value
    existing.page = report.page if report.page is not None else existing.page
    existing.note = report.note or existing.note
    existing.provider = report.provider or existing.provider
    existing.model = report.model or existing.model


def accept(
    proposal: ParameterFieldProposal,
    *,
    template: ParameterTemplate,
    note: str | None = None,
) -> None:
    """Record that a person created or merged into `template`.

    **Takes a template that already exists.** It does not create one, and there is
    no argument here from which one could be created — the caller made it through
    `POST /api/parameter-fields`, with every guard in
    `app.services.parameter_fields` applied. That is the same shape the intake queue
    keeps by routing acceptance through the existing `resolve` route instead of
    growing a second door.
    """
    proposal.status = FieldProposalStatus.ACCEPTED
    proposal.created_template_id = template.id
    proposal.decision_note = note
    proposal.decided_at = utcnow()


def dismiss(proposal: ParameterFieldProposal, *, note: str | None = None) -> None:
    """Record that a person said no. Kept, not deleted — see `FieldProposalStatus`."""
    proposal.status = FieldProposalStatus.DISMISSED
    proposal.created_template_id = None
    proposal.decision_note = note
    proposal.decided_at = utcnow()


def _as_str(value: ValueType | str | None) -> str | None:
    if value is None:
        return None
    return str(value)
