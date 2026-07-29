"""Candidates in, and the rules that decide what crosses into `parameter_value`.

This module is the whole reason enrichment is safe to run unattended. Every
automated source — a provider API, a datasheet table, an MPN decoder, a model —
writes a `parameter_value_candidate` here and **never touches
`parameter_value`**. `evaluate()` then applies four rules, in this order:

1. An observation that must not auto-promote (a marking, an OCR'd or model-read
   part number) never does, whatever its confidence says.
2. If the field is **empty** and two or more **distinct** pending sources
   **agree within tolerance**, the most trusted of them is promoted — agreement
   between two independent sources is stronger evidence than either one's
   self-reported confidence, so the 0.8 threshold does not apply to this case.
3. If the field is **empty** and every usable row comes from **one** source, it
   is promoted only when `confidence >= 0.8`. "Distinct source" is counted over
   `source`, never over rows: one source legitimately holds several observations
   of a field (two datasheet revisions, two rows of a variant table), and a
   reading that could corroborate itself by turning up twice would make the 0.8
   bar bypassable by any source that runs nightly.
4. Everything else is a review-queue item, including — deliberately — a
   higher-priority source that arrives after a lower-priority value was already
   promoted. See `evaluate()` for why that is a queue item and not an
   overwrite.

There is deliberately **no majority vote**: two sources agreeing while a third
dissents does not promote. A dissent is the signal that something is wrong with
one of three readings, and outvoting it discards exactly the evidence that made
the field worth a human's attention. Sources here are not independent enough for
a vote to mean much anyway — distributor free text is frequently copied from the
same datasheet the extractor read.

Promotion always goes through `app.services.parameters`, never through a direct
write. That is not tidiness: `set_numeric` is what populates
`value_min`/`value_max`, and a numeric row with null bounds is invisible to
every range query in the system, silently. A promotion path that inserted its
own row would reintroduce exactly that bug for automated data only, which is
the data nobody eyeballs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from elec_value_parser import ValueParseError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enrichment import ParameterValueCandidate
from app.models.enums import (
    PROVENANCE_PRIORITY,
    CandidateReviewReason,
    CandidateStatus,
    PromotionOutcome,
    Provenance,
    ValueType,
)
from app.models.parameter import ParameterTemplate, ParameterValue
from app.models.types import utcnow
from app.services import parameters
from app.services.enrichment.mpn_decoders import DecodedPart
from app.services.search.value_parser import parse_for_template

#: The design doc's threshold, and it applies to exactly one case: a single
#: source proposing into an empty field. Agreement between sources promotes
#: without consulting it.
AUTO_PROMOTE_CONFIDENCE = 0.8

#: How close two numerics must be to count as the same value.
#:
#: **0.1% relative, which is a float-noise band and emphatically not a
#: component-tolerance band.** The reasoning:
#:
#: * `100 nF` and `0.1 uF` are the *same number* — the parser converts both to
#:   1e-7 F exactly — so the only slack genuinely required is floating-point
#:   round-trip noise, on the order of 1e-12 relative. 1e-3 is a thousand times
#:   that, so no unit spelling can ever fail to agree with itself.
#: * `100 nF` and `104 nF` are 4% apart. That is inside a ±10% part's tolerance
#:   band, and treating them as agreeing is the tempting mistake. They are
#:   nonetheless **different values**: `104` is the printed marking *for* 100 nF,
#:   so a source reporting 104 nF has almost certainly mis-read a multiplier —
#:   and letting a mis-read confirm a mis-extraction is precisely the invisible
#:   corruption this table exists to prevent. It goes to review.
#: * The hard ceiling: adjacent E96 values are ~2.3% apart. Any epsilon at or
#:   above half of that would fold two distinct catalogue values into one, so
#:   1e-3 sits an order of magnitude below the smallest real gap the E-series
#:   permits. E-series membership is the strongest error-correction signal
#:   available anywhere in this system, and this is the same argument the
#:   colour-band reader uses.
#:
#: Being wrong in the conservative direction costs a review-queue item. Being
#: wrong in the other direction is undetectable.
AGREEMENT_REL_EPS = 1e-3

#: Shown in the queue beside a `>=50V`-style reading, so the reviewer is told
#: what to type instead rather than merely that the row was refused.
ONE_SIDED_LIMIT_NOTE = (
    "read as a one-sided limit, which states a bound rather than this part's value: "
    "search is an interval-overlap test, so storing it would leave the other bound "
    "empty and the part would match no range query at all. Correct it to a value or "
    "a range."
)

#: An MPN decode is a lookup in the manufacturer's own published numbering
#: table, so it clears the auto-promote bar on its own. Not 1.0, because the
#: *transcription* of that table into this repository is hand-made and a
#: transcription error is the realistic failure mode.
MPN_DECODER_CONFIDENCE = 0.9


class UnsupportedTemplateType(ValueError):
    """A candidate was offered for a `text` or `bool` template.

    `app.services.parameters` has writers for numeric and enum values only, and
    promotion must go through it. Accepting a candidate that could never be
    promoted would either strand rows in the queue forever or tempt a second
    write path onto `parameter_value` — and a second write path is how
    `value_min`/`value_max` stop being guaranteed.
    """


@dataclass(frozen=True)
class Decision:
    """The result of evaluating one field's candidates."""

    outcome: PromotionOutcome
    #: The candidate whose value is now in `parameter_value`, when one was
    #: promoted by this call.
    promoted: ParameterValueCandidate | None = None
    value: ParameterValue | None = None
    #: Why nothing was promoted, when nothing was.
    reason: CandidateReviewReason | None = None
    #: Candidates left in the queue by this call, most trusted first.
    queued: tuple[ParameterValueCandidate, ...] = ()

    @property
    def needs_review(self) -> bool:
        return self.outcome is PromotionOutcome.QUEUED


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    raw_value: str,
    *,
    source: Provenance,
    confidence: float,
    source_ref: str = "",
    requires_human: bool = False,
    note: str | None = None,
) -> ParameterValueCandidate:
    """Write (or update) one source's proposal for one field.

    **Idempotent on the observation**, which is `(part, template, source,
    source_ref)`. Re-running the same extraction over the same datasheet
    updates that one row rather than adding another, because "how many sources
    agree" is counted from these rows — three copies of one observation must
    never manufacture a consensus.

    Two sources proposing different values for the same field are two rows and
    both survive; that disagreement is the most important thing this table
    represents.

    The value is normalised here, not at promotion time: agreement between
    sources is then a comparison of numbers (`100 nF` vs `0.1 uF` compare equal
    without re-parsing), and a value the grammar cannot handle is caught while
    the source and the raw text are still to hand. An unparseable value is still
    **stored** — the raw string is the asset — as a pending
    `UNPARSEABLE` queue item.

    A previously `DISMISSED` row reopens only if the value actually changed. A
    dismissal attaches to a value a human rejected, not to the
    `(part, template, source)` triple forever: the same wrong number must stay
    dismissed on every re-run, and a genuinely new number from that source
    deserves a fresh look.

    "Actually changed" is measured on the **normalised value**, never on the raw
    string. `100nF` and `0.1uF` are the same number — that equivalence is the
    reason this table normalises on write at all — so a source that respells a
    value between runs has not changed it, and reopening on the spelling would
    quietly undo a human's "no" and hand the value straight back to the
    promotion rules with no human present. Only when neither side normalised to
    anything (both unparseable) does the raw text decide, because then it is the
    only evidence there is and nothing here is entitled to claim two strings it
    cannot parse mean the same thing.
    """
    value_type = parameters.value_type_of(template)
    if value_type not in (ValueType.NUMERIC, ValueType.ENUM):
        raise UnsupportedTemplateType(
            f"template {template.name!r} is {value_type}; candidates support numeric and enum only"
        )

    nominal, low, high, choice_id, parse_note = _normalise(session, template, raw_value, value_type)

    row = session.execute(
        select(ParameterValueCandidate).where(
            ParameterValueCandidate.part_id == part.id,
            ParameterValueCandidate.template_id == template.id,
            ParameterValueCandidate.source == source,
            ParameterValueCandidate.source_ref == source_ref,
        )
    ).scalar_one_or_none()

    if row is None:
        row = ParameterValueCandidate(
            part_id=part.id,
            template_id=template.id,
            source=source,
            source_ref=source_ref,
            raw_value=raw_value,
            confidence=confidence,
            # Set explicitly, not left to the column default: a mapped default
            # is not applied until INSERT, so the checks below would read `None`
            # and quietly decline to record the review reason.
            status=CandidateStatus.PENDING,
        )
        session.add(row)
    else:
        if _asserts_a_different_value(
            _Normalised(nominal, low, high, choice_id),
            raw_value,
            _Normalised(row.value_nominal, row.value_min, row.value_max, row.choice_id),
            row.raw_value,
        ):
            row.status = CandidateStatus.PENDING
            row.review_reason = None
            row.decided_at = None

    row.raw_value = raw_value
    row.confidence = confidence
    row.value_nominal = nominal
    row.value_min = low
    row.value_max = high
    row.choice_id = choice_id
    row.requires_human = requires_human

    one_sided = parse_note is None and choice_id is None and (low is None) != (high is None)
    detail = parse_note if parse_note is not None else (ONE_SIDED_LIMIT_NOTE if one_sided else None)
    row.note = note if detail is None else "; ".join(filter(None, (note, detail)))

    if row.status == CandidateStatus.PENDING:
        # Ordered by what the reviewer can do about it, most restrictive first.
        # `UNPARSEABLE` and `ONE_SIDED_LIMIT` both mean *nothing here is
        # acceptable as written* — the only way forward is a correction — whereas
        # `REQUIRES_HUMAN` means the value may well be accepted verbatim, just
        # never by a rule. That difference is what the screen disables the Accept
        # button on, so it has to win the single reason slot.
        if parse_note is not None:
            row.review_reason = CandidateReviewReason.UNPARSEABLE
        elif one_sided:
            row.review_reason = CandidateReviewReason.ONE_SIDED_LIMIT
        elif requires_human:
            row.review_reason = CandidateReviewReason.REQUIRES_HUMAN

    session.flush()
    return row


