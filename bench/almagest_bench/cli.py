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
    """
    from almagest_bench.plots import Cell, Run, outcome_matrix
    from almagest_bench.stages import judge_identity

    by_case = {case.case_id: case for case in load_corpus(args.corpus)}

    # Grouped by (case, model) rather than one row per record, so repeats land in
    # the same cell. That grouping is the point: the first run of this harness
    # found the same model answering a case once and exhausting its reasoning
    # budget the next time, and a per-record plot would have shown two unrelated
    # verdicts instead of one unstable one.
    grouped: dict[tuple[str, str], list[Run]] = {}
    for record in read_cases(args.records):
        if record.stage != "vision":
            continue
        call = record.calls[0] if record.calls else None
        if record.error:
            run = Run(
                case_id=record.case_id,
                model_id=record.model_id,
                outcome="error",
                proposed="no answer",
                confidence=None,
                latency_ms=call.latency_ms if call else None,
                prompt_tokens=None,
            )
        else:
            # `ranked[0]`, never the first key of `cells`: records are written
            # with sort_keys, so dict order is alphabetical rather than ranked.
            top = record.ranked[0] if record.ranked else None
            case = by_case.get(record.case_id)
            if case is None:
                outcome = "none"
            elif top is None:
                # No answer. On an item with no part number that is the right one.
                outcome = "correct" if case.unidentifiable else "none"
            else:
                outcome = judge_identity(case, top)
            run = Run(
                case_id=record.case_id,
                model_id=record.model_id,
                outcome=outcome,
                proposed=top or "",
                confidence=record.confidences.get(f"identity:{top}") if top else None,
                latency_ms=call.latency_ms if call else None,
                prompt_tokens=call.prompt_tokens if call else None,
            )
        grouped.setdefault((record.case_id, record.model_id), []).append(run)

    if not grouped:
        print("no vision records in that file", file=sys.stderr)
        return 1

    cells = [Cell(case_id=key[0], model_id=key[1], runs=runs) for key, runs in grouped.items()]
    print(outcome_matrix(cells, args.out, title=args.title, subtitle=args.subtitle))
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
