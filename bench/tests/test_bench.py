"""The parts of the harness that would be wrong silently.

Every test here guards a way this benchmark could produce a confident number that
is not true, which is a worse outcome than producing no number at all:

* a plan that costs more GPU handovers than its author thinks
* a corpus that cannot measure precision, or whose truth came from the thing
  being measured, loading without complaint
* a night's records lost because the process was killed mid-write
* a precision of 0.0 reported for a model that asserted nothing
* a confidence interval computed over cells instead of cases, which halves it

None of this touches a cluster, a model or a GPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from almagest_bench.cluster import swap_count
from almagest_bench.corpus import CorpusError, load_corpus, summarise
from almagest_bench.metrics import FieldScore, ModelScore, bootstrap_f1, score_model
from almagest_bench.record import CallRecord, CaseRecord, RecordWriter, completed_keys, read_cases


class _Choice:
    """Enough of a `ModelChoice` for `swap_count`, without importing the app."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


OLLAMA = "http://almagest-llm:11434"
VLLM = "http://almagest-llm-27b:8000"


# ---------------------------------------------------------------------------
# Plan ordering
# ---------------------------------------------------------------------------


def test_two_ollama_models_in_a_row_cost_no_gpu_handover() -> None:
    """The single most valuable ordering fact about a night's run.

    The 4B and 8B share one deployment, so moving between them is a weight reload
    rather than a rollout. Counting swaps by base URL rather than by model is what
    makes "put them next to each other" visibly worth doing.
    """
    assert swap_count([_Choice(OLLAMA), _Choice(OLLAMA)]) == 0


def test_interleaving_models_costs_a_swap_every_time() -> None:
    # The natural-looking plan, and the one that would spend the night loading
    # weights instead of measuring.
    assert swap_count([_Choice(OLLAMA), _Choice(VLLM), _Choice(OLLAMA), _Choice(VLLM)]) == 3
    # The same three models, ordered properly: one handover for the night.
    assert swap_count([_Choice(OLLAMA), _Choice(OLLAMA), _Choice(VLLM)]) == 1


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def _write_case(root: Path, name: str, body: dict[str, object], text: str = "datasheet") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "case.json").write_text(json.dumps(body), encoding="utf-8")
    (directory / "text.txt").write_text(text, encoding="utf-8")
    return directory


def _case_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "mpn": "CF14JT100K",
        "manufacturer": "Stackpole",
        "expected": {
            "resistance": {"raw_value": "100 kOhm", "truth_source": "barcode"},
            "power_rating": {"raw_value": "0.25 W", "truth_source": "human"},
        },
        "absent": ["tolerance"],
    }
    body.update(overrides)
    return body


def test_a_case_loads_with_its_truth_and_its_absences(tmp_path: Path) -> None:
    _write_case(tmp_path, "0001-cf14jt100k", _case_body())
    case = load_corpus(tmp_path)[0]

    assert case.mpn == "CF14JT100K"
    assert case.expected["resistance"].truth_source == "barcode"
    assert case.absent == ("tolerance",)
    # The model is asked for the absent field too. Asking only for fields that
    # have answers would make omission impossible to get wrong, and "did it
    # decline to invent one" is half of what is measured.
    assert case.requested_fields == ("power_rating", "resistance", "tolerance")


def test_a_field_both_stated_and_absent_is_refused(tmp_path: Path) -> None:
    # Whichever way it were resolved, one of the two scoring rules would be
    # silently wrong for that cell.
    _write_case(tmp_path, "0001-x", _case_body(absent=["resistance"]))
    with pytest.raises(CorpusError, match="both expected and absent"):
        load_corpus(tmp_path)


def test_an_unknown_truth_source_is_refused(tmp_path: Path) -> None:
    """Because `truth_source` is what makes a poisoned corpus detectable.

    An unrecognised value would silently fall outside `INDEPENDENT_SOURCES` and
    quietly reclassify a cell nobody meant to reclassify.
    """
    _write_case(
        tmp_path,
        "0001-x",
        _case_body(expected={"resistance": {"raw_value": "1 kOhm", "truth_source": "vibes"}}),
    )
    with pytest.raises(CorpusError, match="truth_source"):
        load_corpus(tmp_path)


