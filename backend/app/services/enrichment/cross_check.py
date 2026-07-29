"""The MPN-decoder cross-check: the step between a model's reading and the queue.

`docs/PLAN.md`: `... -> LLM structured extraction against a JSON schema ->
**MPN-decoder cross-check** -> confidence score -> review queue below 0.8 or on
disagreement`. This module is that arrow. It takes what a model claims a datasheet
says and what arithmetic on the manufacturer's own numbering scheme says, and
turns the pair into candidate rows the existing promotion rules can act on.

## Why the decoder is the trustworthy side

The decoder is a lookup in a published numbering table: `GRM188R71H104KA93D`
contains `104`, `104` is 100 nF, and that is not an opinion. The model is reading
prose and multi-column tables, and its documented failure mode is picking the
**wrong row of a variant table** — a value that is real, correct for some part,
and wrong for this one. Nothing about a confidence score distinguishes that from
a right answer, which is why a second, differently-shaped source is worth having
at all.

So `mpn_decoder > llm_inferred` in `PROVENANCE_PRIORITY` already decides who wins,
and this module's job is to make sure the code *uses* that rather than
splitting the difference:

* **A conflict is never averaged.** There is no numeric blend of "100 nF" and
  "4.7 µF"; the mean of two values when one is wrong is a third value that no
  source ever asserted and that no reviewer can trace to anything.
* **A conflict is never resolved by whichever confidence is higher.** The model
  reports its own confidence; letting that outrank the decoder would let a
  hallucination win by being self-assured, which is the precise failure the
  priority order exists to prevent.
* **A conflict does not auto-write the decoder's value either.** The winner is
  *reported* (`FieldCrossCheck.winner`) and the decoder's row sorts to the top of
  the queue, so the obvious click is the right one — but the value lands when a
  human clicks it. `candidates.evaluate()` already refuses to pick between two
  disagreeing sources for an empty field, and this module must not become a
  second, laxer path onto `parameter_value` for exactly the data nobody eyeballs.
  `docs/PLAN.md` says review below 0.8 *or on disagreement*, without an exception
  for "unless we are fairly sure".

What the conflict *does* change is the model's recorded confidence: it is clamped
to `CONFLICT_CONFIDENCE_CEILING`, below the auto-promote bar. That is load-bearing
rather than cosmetic. Without it, a reviewer dismissing the decoder's row — a
perfectly reasonable thing to do when the decoder's transcription is what is wrong
— would leave a single high-confidence model reading behind, which the next
`evaluate()` would silently promote. The clamp means a value the manufacturer's
own numbering contradicted can only ever be accepted by someone who looked at it.

## Agreement raises confidence, and why the arithmetic is a noisy-OR

Two sources agreeing is combined as `1 - (1-a)(1-b)`: the chance both are wrong
*and* landed on the same value is the product of their independent error rates.
It is monotone, never below either input, and bounded by
`CONFIRMATION_CEILING < 1.0` because nothing in this system is certain.

The independence assumption is doing real work here, so it is worth stating why it
holds for this pair and not in general: the decoder's error mode is a transcription
mistake in *this repository's* copy of a numbering table, the model's is misreading
a document. Neither can cause the other. That is emphatically **not** true of a
distributor's free text and a datasheet extraction — distributor copy is frequently
lifted from the same datasheet — which is why `candidates.evaluate()` has no
majority vote and why this combination rule is used only here.

## The part number itself is never accepted

A variant is attached to a part only when its number matches a catalogue row's
`mpn_norm` exactly, by index lookup. A number matching nothing is refused
outright — `IdentityRefusal.NO_MATCH`, nothing written — and handed back with the
text it was read from, because "the model found a real sibling variant in the
table" and "the model invented a plausible part number" are indistinguishable from
here and a wrong-but-confident identity is worse than none. The decode is run on
the **catalogue's** part number, never on the model's string, so a hallucinated
character cannot even reach the decoder.

Unclaimed identities are returned, not stored: this chunk adds no table and no
route. A refusal needs no storage to be correct, and the two candidate homes for
one are both wrong today — `parameter_value_candidate` needs a `part_id` (there
is no part), and `pending_intakes` is a *scan* worklist whose ordering and UI say
"the ones I just scanned", so injecting a background job's findings would show the
user scans they never made. A durable `part identity candidate` queue is a
follow-up with its own migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import CrossCheckVerdict, IdentityRefusal, Provenance
from app.models.parameter import ParameterTemplate
from app.services.enrichment import candidates
from app.services.enrichment.extract import (
    ExtractedField,
    ExtractedVariant,
    ExtractionResult,
)
from app.services.enrichment.mpn_decoders import DecodedPart, decode
from app.services.scanning.codes import normalize_mpn

#: A corroborated value is very likely right, and still not certain: the two
#: sources could share a cause nobody has thought of, and the *field* could be
#: right about the wrong part. Nothing in this system is allowed to report 1.0.
CONFIRMATION_CEILING = 0.99

#: The confidence a model reading is recorded with once the MPN decoder has
#: contradicted it.
#:
#: The only load-bearing property is `< candidates.AUTO_PROMOTE_CONFIDENCE`, and
#: a test asserts exactly that relation so neither constant can be edited into
#: agreement with the other by accident. 0.5 is then simply "no better than a
#: coin flip against the part number's own arithmetic" — the number itself never
#: decides anything.
CONFLICT_CONFIDENCE_CEILING = 0.5


def combine(left: float, right: float) -> float:
    """Two independent sources agreeing. Never below either input, never 1.0."""
    return min(CONFIRMATION_CEILING, 1.0 - (1.0 - left) * (1.0 - right))


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldCrossCheck:
    """One extracted field, and what the decoder said about it."""

    template_name: str
    verdict: CrossCheckVerdict
    extracted: ExtractedField
    #: The decoder's raw value for the same field, when it had one.
    decoded_raw: str | None
    #: What the extracted candidate is recorded with: raised on agreement,
    #: clamped on conflict, untouched when there was nothing to check against.
    confidence: float

    @property
    def winner(self) -> Provenance:
        """Which source the priority order believes for this field.

        A pure function of `PROVENANCE_PRIORITY` and of which sources spoke —
        **never of the confidences**. Whether that source's value is *written*
        is `candidates.evaluate()`'s decision, not this one's; a conflict is
        reported with a winner and still queued.
        """
        return Provenance.MPN_DECODER if self.decoded_raw is not None else Provenance.LLM_INFERRED

    @property
    def winning_value(self) -> str:
        return self.decoded_raw if self.decoded_raw is not None else self.extracted.raw_value


@dataclass(frozen=True)
class VariantCrossCheck:
    """One part number's fields, cross-checked."""

    mpn: str
    #: The decoder family that claimed the number, or `None` when no family did —
    #: in which case every field is `UNCHECKED` and the model is unverified.
    decoder_family: str | None
    fields: tuple[FieldCrossCheck, ...]

    def field(self, template_name: str) -> FieldCrossCheck | None:
        for row in self.fields:
            if row.template_name == template_name:
                return row
        return None

    @property
    def disagreements(self) -> tuple[FieldCrossCheck, ...]:
        return tuple(row for row in self.fields if row.verdict is CrossCheckVerdict.CONFLICT)

    @property
    def confirmations(self) -> tuple[FieldCrossCheck, ...]:
        return tuple(row for row in self.fields if row.verdict is CrossCheckVerdict.CONFIRMED)

    @property
    def needs_review(self) -> bool:
        """Any disagreement, or anything unchecked and under the bar.

        The two clauses of `docs/PLAN.md`'s rule, in order. Note that a
        *confirmed* field never lands here however low the model's own
        confidence was: corroboration by an independent source is stronger
        evidence than a self-report, which is the same reasoning that exempts
        agreement from the 0.8 threshold in `candidates.evaluate()`.
        """
        return bool(self.disagreements) or any(
            row.confidence < candidates.AUTO_PROMOTE_CONFIDENCE
            for row in self.fields
            if row.verdict is CrossCheckVerdict.UNCHECKED
        )