def submit(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    raw_value: str,
    *,
    source: Provenance,
    confidence: float,
    source_ref: str = "",
    requires_human: bool = False,
    note: str | None = None,
) -> Decision:
    """`record()` then `evaluate()` — the ordinary way a source offers a value."""
    record(
        session,
        part,
        template,
        raw_value,
        source=source,
        confidence=confidence,
        source_ref=source_ref,
        requires_human=requires_human,
        note=note,
    )
    return evaluate(session, part, template)


def record_decoded_part(
    session: Session,
    part: Part,
    decoded: DecodedPart,
    *,
    confidence: float = MPN_DECODER_CONFIDENCE,
    confidence_by_field: Mapping[str, float] | None = None,
    source_ref: str | None = None,
) -> tuple[ParameterValueCandidate, ...]:
    """Fan a `DecodedPart` out into candidates. Does **not** evaluate.

    `confidence_by_field` overrides `confidence` per `parameter_template.name`.
    Its one caller is the MPN-decoder cross-check, which raises the confidence of
    the fields an independent reading of the datasheet corroborated: the column
    records confidence *in the value*, and two sources that fail for unrelated
    reasons agreeing is evidence about the value even though it says nothing new
    about either source's reliability.

    `source_ref` defaults to the decoding family, so re-decoding the same part
    number updates the same rows and a *different* family claiming the number
    later (a new, more specific prefix) produces a second, comparable
    observation rather than overwriting the first.

    **A marking decode is flagged `requires_human` and can never auto-promote.**
    `decoded.is_marking` means the input was three digits off the top of a
    component, and `104` is 100 kΩ on a resistor and 100 nF on a capacitor: the
    marking cannot tell you which one you are holding. This is the same rule as
    "never auto-accept an OCR'd or model-read part number" — a confident wrong
    identity is worse than none — applied to the one decoder that reads
    markings.

    `decoded.unknown` is carried into the note so a partial decode is legible in
    the queue: the point is to show *what* was not understood, not merely that
    something was not. `decoded.extras` is dropped, as documented on
    `DecodedPart`: those facts have no `parameter_template` to land in and
    writing them anywhere else would invent a second, unfiltered spec store.

    A template the decoder names but this install has not seeded is **skipped**.
    Templates are user-curated content and a decoder must not create one; the
    decode is a pure function of the part number, so re-running it after the
    template exists loses nothing.
    """
    note = f"undecoded fields: {', '.join(decoded.unknown)}" if decoded.unknown else None
    if decoded.is_marking:
        marking_note = (
            "decoded as a printed marking, not a part number: the same digits mean "
            "different quantities on different components"
        )
        note = "; ".join(filter(None, (marking_note, note)))

    recorded: list[ParameterValueCandidate] = []
    for name, raw_value in decoded.parameters.items():
        template = session.execute(
            select(ParameterTemplate).where(ParameterTemplate.name == name)
        ).scalar_one_or_none()
        if template is None:
            continue
        recorded.append(
            record(
                session,
                part,
                template,
                raw_value,
                source=Provenance.MPN_DECODER,
                confidence=(confidence_by_field or {}).get(name, confidence),
                source_ref=decoded.family if source_ref is None else source_ref,
                requires_human=decoded.is_marking,
                note=note,
            )
        )
    return tuple(recorded)


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def evaluate(session: Session, part: Part, template: ParameterTemplate) -> Decision:
    """Decide what, if anything, this field's pending candidates justify writing.

    ## The late higher-priority source

    A `llm_inferred` value was promoted last week; tonight's datasheet
    extraction disagrees with it. `datasheet_table` outranks `llm_inferred`, so
    the priority order says the table is right — and this function still
    **queues it for review rather than overwriting**. Three reasons, and the
    first is the decisive one:

    * **The two errors are not symmetric.** A review item costs a human ten
      seconds. A silent overwrite is undetectable: nobody is told, the previous
      value is gone, and if the extraction picked the wrong row of a variant
      table — the documented failure mode of table extraction — the catalogue
      now holds a wrong value that no human has ever seen, in the data nobody
      eyeballs. Everything in this design leans the direction that fails
      loudly.
    * **The priority order answers a different question.** It says which source
      to *believe* when a value must be chosen now, and
      `parameters._existing_or_new` already enforces it for direct writes. It
      does not license a background job to rewrite a value someone may already
      have acted on — ordered stock against, or designed a board around.
    * **Disagreement between the two most trusted sources is the strongest
      signal in the system that a human should look.** Resolving it
      automatically discards precisely the evidence that made it worth looking
      at. `PLAN.md` says review below 0.8 *or on disagreement*, and this is a
      disagreement.

    Nothing is lost by queueing: the higher-priority candidate keeps its row
    with `review_reason = FIELD_OCCUPIED`, ordered above the incumbent's source
    in the queue, and `promote()` applies it through the same
    `app.services.parameters` door. It is one click, taken by someone who saw
    both numbers.

    A late higher-priority source that **agrees** is applied, because it changes
    only the provenance and confidence recorded on the row and never the value —
    the catalogue's numbers do not move, its trust in them rises.

    **A `manual` value is never overwritten automatically, whatever else is
    true.** Nothing outranks `MANUAL` in `PROVENANCE_PRIORITY`, so that already
    follows from the ordering; it is asserted separately below anyway, because
    it is the one guarantee that must not be lost to a future reshuffle of the
    numbers in that table.
    """
    pending_rows = _pending_for_field(session, part, template)
    if not pending_rows:
        return Decision(outcome=PromotionOutcome.NOTHING_PENDING)

    existing = session.execute(
        select(ParameterValue).where(
            ParameterValue.part_id == part.id,
            ParameterValue.template_id == template.id,
        )
    ).scalar_one_or_none()

    # Rule 1. Unparseable rows and rows flagged as needing a human are not
    # eligible, and stay in the queue carrying the reason they were flagged with
    # at record time.
    eligible = [row for row in pending_rows if is_promotable(row) and not row.requires_human]
    blocked = [row for row in pending_rows if row not in eligible]

    if not eligible:
        return Decision(
            outcome=PromotionOutcome.QUEUED,
            reason=_first_reason(blocked),
            queued=tuple(blocked),
        )

    best = eligible[0]
    agreeing = [row for row in eligible if _agree(_of_candidate(row), _of_candidate(best))]
    dissenting = [row for row in eligible if row not in agreeing]

    if existing is not None:
        # Agreement with `best` stops mattering once a value exists: the axis
        # that decides is agreement with the *incumbent*, so that partition is
        # redone there.
        return _decide_occupied(session, part, template, existing, eligible, blocked)

    if dissenting:
        # Rule 4. Two sources, two different numbers, an empty field: refuse to
        # pick. The queue orders them by trust so the obvious click is the right
        # one, but a human makes it.
        for row in eligible:
            _keep_pending(row, CandidateReviewReason.SOURCES_DISAGREE)
        session.flush()
        return Decision(
            outcome=PromotionOutcome.QUEUED,
            reason=CandidateReviewReason.SOURCES_DISAGREE,
            queued=tuple(eligible + blocked),
        )

    # Rule 3. One source into an empty field: the 0.8 bar applies.
    #
    # Counted over **distinct sources**, not over rows. Uniqueness here is
    # `(part, template, source, source_ref)` precisely so that one source can
    # hold several observations of one field — two revisions of a datasheet, two
    # rows of a variant table, a family PDF plus a package PDF — and the nightly
    # re-extraction produces exactly that. Counting rows would let a single
    # `llm_inferred` reading at confidence 0.1 corroborate *itself* the moment it
    # appeared under a second `source_ref`, skipping the 0.8 bar entirely. Rule 2
    # is stated over *independent* sources, and two documents read by the same
    # model are the correlated pair it rules out.
    if len({row.source for row in agreeing}) == 1 and best.confidence < AUTO_PROMOTE_CONFIDENCE:
        # Every one of them, not just `best`: a row left `PENDING` without a
        # reason is unexplainable in the queue and matches no filter.
        for row in agreeing:
            _keep_pending(row, CandidateReviewReason.LOW_CONFIDENCE)
        session.flush()
        return Decision(
            outcome=PromotionOutcome.QUEUED,
            reason=CandidateReviewReason.LOW_CONFIDENCE,
            queued=(*agreeing, *blocked),
        )

    # Rule 2 (two or more independent sources agreeing) or rule 3 satisfied (one
    # source, confident enough on its own).
    value = _write_through_parameters(session, part, template, best)
    _finish(best, CandidateStatus.PROMOTED, None)
    for row in agreeing[1:]:
        _finish(row, CandidateStatus.SUPERSEDED, None)
    session.flush()
    return Decision(
        outcome=PromotionOutcome.QUEUED if blocked else PromotionOutcome.PROMOTED,
        promoted=best,
        value=value,
        reason=_first_reason(blocked) if blocked else None,
        queued=tuple(blocked),
    )