def test_a_corpus_with_no_absent_fields_says_precision_is_unmeasurable(tmp_path: Path) -> None:
    """The warning that matters most, and the one easiest to skip past.

    Without `absent`, a hallucinated value is indistinguishable from a value
    nobody labelled, and every hallucination scores as a missing label.
    """
    _write_case(tmp_path, "0001-x", _case_body(absent=[]))
    warnings = summarise(load_corpus(tmp_path)).warnings()
    assert any("Precision cannot be measured" in w for w in warnings)


def test_a_mostly_model_labelled_corpus_is_called_out(tmp_path: Path) -> None:
    _write_case(
        tmp_path,
        "0001-x",
        _case_body(
            expected={
                "resistance": {"raw_value": "100 kOhm", "truth_source": "model"},
                "power_rating": {"raw_value": "0.25 W", "truth_source": "model"},
            }
        ),
    )
    warnings = summarise(load_corpus(tmp_path)).warnings()
    assert any("measured itself" in w for w in warnings)


def test_a_small_corpus_warns_about_clustering(tmp_path: Path) -> None:
    _write_case(tmp_path, "0001-x", _case_body())
    warnings = summarise(load_corpus(tmp_path)).warnings()
    assert any("effective n is the case count" in w for w in warnings)


def test_an_empty_corpus_raises_rather_than_scoring_nothing(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="no cases"):
        load_corpus(tmp_path)


def test_a_case_can_point_at_a_photograph_committed_elsewhere(tmp_path: Path) -> None:
    """So the DigiKey bag is not committed twice.

    It already lives at `frontend/src/lib/capture/fixtures/` and `test_vision.py`
    asserts its sha256. A second copy in the corpus would be 240 KB that can
    silently diverge from the file those tests pin, which is the one kind of
    drift a corpus must not have.
    """
    root = tmp_path / "bench" / "corpus"
    root.mkdir(parents=True)
    elsewhere = tmp_path / "frontend" / "fixtures"
    elsewhere.mkdir(parents=True)
    (elsewhere / "label.jpg").write_bytes(b"\xff\xd8\xff")
    _write_case(root, "0001-x", _case_body(capture="frontend/fixtures/label.jpg"))

    case = load_corpus(root)[0]
    assert case.capture_path is not None
    assert case.capture_path.read_bytes() == b"\xff\xd8\xff"