def cross_check_variant(
    session: Session,
    variant: ExtractedVariant,
    decoded: DecodedPart | None,
    templates: Mapping[str, ParameterTemplate],
) -> VariantCrossCheck:
    """Compare one variant's extracted fields against the decoded part number.

    Agreement uses `candidates.compare_raw`, which is the same rule
    `evaluate()` will apply to the two rows a moment later — deliberately, since
    a cross-check that reported agreement while the promotion rules queued a
    disagreement would be two subsystems each behaving sensibly and the pair of
    them incoherent.
    """
    checks: list[FieldCrossCheck] = []
    for extracted in variant.fields:
        template = templates.get(extracted.template_name)
        decoded_raw = decoded.parameters.get(extracted.template_name) if decoded else None

        verdict = CrossCheckVerdict.UNCHECKED
        confidence = extracted.confidence
        if template is not None and decoded_raw is not None:
            same = candidates.compare_raw(session, template, extracted.raw_value, decoded_raw)
            if same is True:
                verdict = CrossCheckVerdict.CONFIRMED
                confidence = combine(extracted.confidence, candidates.MPN_DECODER_CONFIDENCE)
            elif same is False:
                verdict = CrossCheckVerdict.CONFLICT
                confidence = min(extracted.confidence, CONFLICT_CONFIDENCE_CEILING)

        checks.append(
            FieldCrossCheck(
                template_name=extracted.template_name,
                verdict=verdict,
                extracted=extracted,
                decoded_raw=decoded_raw,
                confidence=confidence,
            )
        )
    return VariantCrossCheck(
        mpn=variant.mpn,
        decoder_family=decoded.family if decoded else None,
        fields=tuple(checks),
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnclaimedIdentity:
    """A part number the model produced that no catalogue row claims.

    **Nothing was written for it**, including the values, which are carried here
    only so a human deciding "yes, add that variant" does not have to re-run the
    extraction to get them back.
    """

    mpn: str
    reason: IdentityRefusal
    #: Verbatim text the number was read from — the evidence a reviewer judges.
    source_text: str
    document_ref: str
    fields: tuple[ExtractedField, ...]


@dataclass(frozen=True)
class FieldDecision:
    """What the promotion rules did with one field of one part."""

    mpn: str
    template_name: str
    decision: candidates.Decision


@dataclass(frozen=True)
class IngestReport:
    document_ref: str
    provider: str
    model: str
    checks: tuple[VariantCrossCheck, ...]
    decisions: tuple[FieldDecision, ...]
    unclaimed: tuple[UnclaimedIdentity, ...]
    #: Field names the response carried that this install has no
    #: `parameter_template` for. Skipped, never created: templates are curated
    #: content and a model must not add one.
    unknown_templates: tuple[str, ...]

    def check_for(self, mpn: str) -> VariantCrossCheck | None:
        key = normalize_mpn(mpn)
        for row in self.checks:
            if normalize_mpn(row.mpn) == key:
                return row
        return None

    def decision_for(self, mpn: str, template_name: str) -> candidates.Decision | None:
        key = normalize_mpn(mpn)
        for row in self.decisions:
            if normalize_mpn(row.mpn) == key and row.template_name == template_name:
                return row.decision
        return None

    @property
    def needs_review(self) -> bool:
        return (
            any(check.needs_review for check in self.checks)
            or bool(self.unclaimed)
            or any(row.decision.needs_review for row in self.decisions)
        )


def ingest(session: Session, result: ExtractionResult) -> IngestReport:
    """Record an extraction's variants as candidates, cross-checked, then evaluate.

    Order matters and is not an implementation detail: **both** sources' rows are
    recorded before anything is evaluated. `evaluate()` decides from the rows
    present for a field, so evaluating after the decoder but before the model
    would promote the decoder's value into an empty field as a lone confident
    source, and the model's contradicting reading would then arrive to find the
    field occupied — a `FIELD_OCCUPIED` review item instead of the
    `SOURCES_DISAGREE` one it actually is, with a value already written that
    nobody chose. This is also why `record_decoded_part` deliberately does not
    evaluate.
    """
    templates = {
        row.name: row for row in session.execute(select(ParameterTemplate)).scalars().all()
    }
    names_by_id = {template.id: name for name, template in templates.items()}
    keys = [normalize_mpn(variant.mpn) for variant in result.variants]
    by_norm: dict[str, list[Part]] = {}
    if keys:
        for part in session.execute(select(Part).where(Part.mpn_norm.in_(keys))).scalars():
            if part.mpn_norm:
                by_norm.setdefault(part.mpn_norm, []).append(part)

    checks: list[VariantCrossCheck] = []
    decisions: list[FieldDecision] = []
    unclaimed: list[UnclaimedIdentity] = []
    unknown: set[str] = set()

    for variant in result.variants:
        matched = by_norm.get(normalize_mpn(variant.mpn), [])
        if len(matched) != 1:
            unclaimed.append(
                UnclaimedIdentity(
                    mpn=variant.mpn,
                    reason=(IdentityRefusal.NO_MATCH if not matched else IdentityRefusal.AMBIGUOUS),
                    source_text=variant.mpn_source_text,
                    document_ref=result.document_ref,
                    fields=variant.fields,
                )
            )
            continue
        part = matched[0]

        # The catalogue's part number, never the model's string: a hallucinated
        # character must not reach the decoder and come back as a decoded fact.
        decoded = decode(part.mpn) if part.mpn else None
        check = cross_check_variant(session, variant, decoded, templates)
        checks.append(check)

        touched: set[str] = set()
        if decoded is not None:
            confirmed = {
                row.template_name: row.confidence
                for row in check.fields
                if row.verdict is CrossCheckVerdict.CONFIRMED
            }
            for row in candidates.record_decoded_part(
                session, part, decoded, confidence_by_field=confirmed
            ):
                touched.add(names_by_id[row.template_id])

        for field_check in check.fields:
            template = templates.get(field_check.template_name)
            if template is None:
                unknown.add(field_check.template_name)
                continue
            candidates.record(
                session,
                part,
                template,
                field_check.extracted.raw_value,
                source=Provenance.LLM_INFERRED,
                confidence=field_check.confidence,
                source_ref=result.document_ref,
                note=review_note(field_check, result, check.decoder_family),
            )
            touched.add(field_check.template_name)

        for name in sorted(touched):
            decisions.append(
                FieldDecision(
                    mpn=variant.mpn,
                    template_name=name,
                    decision=candidates.evaluate(session, part, templates[name]),
                )
            )

    return IngestReport(
        document_ref=result.document_ref,
        provider=result.provider,
        model=result.model,
        checks=tuple(checks),
        decisions=tuple(decisions),
        unclaimed=tuple(unclaimed),
        unknown_templates=tuple(sorted(unknown)),
    )


def review_note(
    check: FieldCrossCheck, result: ExtractionResult, decoder_family: str | None
) -> str:
    """What the review item says, in the queue, without opening a PDF.

    Every branch names the evidence *first*. A reviewer's question is never "what
    did the confidence score say" — it is "what does the datasheet actually say
    here", and the answer is the quoted line. The cross-check outcome follows it
    so the reviewer knows why they are being asked at all.
    """
    page = f", page {check.extracted.page}" if check.extracted.page is not None else ""
    note = (
        f'{result.provider}/{result.model} read "{check.extracted.source_text}"{page}'
        f" (self-reported confidence {check.extracted.confidence:.2f})"
    )
    if check.verdict is CrossCheckVerdict.CONFIRMED:
        return (
            f"{note}; confirmed by the {decoder_family} part-number decoder ({check.decoded_raw})"
        )
    if check.verdict is CrossCheckVerdict.CONFLICT:
        return (
            f"{note}; DISAGREES with the {decoder_family} part-number decoder, which reads "
            f"{check.decoded_raw} from the part number itself. Not auto-accepted: the decoder is "
            f"arithmetic on the manufacturer's own scheme and outranks a reading of the document."
        )
    if check.decoded_raw is None:
        reason = (
            f"the {decoder_family} decoder does not cover this field"
            if decoder_family
            else "no part-number decoder recognised this part number"
        )
        return f"{note}; not cross-checked: {reason}"
    return (
        f"{note}; not cross-checked: the decoder reads {check.decoded_raw} but one of the two "
        f"values could not be parsed for comparison"
    )