def _decide_occupied(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    existing: ParameterValue,
    eligible: list[ParameterValueCandidate],
    blocked: list[ParameterValueCandidate],
) -> Decision:
    """The field already holds a value. Nothing that disagrees may overwrite it."""
    incumbent = _of_value(existing)
    confirming = [row for row in eligible if _agree(_of_candidate(row), incumbent)]
    conflicting = [row for row in eligible if row not in confirming]

    for row in confirming:
        _finish(row, CandidateStatus.SUPERSEDED, None)
    for row in conflicting:
        _keep_pending(row, CandidateReviewReason.FIELD_OCCUPIED)

    promoted: ParameterValueCandidate | None = None
    value: ParameterValue | None = None
    if confirming:
        upgrade = confirming[0]
        # A confirming source may raise the recorded provenance and confidence,
        # never the value. `MANUAL` is excluded explicitly rather than left to
        # the fact that nothing outranks it.
        manual = existing.provenance == Provenance.MANUAL
        if not manual and _outranks(upgrade.source, existing.provenance):
            value = _write_through_parameters(session, part, template, upgrade)
            _finish(upgrade, CandidateStatus.PROMOTED, None)
            promoted = upgrade

    session.flush()
    queued = tuple(conflicting + blocked)
    if queued:
        return Decision(
            outcome=PromotionOutcome.QUEUED,
            promoted=promoted,
            value=value,
            reason=CandidateReviewReason.FIELD_OCCUPIED if conflicting else _first_reason(blocked),
            queued=queued,
        )
    return Decision(
        outcome=PromotionOutcome.PROMOTED if promoted else PromotionOutcome.ALREADY_SATISFIED,
        promoted=promoted,
        value=value,
    )


