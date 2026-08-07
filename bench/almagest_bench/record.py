"""What one benchmarked case produced, and how it is written down.

## JSONL, appended and flushed, rather than a database

The realistic failure mode of an overnight run is a kill at 03:40 -- a laptop
sleeping, a session ending, a `--max-hours` guard firing. An append-only file is
complete up to its last flush with no recovery step and no half-written
transaction; a SQLite file interrupted mid-write needs care before it can be read.
A night is roughly six hundred records, not a million, so nothing here needs an
index.

## Scoring is a separate pass over the file, deliberately

`CaseRecord` stores **what happened**, not what it was worth: the cells, the
outcomes, the promotions, the tokens. Turning that into precision and recall is
`metrics.py`, run afterwards from the file. That split is what makes a scoring bug
cost a re-score rather than a re-run -- and on a matrix that takes seven hours and
a GPU handover, the difference between those two is the difference between fixing
it and not bothering.

## Nothing here invents a number

Every field is either measured or absent. `None` means the server did not say, and
never zero: a run that silently recorded absent token counts as zero would pull
every average toward whichever models were quiet, which is the sort of error that
makes a benchmark confidently wrong rather than merely incomplete.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: How a per-cell outcome is named. Five, not two, because precision and recall
#: need different denominators and collapsing them loses the distinction that
#: matters most: a field the model invented is not the same mistake as a field it
#: read wrongly, and neither is the same as one it correctly declined to answer.
CellOutcome = str
CORRECT: CellOutcome = "correct"
WRONG: CellOutcome = "wrong"
MISSING: CellOutcome = "missing"
#: The truth says this field is absent from the datasheet and the model asserted
#: one anyway. Only measurable because the corpus records `absent` explicitly.
HALLUCINATED: CellOutcome = "hallucinated"
CORRECT_OMISSION: CellOutcome = "correct_omission"

CELL_OUTCOMES = (CORRECT, WRONG, MISSING, HALLUCINATED, CORRECT_OMISSION)


@dataclass(frozen=True)
class CallRecord:
    """One model call. Mirrors `CallStats`, plus how it failed if it did."""

    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    #: `ModelUnavailable`'s message. A failed call is still a call and still cost
    #: wall-clock, so it is recorded rather than dropped -- a model that fails
    #: slowly and a model that fails instantly are different problems.
    error: str | None = None

    @property
    def tokens_per_second(self) -> float | None:
        if self.completion_tokens is None or self.latency_ms <= 0:
            return None
        return self.completion_tokens / (self.latency_ms / 1000)


@dataclass(frozen=True)
class SwapRecord:
    """A model handover, which is a real cost and is not amortised away here.

    A model that wins by three points of F1 and costs forty minutes of GPU
    handover before answering anything has not obviously won. Hiding that in a
    footnote is how it gets decided wrongly, so the swap gets its own row and its
    own segment on the wall-clock chart.
    """

    model_id: str
    base_url: str
    #: `already_ready` | `swapped` | `pending_or_starting` | `preempted`
    outcome: str
    ready_seconds: float
    #: Measured and then discarded from the latency figures. The first call after
    #: a swap pays for weights still arriving, and folding that into case 1 makes
    #: median latency depend on corpus ordering.
    warmup_seconds: float = 0.0
    released: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class CaseRecord:
    """One (model, stage, case, repeat). The unit the file is made of."""

    run_id: str
    model_id: str
    served_name: str
    base_url: str
    #: `extraction` | `research` | `vision`
    stage: str
    case_id: str
    repeat: int
    started_at: str
    wall_ms: int
    calls: tuple[CallRecord, ...] = ()

    # -- identity ---------------------------------------------------------
    identity_exact: bool = False
    #: The same comparison under `normalize_mpn`. The gap between this and the
    #: exact one is real and interesting: case, hyphens, packaging suffixes.
    identity_normalised: bool = False
    #: Part numbers the model returned that the catalogue could not match.
    unclaimed: tuple[str, ...] = ()
    #: **Should always be empty.** `schema_for` makes `template_name` an enum, so
    #: a non-empty value here is not a fact about the model -- it is a fact about
    #: the deployment, namely that this server ignored the constraint. Charted
    #: separately and loudly for that reason.
    unknown_templates: tuple[str, ...] = ()

    # -- fields -----------------------------------------------------------
    #: template name -> one of CELL_OUTCOMES.
    #:
    #: **Order carries no meaning here and must not be relied on.** Records are
    #: written with `sort_keys=True`, which sorts nested dicts as well, so any
    #: ranking expressed as key order is silently destroyed on the way to disk.
    #: That happened: the vision stage put its ranked candidates in this dict and
    #: the chart drew the alphabetically-first one as the model's top answer.
    #: Anything ordered goes in `ranked`.
    cells: dict[str, CellOutcome] = field(default_factory=dict)
    #: What the model proposed, **in the order it proposed it**, best first.
    #:
    #: A list rather than dict ordering because rank is load-bearing: the first
    #: entry is what a stub part would actually be created from, and the
    #: difference between "the right answer was first" and "the right answer was
    #: third" is most of what a proposal-based stage is measuring.
    ranked: tuple[str, ...] = ()
    #: template name -> the model's own confidence, for the calibration curve.
    confidences: dict[str, float] = field(default_factory=dict)

    # -- what the pipeline did with it ------------------------------------
    promoted: tuple[str, ...] = ()
    queued_for_review: tuple[str, ...] = ()
    #: **The headline safety number.** Fields written to `parameter_value` whose
    #: value disagrees with truth. Everything else in this record is recoverable
    #: by looking at it; a wrongly promoted value is stored as fact and nobody
    #: checks it again. A model with higher F1 and more of these is the worse
    #: model.
    wrongly_promoted: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    confirmed: tuple[str, ...] = ()
    research_state: str | None = None
    #: Set when the case did not complete. The record is still written: a case
    #: that fell over is data, and silently omitting it would make a model that
    #: crashes on hard datasheets look like a model that finds them easy.
    error: str | None = None

    @property
    def key(self) -> tuple[str, str, str, int]:
        """What `--resume` skips on."""
        return (self.model_id, self.stage, self.case_id, self.repeat)

    @property
    def total_latency_ms(self) -> int:
        return sum(call.latency_ms for call in self.calls)


class RecordWriter:
    """Append-and-flush. Opened once per run, closed by a context manager.

    `flush()` after every line and not only at close, because the point of the
    format is that a kill leaves a readable file. Buffering would give that up for
    an amount of I/O that is irrelevant at six hundred records a night.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8")

    def write(self, record: CaseRecord | SwapRecord) -> None:
        self._handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> RecordWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_cases(path: Path) -> Iterator[CaseRecord]:
    """Every case in the file, tolerating a truncated final line.

    A run killed mid-write leaves a partial last line. Refusing to read the file
    because of it would throw away the whole night to save the last record, so the
    partial line is dropped and everything before it is returned.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            body: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue  # the truncated tail of an interrupted run
        calls = tuple(CallRecord(**call) for call in body.pop("calls", []))
        yield CaseRecord(**body, calls=calls)


def completed_keys(path: Path) -> set[tuple[str, str, str, int]]:
    """What a `--resume` must not run again.

    Cases that recorded an `error` are **not** counted as done: a resumed run
    should retry the ones that fell over, since a transient model failure is the
    most likely reason a night is missing records at all.
    """
    return {record.key for record in read_cases(path) if record.error is None}
