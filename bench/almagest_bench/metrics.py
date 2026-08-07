"""Turning recorded cases into the numbers a decision gets made on.

A separate pass over the JSONL, never computed during the run. A scoring bug then
costs a re-score rather than a re-run, and on a matrix that takes seven hours and
a GPU handover that is the difference between fixing it and living with it.

## Agreement is the pipeline's own comparison, never string equality

`0.10 uF` and `100 nF` are the same value, and `candidates.compare_raw` already
knows it -- it is what the promotion rules use to decide whether two sources
agree. Scoring with anything else would give the benchmark a second opinion about
correctness, free to disagree with the rows the system actually writes. A model
marked wrong here and promoted in production would make the whole exercise
misleading in the most expensive direction.

## Micro and macro are both reported, because they disagree

Micro weights every cell equally, so a template that dominates the corpus carries
the score. Macro weights every template equally, so a template with three cases
counts as much as one with forty. Neither is right on its own: reporting only
micro lets "capacitance" stand in for "reads datasheets well", and reporting only
macro lets a rare field nobody cares about sink a good model. They are printed
side by side and a gap between them is itself the finding.

## Confidence intervals bootstrap over cases, never over cells

The five fields of one part are read from one table in one document and succeed or
fail together. Treating 300 cells as 300 independent observations understates the
error by roughly half and manufactures differences that are not there. Resampling
whole cases keeps the clustering intact. This is the single easiest way to make
this benchmark lie, so it is done in one place and commented at the point of use.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.models.parameter import ParameterTemplate
from app.services.enrichment import candidates as candidate_rules
from sqlalchemy.orm import Session

from almagest_bench.corpus import Case
from almagest_bench.record import (
    CORRECT,
    CORRECT_OMISSION,
    HALLUCINATED,
    MISSING,
    WRONG,
    CaseRecord,
    CellOutcome,
)

#: Resamples for a bootstrap interval. A thousand is plenty for a 95% interval on
#: a few dozen cases and keeps a re-score instant.
BOOTSTRAP_RESAMPLES = 1000
#: Fixed, because a confidence interval that moves when you re-score the same
#: file invites re-scoring until it looks good.
BOOTSTRAP_SEED = 20260807


def score_cell(
    session: Session,
    template: ParameterTemplate,
    *,
    truth: str | None,
    asserted: str | None,
) -> CellOutcome:
    """One field's outcome, using the promotion rules' own idea of agreement.

    `truth is None` means the corpus lists this field as **absent** -- the
    datasheet does not state it. Asserting a value there is a hallucination, and
    declining to is a correct omission the model deserves credit for.
    """
    if truth is None:
        return HALLUCINATED if asserted else CORRECT_OMISSION
    if not asserted:
        return MISSING
    return CORRECT if _agrees(session, template, truth, asserted) else WRONG


def _agrees(session: Session, template: ParameterTemplate, truth: str, asserted: str) -> bool:
    """Delegated to `candidates.compare_raw`, which is the shipped comparison.

    It needs a `Session` and a real template row because agreement depends on the
    field's type, unit and enum aliases -- `0.10 uF` and `100 nF` agree, and only
    the value grammar knows that. Using anything else here would give the
    benchmark a second opinion about correctness, free to disagree with the rows
    the system actually writes.

    **`None` scores as disagreement, deliberately.** It means one side did not
    parse, and a value the pipeline cannot parse cannot become a
    `parameter_value` at all (`is_promotable` requires bounds). Counting it as
    correct would credit a model for an answer the system would discard. If the
    *truth* side is what failed to parse, that is a corpus bug -- and
    `check_truth_parses` is what surfaces it before a run rather than quietly
    marking every model wrong on that field.
    """
    return candidate_rules.compare_raw(session, template, truth, asserted) is True


def check_truth_parses(
    session: Session, templates: dict[str, ParameterTemplate], cases: Sequence[Case]
) -> list[str]:
    """Corpus values the value grammar cannot read, which would sink every model.

    Run before a sweep. A truth cell that does not parse makes `compare_raw`
    return `None` for every model on that field, which scores as universally
    wrong and looks exactly like a hard field rather than like a typo in
    `case.json`.
    """
    broken = []
    for case in cases:
        for name, cell in case.expected.items():
            template = templates.get(name)
            if template is None:
                broken.append(f"{case.case_id}: no parameter_template named {name!r}")
                continue
            if (
                candidate_rules.compare_raw(session, template, cell.raw_value, cell.raw_value)
                is not True
            ):
                broken.append(
                    f"{case.case_id}: {name}={cell.raw_value!r} does not parse; "
                    "every model would score wrong on it"
                )
    return broken


@dataclass(frozen=True)
class FieldScore:
    """Precision, recall and F1 for one template, and the counts behind them."""

    template: str
    correct: int = 0
    wrong: int = 0
    missing: int = 0
    hallucinated: int = 0
    correct_omission: int = 0

    @property
    def asserted(self) -> int:
        return self.correct + self.wrong + self.hallucinated

    @property
    def stated(self) -> int:
        return self.correct + self.wrong + self.missing

    @property
    def precision(self) -> float | None:
        """Of everything it claimed, how much was right.

        `None` rather than 0.0 when it claimed nothing: a model that asserted no
        values has an undefined precision, and reporting 0 would average in as
        though it had been wrong every time.
        """
        return self.correct / self.asserted if self.asserted else None

    @property
    def recall(self) -> float | None:
        return self.correct / self.stated if self.stated else None

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or precision + recall == 0:
            return None
        return 2 * precision * recall / (precision + recall)


@dataclass(frozen=True)
class ModelScore:
    """Everything one model earned over one sweep."""

    model_id: str
    cases: int
    by_template: dict[str, FieldScore] = field(default_factory=dict)

    # identity
    identity_exact: int = 0
    identity_normalised: int = 0
    unclaimed: int = 0
    #: Should be zero. Non-zero says the server ignored the schema -- a deployment
    #: fact, not a model fact, and reported separately for that reason.
    unknown_templates: int = 0

    # what it cost a person
    promoted: int = 0
    queued_for_review: int = 0
    wrongly_promoted: int = 0

    # speed
    latencies_ms: tuple[int, ...] = ()
    wall_ms: tuple[int, ...] = ()
    prompt_tokens: tuple[int, ...] = ()
    completion_tokens: tuple[int, ...] = ()
    swap_seconds: float = 0.0
    errors: int = 0

    @property
    def micro_f1(self) -> float | None:
        total = FieldScore(
            template="*",
            correct=sum(s.correct for s in self.by_template.values()),
            wrong=sum(s.wrong for s in self.by_template.values()),
            missing=sum(s.missing for s in self.by_template.values()),
            hallucinated=sum(s.hallucinated for s in self.by_template.values()),
            correct_omission=sum(s.correct_omission for s in self.by_template.values()),
        )
        return total.f1

    @property
    def macro_f1(self) -> float | None:
        scores = [s.f1 for s in self.by_template.values() if s.f1 is not None]
        return sum(scores) / len(scores) if scores else None

    @property
    def median_wall_seconds(self) -> float | None:
        return _median(self.wall_ms) / 1000 if self.wall_ms else None

    @property
    def p90_latency_ms(self) -> float | None:
        return _percentile(self.latencies_ms, 90) if self.latencies_ms else None

    @property
    def wrong_promotion_rate(self) -> float:
        """The number that outranks accuracy.

        A wrongly promoted value is written to `parameter_value` as fact and
        nobody checks it again. Everything else a model gets wrong sits in the
        review queue with its source line, where a person meets it.
        """
        return self.wrongly_promoted / self.promoted if self.promoted else 0.0

    @property
    def review_burden(self) -> float:
        """Fields per case that a person has to look at. Literally the cost."""
        return self.queued_for_review / self.cases if self.cases else 0.0

    @property
    def amortised_swap_seconds(self) -> float:
        """The handover, spread over the cases it bought. Shown, never hidden.

        A model that wins by three points and costs forty minutes of GPU handover
        has not obviously won.
        """
        return self.swap_seconds / self.cases if self.cases else 0.0


def score_model(
    model_id: str, records: Sequence[CaseRecord], *, swap_seconds: float = 0.0
) -> ModelScore:
    by_template: dict[str, dict[str, int]] = {}
    for record in records:
        for template, outcome in record.cells.items():
            bucket = by_template.setdefault(
                template,
                {CORRECT: 0, WRONG: 0, MISSING: 0, HALLUCINATED: 0, CORRECT_OMISSION: 0},
            )
            if outcome in bucket:
                bucket[outcome] += 1

    return ModelScore(
        model_id=model_id,
        cases=len(records),
        by_template={
            template: FieldScore(
                template=template,
                correct=counts[CORRECT],
                wrong=counts[WRONG],
                missing=counts[MISSING],
                hallucinated=counts[HALLUCINATED],
                correct_omission=counts[CORRECT_OMISSION],
            )
            for template, counts in sorted(by_template.items())
        },
        identity_exact=sum(1 for r in records if r.identity_exact),
        identity_normalised=sum(1 for r in records if r.identity_normalised),
        unclaimed=sum(len(r.unclaimed) for r in records),
        unknown_templates=sum(len(r.unknown_templates) for r in records),
        promoted=sum(len(r.promoted) for r in records),
        queued_for_review=sum(len(r.queued_for_review) for r in records),
        wrongly_promoted=sum(len(r.wrongly_promoted) for r in records),
        latencies_ms=tuple(call.latency_ms for r in records for call in r.calls),
        wall_ms=tuple(r.wall_ms for r in records),
        prompt_tokens=tuple(
            call.prompt_tokens for r in records for call in r.calls if call.prompt_tokens
        ),
        completion_tokens=tuple(
            call.completion_tokens for r in records for call in r.calls if call.completion_tokens
        ),
        swap_seconds=swap_seconds,
        errors=sum(1 for r in records if r.error),
    )


@dataclass(frozen=True)
class Interval:
    low: float
    high: float

    @property
    def half_width(self) -> float:
        return (self.high - self.low) / 2


def bootstrap_f1(
    records: Sequence[CaseRecord], *, resamples: int = BOOTSTRAP_RESAMPLES
) -> Interval | None:
    """A 95% interval on micro F1, resampling **whole cases**.

    This is the one function in the package where getting it subtly wrong would
    be invisible and consequential. Resampling cells instead of cases would treat
    the five fields of one part -- read from one table, right or wrong together --
    as five independent observations, halving the interval and manufacturing
    differences between models that the data does not support.
    """
    if len(records) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    scores: list[float] = []
    for _ in range(resamples):
        sample = [records[rng.randrange(len(records))] for _ in range(len(records))]
        f1 = score_model("bootstrap", sample).micro_f1
        if f1 is not None:
            scores.append(f1)
    if not scores:
        return None
    scores.sort()
    return Interval(
        low=scores[int(0.025 * (len(scores) - 1))],
        high=scores[int(0.975 * (len(scores) - 1))],
    )


def resolvable(
    left: ModelScore, right: ModelScore, records: dict[str, Sequence[CaseRecord]]
) -> bool:
    """Is the gap between two models bigger than the noise on either of them?

    The reporting rule this exists to enforce: **refuse to report a between-model
    difference smaller than the within-measurement variance.** If that eats the
    headline result, the headline result was noise, and saying so is the more
    useful output.
    """
    left_f1, right_f1 = left.micro_f1, right.micro_f1
    if left_f1 is None or right_f1 is None:
        return False
    intervals = [
        bootstrap_f1(records.get(left.model_id, ())),
        bootstrap_f1(records.get(right.model_id, ())),
    ]
    widths = [i.half_width for i in intervals if i is not None]
    if not widths:
        return False
    return abs(left_f1 - right_f1) > max(widths)


def calibration(records: Sequence[CaseRecord], buckets: int = 10) -> list[tuple[float, float, int]]:
    """Confidence decile against how often that decile was right.

    The only direct evidence for or against `AUTO_PROMOTE_CONFIDENCE = 0.8`. If a
    model's 0.8-0.9 bucket is right half the time, the threshold is letting wrong
    values through unattended and the number to change is the threshold.

    Returns `(bucket_midpoint, empirical_accuracy, n)`, skipping empty buckets --
    an accuracy of 0 over no observations is not a data point.
    """
    tally: dict[int, list[int]] = {}
    for record in records:
        for template, confidence in record.confidences.items():
            outcome = record.cells.get(template)
            if outcome not in (CORRECT, WRONG):
                continue  # only cells the model actually asserted
            index = min(buckets - 1, max(0, int(confidence * buckets)))
            tally.setdefault(index, []).append(1 if outcome == CORRECT else 0)
    return [
        ((index + 0.5) / buckets, sum(hits) / len(hits), len(hits))
        for index, hits in sorted(tally.items())
        if hits
    ]


def _median(values: Sequence[int]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _percentile(values: Sequence[int], percentile: float) -> float:
    ordered = sorted(values)
    rank = (percentile / 100) * (len(ordered) - 1)
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return float(ordered[low])
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