def promote(
    session: Session, candidate: ParameterValueCandidate, *, force: bool = False
) -> ParameterValue:
    """Apply a candidate a human chose, through the ordinary write path.

    `force` overrides `app.services.parameters`' own precedence refusal, and is
    what "the reviewer looked at both numbers and picked the lower-priority one"
    needs. It is only ever reachable from a human decision — no rule in
    `evaluate()` sets it — which is the distinction the whole module rests on:
    a human may overrule the priority order, a background job may not.
    """
    part = session.get(Part, candidate.part_id)
    template = session.get(ParameterTemplate, candidate.template_id)
    if part is None or template is None:  # pragma: no cover - FK-guaranteed
        raise ValueError(f"candidate {candidate.id} points at a missing part or template")

    value = _write_through_parameters(session, part, template, candidate, force=force)
    _finish(candidate, CandidateStatus.PROMOTED, None)

    # Every other pending candidate for the field is now answered: the human
    # chose. Agreeing ones are superseded; the rest stay visible as a conflict
    # with a value that now exists.
    for row in _pending_for_field(session, part, template):
        if row.id == candidate.id:
            continue
        if _agree(_of_candidate(row), _of_candidate(candidate)):
            _finish(row, CandidateStatus.SUPERSEDED, None)
        else:
            _keep_pending(row, CandidateReviewReason.FIELD_OCCUPIED)

    session.flush()
    return value