def test_a_relative_corpus_path_still_finds_the_photograph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug the absolute-path test above could not catch.

    `corpus/0001-x` has fewer than three parents, so walking up to the repository
    root raised IndexError -- which the first version of this feature did, the
    first time it was pointed at the real corpus from inside `bench/`.
    """
    root = tmp_path / "bench" / "corpus"
    root.mkdir(parents=True)
    elsewhere = tmp_path / "frontend" / "fixtures"
    elsewhere.mkdir(parents=True)
    (elsewhere / "label.jpg").write_bytes(b"\xff\xd8\xff")
    _write_case(root, "0001-x", _case_body(capture="frontend/fixtures/label.jpg"))

    monkeypatch.chdir(tmp_path / "bench")
    case = load_corpus(Path("corpus"))[0]

    assert case.capture_path is not None
    assert case.capture_path.read_bytes() == b"\xff\xd8\xff"


def test_a_distractor_is_recorded_so_it_can_be_scored_apart(tmp_path: Path) -> None:
    """Returning the FCC ID is a different failure from inventing a part number.

    Measured, not hypothetical: on the XBee case `qwen3-vl:8b` answered
    `MCQ-XBEE3` -- the FCC ID printed two lines above the real part number -- at
    confidence 0.95. It read the image correctly and misunderstood what it was
    looking at, which is fixable in the prompt. A part number that appears
    nowhere on the label is a different problem entirely.
    """
    _write_case(root := tmp_path, "0001-x", _case_body(distractors=["MCQ-XBEE3", "0013A2004"]))
    assert load_corpus(root)[0].distractors == ("MCQ-XBEE3", "0013A2004")


# ---------------------------------------------------------------------------
# The record file
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> CaseRecord:
    body: dict[str, object] = {
        "run_id": "r1",
        "model_id": "qwen3-8b",
        "served_name": "qwen3:8b",
        "base_url": OLLAMA,
        "stage": "extraction",
        "case_id": "0001-x",
        "repeat": 0,
        "started_at": "2026-08-07T00:00:00Z",
        "wall_ms": 1200,
        "calls": (CallRecord(latency_ms=1100, prompt_tokens=900, completion_tokens=40),),
    }
    body.update(overrides)
    return CaseRecord(**body)  # type: ignore[arg-type]


def test_records_round_trip_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    with RecordWriter(path) as writer:
        writer.write(_record(cells={"resistance": "correct"}))

    (restored,) = list(read_cases(path))
    assert restored.cells == {"resistance": "correct"}
    assert restored.calls[0].prompt_tokens == 900


def test_a_run_killed_mid_write_still_reads(tmp_path: Path) -> None:
    """The realistic overnight failure, and why this is JSONL rather than SQLite.

    Refusing the file because of a half-written final line would throw away the
    whole night to save the last record.
    """
    path = tmp_path / "records.jsonl"
    with RecordWriter(path) as writer:
        writer.write(_record())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "r1", "model_id": "qwen3-8b", "st')

    assert len(list(read_cases(path))) == 1


def test_a_resume_retries_the_cases_that_fell_over(tmp_path: Path) -> None:
    """A transient model failure is the likeliest reason a night is incomplete.

    Skipping errored cases on resume would make the gap permanent.
    """
    path = tmp_path / "records.jsonl"
    with RecordWriter(path) as writer:
        writer.write(_record(case_id="ok"))
        writer.write(_record(case_id="broke", error="ModelUnavailable: refused"))

    done = completed_keys(path)
    assert ("qwen3-8b", "extraction", "ok", 0) in done
    assert ("qwen3-8b", "extraction", "broke", 0) not in done


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_a_model_that_asserted_nothing_has_undefined_precision_not_zero() -> None:
    """Reporting 0.0 would average in as though it had been wrong every time."""
    score = FieldScore(template="resistance", missing=5)
    assert score.precision is None
    assert score.recall == 0.0
    assert score.f1 is None


def test_a_correct_omission_is_not_counted_as_an_assertion() -> None:
    # Declining to invent a value the datasheet does not state is a right answer
    # and must not dilute precision.
    score = FieldScore(template="tolerance", correct=3, correct_omission=7)
    assert score.asserted == 3
    assert score.precision == 1.0


def test_a_hallucination_costs_precision_but_not_recall() -> None:
    score = FieldScore(template="tolerance", correct=4, hallucinated=4)
    assert score.precision == 0.5
    # Recall is about fields the datasheet actually states, and this one did not.
    assert score.recall == 1.0


def test_the_wrong_promotion_rate_is_reported_against_promotions() -> None:
    """The number that outranks accuracy: values written to `parameter_value` as
    fact that disagree with truth. Nobody checks them again."""
    score = ModelScore(model_id="m", cases=10, promoted=20, wrongly_promoted=3)
    assert score.wrong_promotion_rate == pytest.approx(0.15)


def test_the_swap_is_amortised_and_visible_rather_than_hidden() -> None:
    score = ModelScore(model_id="m", cases=60, swap_seconds=2400.0)
    assert score.amortised_swap_seconds == 40.0


def test_scoring_buckets_cells_by_template() -> None:
    records = [
        _record(cells={"resistance": "correct", "power_rating": "wrong"}),
        _record(case_id="0002", cells={"resistance": "correct", "power_rating": "missing"}),
    ]
    score = score_model("qwen3-8b", records)
    assert score.by_template["resistance"].correct == 2
    assert score.by_template["power_rating"].wrong == 1
    assert score.by_template["power_rating"].missing == 1
    assert score.micro_f1 is not None


def test_the_interval_resamples_cases_not_cells() -> None:
    """The single easiest way to make this benchmark lie.

    Every case here is internally consistent -- all its cells right or all wrong,
    which is how clustered truth actually behaves. Resampling cells would treat
    them as independent and report a far tighter interval than the data supports,
    so a real interval over this input must be wide.
    """
    good = [
        _record(case_id=f"g{i}", cells={"a": "correct", "b": "correct", "c": "correct"})
        for i in range(5)
    ]
    bad = [
        _record(case_id=f"b{i}", cells={"a": "wrong", "b": "wrong", "c": "wrong"}) for i in range(5)
    ]
    interval = bootstrap_f1(good + bad)

    assert interval is not None
    # Ten all-or-nothing cases cannot pin an F1 down tightly, and the interval
    # has to say so. A cell-wise bootstrap over the same input would report
    # roughly half this width.
    assert interval.half_width > 0.1


def test_an_interval_needs_more_than_one_case() -> None:
    assert bootstrap_f1([_record()]) is None
