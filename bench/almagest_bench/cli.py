"""`almagest-bench` — run the corpus, write the records, draw the picture.

    almagest-bench corpus check
    almagest-bench vision --base-url http://localhost:11434 --model qwen3-vl:8b
    almagest-bench plot out/bench/<run>/records.jsonl out/bench/<run>/vision.png

Deliberately three commands rather than one. Running costs a GPU and scoring does
not, so a scoring or plotting mistake must never require re-running: the sweep
writes JSONL and stops, and everything downstream reads that file.

Models are run **one at a time, named explicitly**, rather than from a matrix
file. On this cluster only one model server holds the card, and a plan that
implied otherwise would be a plan that quietly serialised anyway. The caller
sequences them; `cluster.swap_to` is there for when that becomes worth
automating.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from almagest_bench.corpus import load_corpus, summarise
from almagest_bench.record import RecordWriter, read_cases
from almagest_bench.stages import run_vision_case, summarise_vision


def _run_dir(root: Path, run_id: str) -> Path:
    path = root / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_corpus_check(args: argparse.Namespace) -> int:
    cases = load_corpus(args.corpus)
    summary = summarise(cases)
    print(f"{summary.cases} cases · {summary.cells} truth cells · {summary.absent_cells} absent")
    print(f"truth sources: {summary.by_source or '(none)'}")
    for case in cases:
        photo = case.capture_path
        mark = "photo" if photo else "NO PHOTO"
        verified = "" if case.verify_capture() else "  !! HASH MISMATCH"
        name = case.mpn or "(no part number printed)"
        print(f"  {case.case_id:28s} {name:26s} {mark}{verified}")
    warnings = summary.warnings()
    if warnings:
        print("\nwhat this corpus cannot yet support:")
        for warning in warnings:
            print(f"  - {warning}")
    # A corpus with a mismatched photograph is a corpus scoring the wrong
    # picture, which is worse than one with no picture at all.
    return 1 if any(not case.verify_capture() for case in cases) else 0


def cmd_vision(args: argparse.Namespace) -> int:
    from app.services.enrichment.vision_openai_compat import for_base_url

    cases = load_corpus(args.corpus)
    if args.limit:
        cases = cases[: args.limit]

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    run_dir = _run_dir(args.out, run_id)
    records_path = run_dir / "records.jsonl"

    provider = for_base_url(args.base_url, args.model, timeout=args.timeout)

    print(f"run {run_id} · {args.model} · {len(cases)} cases -> {records_path}")
    produced = []
    with RecordWriter(records_path) as writer:
        for case in cases:
            for repeat in range(args.repeats):
                record = run_vision_case(
                    case,
                    provider,
                    run_id=run_id,
                    model_id=args.model,
                    served_name=args.model,
                    base_url=args.base_url,
                    repeat=repeat,
                    max_candidates=args.max_candidates,
                )
                writer.write(record)
                produced.append(record)
                said = record.ranked[0] if record.ranked else "(nothing)"
                verdict = "ERROR" if record.error else record.cells.get(f"identity:{said}", "none")
                print(
                    f"  {case.case_id:28s} {verdict:12s} {said[:36]}  {record.wall_ms / 1000:.1f}s"
                )

    print(json.dumps(summarise_vision(produced), indent=2))
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "model": args.model,
                "base_url": args.base_url,
                "cases": len(cases),
                "repeats": args.repeats,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def cmd_plot(args: argparse.Namespace) -> int:
    """Draw the matrix, **re-judging every answer from the corpus as it goes.**

    The verdict is recomputed here rather than read off the record, and that is a
    correction to how this worked at first. `stages.py` writes an outcome at run
    time, which quietly made scoring part of the expensive step: adding the
    `misread` category would have meant paying for another GPU sweep to see its
    effect. Re-judging from `ranked` -- which is the raw thing the model said --
    keeps the promise the design made, that a scoring change costs a re-score.

    The judging itself now lives in `metrics.judge_records`, shared with `score`.
    It was inline here, which meant the table and the picture could have drawn
    different conclusions from one file -- and a chart disagreeing with the console
    is precisely how the ranked-candidate bug in the handoff was found. One
    function, one verdict.
    """
    from almagest_bench.metrics import judge_records
    from almagest_bench.plots import Cell, Run, outcome_matrix

    by_case = {case.case_id: case for case in load_corpus(args.corpus)}
    records = [record for record in read_cases(args.records) if record.stage == "vision"]

    # Grouped by (case, model) rather than one row per record, so repeats land in
    # the same cell. That grouping is the point: the first run of this harness
    # found the same model answering a case once and exhausting its reasoning
    # budget the next time, and a per-record plot would have shown two unrelated
    # verdicts instead of one unstable one.
    grouped: dict[tuple[str, str], list[Run]] = {}
    for record, outcome in judge_records(records, by_case):
        call = record.calls[0] if record.calls else None
        top = record.ranked[0] if record.ranked else None
        grouped.setdefault((record.case_id, record.model_id), []).append(
            Run(
                case_id=record.case_id,
                model_id=record.model_id,
                outcome=outcome,
                proposed=("no answer" if record.error else (top or "")),
                confidence=(
                    None
                    if record.error or top is None
                    else record.confidences.get(f"identity:{top}")
                ),
                latency_ms=call.latency_ms if call else None,
                prompt_tokens=None if record.error else (call.prompt_tokens if call else None),
            )
        )

    if not grouped:
        print("no vision records in that file", file=sys.stderr)
        return 1

    cells = [Cell(case_id=key[0], model_id=key[1], runs=runs) for key, runs in grouped.items()]
    print(outcome_matrix(cells, args.out, title=args.title, subtitle=args.subtitle))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Score a recorded sweep. No GPU, no model, no network — just the JSONL.

    The third command the design always implied and nobody had written:
    `metrics.py` existed complete and unreachable, so every number anybody quoted
    from a run came off `cmd_vision`'s inline summary, which is computed *during*
    the expensive step and therefore cannot be corrected without re-running.

    Three refusals are the point of this command, and each one is a way the
    benchmark could otherwise mislead:

    * **No percentages below `MIN_CASES_FOR_RATES`.** `plots.refuse_rate_chart`
      already declines to draw a rate over twelve cases; printing "50%" as text
      instead would defeat that by the shortest possible route. Counts are printed
      and the floor is named.
    * **No between-model comparison the noise does not support.** `resolvable`'s
      rule, via `identity_resolvable`. At twelve cases almost nothing clears it,
      and reporting *that* is more useful than a ranking.
    * **`fabricated` is reported on its own line**, never folded into a total
      wrong count. A misread is repaired by the anchor or a better photograph; an
      invention is repaired by not trusting the model.
    """
    from almagest_bench.metrics import (
        bootstrap_identity,
        identity_resolvable,
        judge_records,
        score_identity,
    )
    from almagest_bench.plots import MIN_CASES_FOR_RATES
    from almagest_bench.stages import IDENTITY_OUTCOMES

    by_case = {case.case_id: case for case in load_corpus(args.corpus)}
    records = [record for record in read_cases(args.records) if record.stage == "vision"]
    if not records:
        # Named rather than a bare "no records": the other stages are the next
        # thing to be built, and a reader hitting this should learn that rather
        # than wonder whether their file is corrupt.
        print(
            "no vision records in that file. The extraction and research sweeps "
            "are not built yet, so `vision` is the only stage that writes records.",
            file=sys.stderr,
        )
        return 1

    judged = judge_records(records, by_case)
    by_model: dict[str, list[tuple[Any, str]]] = {}
    for record, outcome in judged:
        by_model.setdefault(record.model_id, []).append((record, outcome))

    scores = {
        model_id: score_identity(model_id, rows, outcomes=(*IDENTITY_OUTCOMES, "error"))
        for model_id, rows in sorted(by_model.items())
    }

    report: dict[str, Any] = {"stage": "vision", "models": {}}
    for model_id, score in scores.items():
        interval = bootstrap_identity(by_model[model_id])
        entry: dict[str, Any] = {
            "cases": score.cases,
            "outcomes": score.outcomes,
            "errors": score.errors,
            "median_wall_seconds": score.median_wall_seconds,
            "median_prompt_tokens": score.median_prompt_tokens,
            # `None` under the floor, and the key is still present — so a consumer
            # sees "we declined to compute this" rather than a missing field it
            # might fill in itself.
            "correct_rate": score.rate("correct", min_cases=MIN_CASES_FOR_RATES),
            "rate_floor": MIN_CASES_FOR_RATES,
            "correct_interval": (
                None if interval is None else {"low": interval.low, "high": interval.high}
            ),
        }
        report["models"][model_id] = entry

    # Every ordered pair once, with the verdict on whether the gap is even
    # reportable. Included when it is *not* resolvable too: "these two models are
    # indistinguishable on this corpus" is a finding, and omitting it would leave a
    # reader to assume nobody checked.
    comparisons = []
    model_ids = list(scores)
    for index, left in enumerate(model_ids):
        for right in model_ids[index + 1 :]:
            comparisons.append(
                {
                    "left": left,
                    "right": right,
                    "resolvable": identity_resolvable(scores[left], scores[right], by_model),
                }
            )
    report["comparisons"] = comparisons

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"{len(records)} vision record(s) over {len(by_case)} corpus case(s)\n")
    for model_id, score in scores.items():
        print(f"{model_id}")
        print(f"  cases           {score.cases}")
        for outcome in (*IDENTITY_OUTCOMES, "error"):
            count = score.outcomes.get(outcome, 0)
            mark = "  <-- the one that matters" if outcome == "fabricated" and count else ""
            print(f"  {outcome:15s} {count}{mark}")
        rate = score.rate("correct", min_cases=MIN_CASES_FOR_RATES)
        if rate is None:
            print(
                f"  correct rate    not reported: {score.cases} cases is under the "
                f"{MIN_CASES_FOR_RATES}-case floor for a percentage"
            )
        else:
            print(f"  correct rate    {rate:.0%}")
        interval = bootstrap_identity(by_model[model_id])
        if interval is not None:
            print(
                f"  95% interval    {interval.low:.0%}..{interval.high:.0%} "
                f"(+/-{interval.half_width:.0%}, bootstrapped over cases)"
            )
        if score.median_wall_seconds is not None:
            print(f"  median wall     {score.median_wall_seconds:.1f}s")
        if score.median_prompt_tokens is not None:
            print(f"  median prompt   {score.median_prompt_tokens:.0f} tokens")
        print()

    for comparison in comparisons:
        verdict = (
            "the gap is bigger than the noise"
            if comparison["resolvable"]
            else "INDISTINGUISHABLE on this corpus — do not rank them"
        )
        print(f"{comparison['left']} vs {comparison['right']}: {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="almagest-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    corpus = sub.add_parser("corpus", help="inspect the corpus")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    check = corpus_sub.add_parser("check", help="what it holds and what it cannot support")
    check.add_argument("--corpus", type=Path, default=Path("corpus"))
    check.set_defaults(func=cmd_corpus_check)

    vision = sub.add_parser("vision", help="run the identity stage over the corpus")
    vision.add_argument("--corpus", type=Path, default=Path("corpus"))
    vision.add_argument("--out", type=Path, default=Path("../out/bench"))
    vision.add_argument("--base-url", default="http://localhost:11434")
    vision.add_argument("--model", required=True)
    vision.add_argument("--run-id", default=None)
    vision.add_argument("--limit", type=int, default=None)
    vision.add_argument("--repeats", type=int, default=1)
    vision.add_argument("--max-candidates", type=int, default=3)
    vision.add_argument("--timeout", type=float, default=600.0)
    vision.set_defaults(func=cmd_vision)

    score = sub.add_parser("score", help="score recorded runs — no GPU, no model")
    score.add_argument("records", type=Path)
    score.add_argument("--corpus", type=Path, default=Path("corpus"))
    score.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable. Rates below the case floor come back null, not omitted.",
    )
    score.set_defaults(func=cmd_score)

    plot = sub.add_parser("plot", help="draw an outcome matrix from recorded runs")
    plot.add_argument("records", type=Path)
    plot.add_argument("out", type=Path)
    plot.add_argument("--corpus", type=Path, default=Path("corpus"))
    plot.add_argument("--title", default="Reading a part number off a photograph")
    plot.add_argument("--subtitle", default="")
    plot.set_defaults(func=cmd_plot)

    args = parser.parse_args(argv)
    result: Any = args.func(args)
    return int(result)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