def dismiss(session: Session, candidate: ParameterValueCandidate) -> ParameterValueCandidate:
    """A human said no. Sticks across re-runs of the same extraction."""
    _finish(candidate, CandidateStatus.DISMISSED, candidate.review_reason)
    session.flush()
    return candidate


def pending(
    session: Session, *, part: Part | None = None, limit: int | None = None
) -> list[ParameterValueCandidate]:
    """The review queue: every pending candidate, most trusted first.

    Ordering is by source priority then age, so within one field the top row is
    the one the priority order would have chosen — the obvious click is the
    right click, and a reviewer working top-down is agreeing with
    `manual > datasheet_table > mpn_decoder > distributor_freetext >
    llm_inferred` rather than fighting it.
    """
    stmt = select(ParameterValueCandidate).where(
        ParameterValueCandidate.status == CandidateStatus.PENDING
    )
    if part is not None:
        stmt = stmt.where(ParameterValueCandidate.part_id == part.id)
    rows = sorted(session.execute(stmt).scalars(), key=_queue_key)
    return rows[:limit] if limit is not None else rows


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Normalised:
    """The comparable part of a value, from a candidate or a stored value."""

    nominal: float | None
    low: float | None
    high: float | None
    choice_id: int | None


def compare_raw(
    session: Session, template: ParameterTemplate, left: str, right: str
) -> bool | None:
    """Whether two raw source strings assert the same value. `None` if either fails to parse.

    The **only** public entry point to the agreement rule, and the reason the MPN
    cross-check cannot drift from what `evaluate()` will conclude about the same
    two rows a moment later. A second implementation of "these are the same
    value" is how a cross-check comes to report agreement while the promotion
    rules queue a disagreement — two subsystems each behaving sensibly and the
    pair of them incoherent.

    `None` rather than `False` for an unparseable side, because "I could not
    compare these" and "these differ" want opposite handling: the first is
    `CrossCheckVerdict.UNCHECKED` and leaves the model's confidence alone, the
    second is a disagreement that blocks promotion.
    """
    value_type = parameters.value_type_of(template)
    if value_type not in (ValueType.NUMERIC, ValueType.ENUM):
        raise UnsupportedTemplateType(
            f"template {template.name!r} is {value_type}; comparison supports numeric and enum only"
        )
    left_nominal, left_low, left_high, left_choice, left_note = _normalise(
        session, template, left, value_type
    )
    right_nominal, right_low, right_high, right_choice, right_note = _normalise(
        session, template, right, value_type
    )
    if left_note is not None or right_note is not None:
        return None
    return _agree(
        _Normalised(left_nominal, left_low, left_high, left_choice),
        _Normalised(right_nominal, right_low, right_high, right_choice),
    )


