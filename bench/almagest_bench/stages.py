"""Running one case through one model, and writing down what happened.

A stage is deliberately thin: it calls the shipped code, times it, and records.
It does **not** decide whether the answer was any good -- that is `metrics.py`,
run afterwards over the file. The split is what makes a scoring bug cost a
re-score rather than a re-run, and on a matrix that costs a GPU handover per
model that is the difference between fixing it and living with it.

## The vision stage measures identity, and identity has three outcomes

Not two. `correct` and `wrong` would collapse the finding that made this whole
corpus worth having:

* **correct** -- the part number, under `normalize_mpn`.
* **distractor** -- something printed on the label that is not the part number:
  an FCC ID, a regulatory IC number, an OUI, a distributor ordering code. The
  model read the image correctly and misunderstood what it was looking at.
  Observed: `qwen3-vl:8b` answered `MCQ-XBEE3` at confidence 0.95 for a module
  whose part number is `XB3-24Z8UM`.
* **fabricated** -- a part number that appears nowhere on the label and nowhere
  in the case. This is the failure the never-auto-accept rule exists for.

The first two are prompt problems. The third is not, and averaging them together
would hide which one you have.

## Nothing here is scored against a model's own confidence

It is recorded, for the calibration curve, and used for nothing else. The
measured reason: on the case it got wrong, this model reported 0.95.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.services.enrichment.openai_compat import ModelUnavailable
from app.services.enrichment.vision import VisionProvider, VisionRequest
from app.services.scanning.codes import normalize_mpn

from almagest_bench.corpus import Case
from almagest_bench.record import CallRecord, CaseRecord

#: How an identity answer relates to the truth. See the module docstring.
IDENTITY_OUTCOMES = ("correct", "distractor", "fabricated", "none")


@dataclass(frozen=True)
class IdentityJudgement:
    """What one proposed part number was, relative to the case."""

    proposed: str
    outcome: str
    confidence: float
    #: Where the model said it read this. The thing a reviewer checks, and the
    #: thing that caught the confidently-wrong answer on the XBee case.
    source_text: str


def judge_identity(case: Case, proposed: str) -> str:
    """`correct`, `distractor` or `fabricated` for one proposed part number.

    Matched under `normalize_mpn` rather than verbatim, because that is what the
    rest of the system matches on: `cross_check.ingest` uses it to decide whether
    a model's variant is a catalogue part at all, and a benchmark that judged
    identity more strictly than the pipeline does would report failures the
    pipeline would not have.
    """
    key = normalize_mpn(proposed)
    if key and key == normalize_mpn(case.mpn):
        return "correct"
    if any(key == normalize_mpn(distractor) for distractor in case.distractors):
        return "distractor"
    # A near-miss that *contains* the right answer is still not the right answer
    # -- `XB3-24Z8UM 0013A2004` is the part number with the adjacent OUI glued
    # on, and nothing downstream can look it up. It is classed as a distractor
    # rather than fabricated because every character came off the label: the
    # model read correctly and segmented badly, which is a different fix.
    if key and normalize_mpn(case.mpn) in key:
        return "distractor"
    return "fabricated"


def run_vision_case(
    case: Case,
    provider: VisionProvider,
    *,
    run_id: str,
    model_id: str,
    served_name: str,
    base_url: str,
    repeat: int = 0,
    max_candidates: int = 3,
    use_hints: bool = False,
) -> CaseRecord:
    """One photograph through one model, recorded.

    `use_hints` decides whether the browser's barcode and OCR readings go in.
    Default off, and that is the honest measurement: with a decoded barcode the
    part number is already known and the model is only confirming, which measures
    almost nothing. The hinted variant is worth running separately to see whether
    a model *contradicts* an anchor it was given, which would be its own finding.
    """
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    photo = case.capture_path
    wall = time.perf_counter()

    if photo is None:
        return CaseRecord(
            run_id=run_id,
            model_id=model_id,
            served_name=served_name,
            base_url=base_url,
            stage="vision",
            case_id=case.case_id,
            repeat=repeat,
            started_at=started_at,
            wall_ms=0,
            error="no photograph on this machine; fetch or upload it first",
        )

    request = VisionRequest(
        image=photo.read_bytes(),
        media_type="image/png" if photo.suffix == ".png" else "image/jpeg",
        document_sha256=case.capture_sha256 or "0" * 64,
        max_candidates=max_candidates,
    )

    try:
        result = provider.read(request)
    except ModelUnavailable as error:
        elapsed = round((time.perf_counter() - wall) * 1000)
        return CaseRecord(
            run_id=run_id,
            model_id=model_id,
            served_name=served_name,
            base_url=base_url,
            stage="vision",
            case_id=case.case_id,
            repeat=repeat,
            started_at=started_at,
            wall_ms=elapsed,
            calls=(
                CallRecord(
                    latency_ms=getattr(error, "elapsed_ms", None) or elapsed, error=str(error)
                ),
            ),
            error=str(error),
        )

    elapsed = round((time.perf_counter() - wall) * 1000)
    stats = result.stats
    call = CallRecord(
        latency_ms=stats.latency_ms if stats else elapsed,
        prompt_tokens=stats.prompt_tokens if stats else None,
        completion_tokens=stats.completion_tokens if stats else None,
        finish_reason=stats.finish_reason if stats else None,
    )

    judgements = [
        IdentityJudgement(
            proposed=candidate.mpn,
            outcome=judge_identity(case, candidate.mpn),
            confidence=candidate.confidence,
            source_text=candidate.source_text,
        )
        for candidate in result.candidates
    ]

    # Ranked, so what matters most is the *first* one -- that is what a stub part
    # would be created from. The rest are recorded because "the right answer was
    # second" is a different and much more recoverable failure than "the right
    # answer was absent", and a reviewer sees both.
    top = judgements[0].outcome if judgements else "none"
    anywhere = any(j.outcome == "correct" for j in judgements)

    return CaseRecord(
        run_id=run_id,
        model_id=model_id,
        served_name=served_name,
        base_url=base_url,
        stage="vision",
        case_id=case.case_id,
        repeat=repeat,
        started_at=started_at,
        wall_ms=elapsed,
        calls=(call,),
        identity_exact=top == "correct",
        # Reused to mean "the right answer was somewhere in the ranked list",
        # which for a proposal-based stage is the more interesting number: the
        # review screen shows all of them.
        identity_normalised=anywhere,
        unclaimed=tuple(j.proposed for j in judgements if j.outcome != "correct"),
        ranked=tuple(j.proposed for j in judgements),
        cells={f"identity:{j.proposed}": j.outcome for j in judgements},
        confidences={f"identity:{j.proposed}": j.confidence for j in judgements},
    )


def summarise_vision(records: list[CaseRecord]) -> dict[str, object]:
    """A first-look tally. Not a benchmark -- a corpus this small cannot be one.

    Deliberately reports counts rather than rates. A percentage over two cases
    reads as a measurement and is not one, and this function exists precisely for
    the period before the corpus is big enough for `metrics.py` to be meaningful.
    """
    outcomes: dict[str, int] = {}
    for record in records:
        top = "error" if record.error else ("correct" if record.identity_exact else "other")
        if not record.error and not record.identity_exact:
            # Which kind of "other" -- the distinction is the whole point.
            first = next(iter(record.cells.values()), "none")
            top = first
        outcomes[top] = outcomes.get(top, 0) + 1
    latencies = [c.latency_ms for r in records for c in r.calls if not c.error]
    prompts = [c.prompt_tokens for r in records for c in r.calls if c.prompt_tokens]
    return {
        "cases": len(records),
        "outcomes": outcomes,
        "median_latency_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
        "median_prompt_tokens": sorted(prompts)[len(prompts) // 2] if prompts else None,
    }