def _of_candidate(row: ParameterValueCandidate) -> _Normalised:
    return _Normalised(row.value_nominal, row.value_min, row.value_max, row.choice_id)


def _of_value(row: ParameterValue) -> _Normalised:
    return _Normalised(row.value_nominal, row.value_min, row.value_max, row.choice_id)


def _agree(left: _Normalised, right: _Normalised) -> bool:
    """Whether two values are the same value, within `AGREEMENT_REL_EPS`.

    Enum facets compare by `choice_id`, which makes agreement exact: alias
    resolution has already collapsed `0603` and `1608` onto one row, so two
    sources using different conventions agree without any fuzziness being
    involved.

    Numerics compare on the **nominal** when both have one, so `100 nF` agrees
    with `100 nF ±10%` — they assert the same value and differ only in how much
    the datasheet promises about it, and the more trusted source's tolerance is
    the one that gets stored. When either side lacks a nominal it is a range
    (`20-30uF`) or a one-sided limit (`>=50V`), and then **both endpoints** must
    match, a missing endpoint matching only another missing endpoint. A range and
    a scalar therefore never agree: "somewhere between 20 and 30 µF" and "22 µF"
    are different assertions, and quietly collapsing the first into the second
    would invent precision the source never claimed. `>=50V` does not agree with
    `50V` either, for the same reason in the other direction.

    **This relation is reflexive on every value that has one.** It has to be: a
    lone candidate is partitioned against itself in `evaluate()`, so a value
    that failed to agree with itself would be counted as its own dissenter and
    the one source in the field would be reported to the reviewer as two sources
    conflicting. It is *not* reflexive on a value that has nothing to compare —
    an unparseable row, all four fields NULL — because "I could not compare
    these" must never read as "these are the same".
    """
    if left.choice_id is not None or right.choice_id is not None:
        return left.choice_id is not None and left.choice_id == right.choice_id

    if left.nominal is not None and right.nominal is not None:
        return _close(left.nominal, right.nominal)

    if left.low is None and left.high is None:
        return False
    if right.low is None and right.high is None:
        return False
    return _endpoints_match(left.low, right.low) and _endpoints_match(left.high, right.high)


def _asserts_a_different_value(
    incoming: _Normalised, incoming_raw: str, stored: _Normalised, stored_raw: str
) -> bool:
    """Whether a re-observation changed what the source is claiming.

    The reopen test for a decided row, and the reason a dismissal sticks. Kept
    separate from `_agree` because it has to answer for the unparseable case,
    where `_agree` is deliberately `False` even against an identical value: an
    unparseable row reopening on every nightly re-run of the same extraction
    would resurrect a dismissal forever, so there the raw text is compared
    verbatim instead.
    """
    incoming_has_value = _has_value(incoming)
    if incoming_has_value != _has_value(stored):
        return True
    if not incoming_has_value:
        return incoming_raw != stored_raw
    return not _agree(incoming, stored)


def _has_value(value: _Normalised) -> bool:
    return (
        value.choice_id is not None
        or value.nominal is not None
        or value.low is not None
        or value.high is not None
    )


def _endpoints_match(left: float | None, right: float | None) -> bool:
    """One interval endpoint against another. A NULL matches only a NULL.

    An absent endpoint is a real, comparable fact — "unbounded above" — so
    `>=50V` matches `>=50V` and not `50V`. Treating NULL as a wildcard would let
    a one-sided limit agree with anything on that side, which is exactly how a
    bound would come to confirm a value.
    """
    if (left is None) != (right is None):
        return False
    if left is None or right is None:
        return True
    return _close(left, right)


def _close(left: float, right: float) -> bool:
    if left == right:
        return True
    scale = max(abs(left), abs(right))
    if scale == 0.0:  # pragma: no cover - only reachable if one side is -0.0
        return True
    return abs(left - right) / scale <= AGREEMENT_REL_EPS


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalise(
    session: Session,
    template: ParameterTemplate,
    raw_value: str,
    value_type: ValueType,
) -> tuple[float | None, float | None, float | None, int | None, str | None]:
    """Parse `raw_value` for comparison. Returns a note instead of raising.

    A source offering something the grammar rejects is a review-queue item, not
    an exception: the raw text is evidence — of a grammar gap, a unit misread or
    a bad extraction — and throwing it away is the one thing that makes the
    problem unfixable.
    """
    if value_type is ValueType.ENUM:
        try:
            choice = parameters.resolve_choice(session, template, raw_value)
        except parameters.ChoiceNotFound as error:
            return None, None, None, None, str(error)
        return None, None, None, choice.id, None

    try:
        parsed = parse_for_template(raw_value, template)
    except ValueParseError as error:
        return None, None, None, None, f"{error.reason}: {error}"
    low, high = parsed.to_interval()
    return parsed.value_nominal, low, high, None, None


def _write_through_parameters(
    session: Session,
    part: Part,
    template: ParameterTemplate,
    candidate: ParameterValueCandidate,
    *,
    force: bool = False,
) -> ParameterValue:
    """The only door onto `parameter_value` from this module.

    `set_numeric` is what guarantees `value_min`/`value_max` are populated —
    equal for a scalar, the tolerance band for `100 nF ±10%` — and a numeric row
    with null bounds is invisible to every range query in the system without any
    error being raised. Re-parsing `raw_value` here rather than copying the
    columns this table already holds is what keeps that guarantee in one place.
    """
    provenance = Provenance(candidate.source)
    if force:
        # The reviewer's decision beats the ordering. Clearing the incumbent's
        # provenance is the narrowest way to say so without teaching
        # `parameters` a bypass that a background job could also reach.
        existing = session.execute(
            select(ParameterValue).where(
                ParameterValue.part_id == part.id,
                ParameterValue.template_id == template.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.provenance = provenance
            session.flush()

    if parameters.value_type_of(template) is ValueType.ENUM:
        return parameters.set_choice(
            session,
            part,
            template,
            candidate.raw_value,
            provenance=provenance,
            confidence=candidate.confidence,
        )
    return parameters.set_numeric(
        session,
        part,
        template,
        candidate.raw_value,
        provenance=provenance,
        confidence=candidate.confidence,
    )


def _pending_for_field(
    session: Session, part: Part, template: ParameterTemplate
) -> list[ParameterValueCandidate]:
    rows = session.execute(
        select(ParameterValueCandidate).where(
            ParameterValueCandidate.part_id == part.id,
            ParameterValueCandidate.template_id == template.id,
            ParameterValueCandidate.status == CandidateStatus.PENDING,
        )
    ).scalars()
    return sorted(rows, key=_queue_key)


def _queue_key(row: ParameterValueCandidate) -> tuple[int, int, int, float, int]:
    """Most trusted first, then most confident, then oldest. Total and stable.

    `id` last so the order never depends on how the rows came back from SQLite —
    `evaluate()` picks `[0]` as the value it would promote, and that choice must
    not vary between two runs over identical data.
    """
    return (
        row.part_id,
        row.template_id,
        -PROVENANCE_PRIORITY.get(row.source, 0),
        -row.confidence,
        row.id,
    )


def is_promotable(row: ParameterValueCandidate) -> bool:
    """Whether this row carries a value `parameters` would actually store.

    Public because the review API must gate accept/bulk-accept on the same
    predicate `evaluate()` uses. Two predicates would drift, and the drift would
    show up as a 500 from a button the screen offered.

    An enum row needs a resolved `choice_id`. A numeric row needs **both**
    bounds: `parameters.set_numeric` refuses a one-sided interval, so a row
    holding only one bound cannot be promoted by anyone, and reporting it as
    usable would mean `evaluate()` selecting it and then raising out of a
    background job. Testing only `value_min` — the earlier form of this check —
    made `>=50V` "usable" and `<=100nF` not, for no reason other than which
    column the comparison happened to fill.
    """
    if row.choice_id is not None:
        return True
    return row.value_min is not None and row.value_max is not None


def is_one_sided(row: ParameterValueCandidate) -> bool:
    """A parsed numeric holding exactly one bound: `>=50V`, `<100nF`.

    The distinction the review API needs from `is_promotable`'s `False`: this row
    parsed cleanly and is a *bound*, where an unparseable row is text nothing
    understood. Same refusal, but a different instruction to the reviewer — type
    a two-sided value, versus report a grammar gap — so it is named here rather
    than re-derived from the columns at the route.
    """
    if row.choice_id is not None:
        return False
    return (row.value_min is None) != (row.value_max is None)


def _outranks(source: str, established: str) -> bool:
    return PROVENANCE_PRIORITY.get(source, 0) > PROVENANCE_PRIORITY.get(established, 0)


def _keep_pending(row: ParameterValueCandidate, reason: CandidateReviewReason) -> None:
    row.status = CandidateStatus.PENDING
    row.decided_at = None
    # A reason already set at record time (unparseable, requires-human) is the
    # more specific fact about the row and is not overwritten by the evaluation's
    # more general one.
    if row.review_reason is None:
        row.review_reason = reason


def _finish(
    row: ParameterValueCandidate,
    status: CandidateStatus,
    reason: CandidateReviewReason | str | None,
) -> None:
    row.status = status
    row.review_reason = reason
    row.decided_at = utcnow()


def _first_reason(rows: list[ParameterValueCandidate]) -> CandidateReviewReason | None:
    for row in rows:
        if row.review_reason is not None:
            return CandidateReviewReason(row.review_reason)
    return None
