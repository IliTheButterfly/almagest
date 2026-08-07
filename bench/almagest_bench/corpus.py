"""The cases, and the truth they are scored against.

## The truth is the hard part, not the harness

Everything else in this package is mechanical. This module is where a benchmark
becomes either evidence or theatre, and two decisions decide which.

**`absent` is what makes precision measurable.** A case lists not only the fields
the datasheet states but the fields it does **not**. Without that, a value the
model invented is indistinguishable from a value the corpus author simply did not
fill in, and every hallucination scores as a missing label rather than as the
error it is. Writing `absent` is tedious and it is the difference between
measuring precision and pretending to.

**`truth_source` is recorded per cell so a poisoned corpus is detectable.** If
truth came from the same family of model being evaluated, every model in that
family scores its own idiosyncrasies as correct and the largest one wins by
agreeing with whatever labelled it. Recording where each cell came from means any
chart can be redrawn excluding model-derived cells -- and **if that exclusion
changes the ranking, the benchmark has told you only about itself.**

Sources, strongest first:

* `barcode` -- decoded from a checksummed symbology. Free, deterministic and
  genuinely independent of anything being measured.
* `distributor` -- structured parametrics from Mouser or Nexar. Independent of
  the datasheet-reading path, which is the path under test.
* `human` -- a person read the datasheet and typed it.
* `model` -- **counted separately and never trusted.** Permitted so a corpus can
  be bootstrapped, but a run whose ranking depends on these cells has measured
  nothing.

## Layout

One directory per case, numbered and named, following the convention
`ecia-barcode/tests/fixtures/ecia/` already uses:

    bench/corpus/0007-cf14jt100k/
      case.json     identity, expected cells, absent fields, truth sources
      text.txt      the datasheet text -- the extraction model's actual input
      capture.jpg   the photograph, for the vision stage (optional)

`text.txt` is committed so the extraction sweep is reproducible and needs no
network: a sweep that re-fetched PDFs would spend the night benchmarking
manufacturer CDNs and would give different models different inputs on different
days. The PDFs themselves stay out of the repository -- redistribution rights are
not ours to assume -- and are fetched by sha256 into a gitignored directory when
the research sweep needs them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRUTH_SOURCES = ("barcode", "distributor", "human", "model")

#: Truth from these sources is independent of the datasheet-reading path under
#: test. `human` is included: a person reading a datasheet is not the model
#: reading a datasheet, even when they agree.
INDEPENDENT_SOURCES = frozenset({"barcode", "distributor", "human"})


class CorpusError(ValueError):
    """A case is malformed. Raised loudly at load rather than skipped.

    A silently dropped case makes a corpus smaller than it says it is, and the
    headline number of a benchmark is only as good as its denominator.
    """


@dataclass(frozen=True)
class TruthCell:
    """One field the datasheet is known to state, and where that is known from."""

    template_name: str
    raw_value: str
    truth_source: str
    note: str | None = None

    @property
    def independent(self) -> bool:
        return self.truth_source in INDEPENDENT_SOURCES


@dataclass(frozen=True)
class Case:
    """One part, its datasheet text, and everything known to be true about it."""

    case_id: str
    directory: Path
    mpn: str
    manufacturer: str | None
    #: `parameter_template.name` -> the value the datasheet states.
    expected: dict[str, TruthCell] = field(default_factory=dict)
    #: Fields the datasheet does **not** state. Asserting one of these is a
    #: hallucination and is scored as such -- see the module docstring.
    absent: tuple[str, ...] = ()
    datasheet_url: str | None = None
    document_sha256: str | None = None
    #: Set when this case is one datasheet covering several part numbers. Batched
    #: and unbatched cases are reported separately: real extraction batches up to
    #: 24 variants, and a benchmark run entirely at batch-size-1 measures a shape
    #: the system does not use.
    sibling_mpns: tuple[str, ...] = ()
    #: Strings printed on this item that **look like a part number and are not**:
    #: an FCC ID, a regulatory IC number, an OUI, a distributor ordering code.
    #:
    #: Scored separately from a random wrong answer, because they are different
    #: failures with different fixes. A model that returns `MCQ-XBEE3` read the
    #: image correctly and misunderstood what it was looking at -- fixable in the
    #: prompt. A model that returns a part number nowhere on the label invented
    #: one, which is the failure this whole pipeline is built around.
    distractors: tuple[str, ...] = ()

    @property
    def text_path(self) -> Path:
        return self.directory / "text.txt"

    #: Where the photograph lives when it is **not** beside `case.json`, as a path
    #: from the repository root.
    #:
    #: Exists for one real case: the DigiKey resistor bag is already committed as
    #: `frontend/src/lib/capture/fixtures/digikey-creased-datamatrix.jpg`, and
    #: `test_vision.py` asserts its sha256. A second copy here would be 240 KB of
    #: duplication that can silently diverge from the file those tests pin --
    #: which is exactly the kind of drift a corpus must not have.
    capture: str | None = None

    @property
    def capture_path(self) -> Path | None:
        if self.capture:
            # `resolve()` first, and it is load-bearing rather than tidiness: the
            # case directory is often reached by a relative path (`corpus/0001-x`),
            # which has fewer than three parents and raises IndexError. Found by
            # running it, not by reading it.
            #
            # Three parents up from `<root>/bench/corpus/<case>` is `<root>`.
            path = self.directory.resolve().parents[2] / self.capture
            return path if path.exists() else None
        path = self.directory / "capture.jpg"
        return path if path.exists() else None

    @property
    def batched(self) -> bool:
        return bool(self.sibling_mpns)

    @property
    def requested_fields(self) -> tuple[str, ...]:
        """Everything the model is asked for: the stated fields and the absent ones.

        Asking only for the fields that have answers would make omission
        impossible to get wrong, and "did it decline to invent one" is half of
        what is being measured.
        """
        return tuple(sorted({*self.expected, *self.absent}))

    def text(self) -> str:
        if not self.text_path.exists():
            raise CorpusError(f"{self.case_id}: no text.txt to extract from")
        return self.text_path.read_text(encoding="utf-8")

    def independent_cells(self) -> dict[str, TruthCell]:
        return {name: cell for name, cell in self.expected.items() if cell.independent}


def load_case(directory: Path) -> Case:
    path = directory / "case.json"
    if not path.exists():
        raise CorpusError(f"{directory.name}: no case.json")
    try:
        body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CorpusError(f"{directory.name}: case.json is not JSON: {error}") from error

    mpn = body.get("mpn")
    if not isinstance(mpn, str) or not mpn.strip():
        raise CorpusError(f"{directory.name}: case.json needs an mpn")

    expected: dict[str, TruthCell] = {}
    for name, cell in (body.get("expected") or {}).items():
        if not isinstance(cell, dict):
            raise CorpusError(f"{directory.name}: expected.{name} must be an object")
        source = cell.get("truth_source")
        if source not in TRUTH_SOURCES:
            raise CorpusError(
                f"{directory.name}: expected.{name}.truth_source must be one of "
                f"{list(TRUTH_SOURCES)}, got {source!r}"
            )
        raw = cell.get("raw_value")
        if not isinstance(raw, str) or not raw.strip():
            raise CorpusError(f"{directory.name}: expected.{name} needs a raw_value")
        expected[name] = TruthCell(
            template_name=name,
            raw_value=raw,
            truth_source=source,
            note=cell.get("note"),
        )

    absent = tuple(body.get("absent") or ())
    overlap = set(absent) & set(expected)
    if overlap:
        # Both stated and not stated. Whichever way it were resolved, one of the
        # two scoring rules would be silently wrong for that cell.
        raise CorpusError(f"{directory.name}: {sorted(overlap)} appear in both expected and absent")

    return Case(
        case_id=directory.name,
        directory=directory,
        mpn=mpn.strip(),
        manufacturer=body.get("manufacturer"),
        expected=expected,
        absent=absent,
        datasheet_url=body.get("datasheet_url"),
        document_sha256=body.get("document_sha256"),
        sibling_mpns=tuple(body.get("sibling_mpns") or ()),
        distractors=tuple(body.get("distractors") or ()),
        capture=body.get("capture"),
    )


def load_corpus(root: Path) -> list[Case]:
    """Every case under `root`, in directory order, refusing a malformed one."""
    if not root.exists():
        raise CorpusError(f"no corpus at {root}")
    cases = [
        load_case(directory)
        for directory in sorted(root.iterdir())
        if directory.is_dir() and not directory.name.startswith("_")
    ]
    if not cases:
        raise CorpusError(f"{root} has no cases")
    return cases


@dataclass(frozen=True)
class CorpusSummary:
    """What a corpus can and cannot support, computed before a night is spent.

    Printed by `almagest-bench corpus check`. The point is to have the
    conversation about statistical power *before* the seven-hour run rather than
    in the report afterwards.
    """

    cases: int
    cells: int
    absent_cells: int
    batched_cases: int
    by_source: dict[str, int]
    templates: tuple[str, ...]

    @property
    def model_derived_fraction(self) -> float:
        return self.by_source.get("model", 0) / self.cells if self.cells else 0.0

    def warnings(self) -> list[str]:
        """Everything that would make the eventual numbers less than they look."""
        out = []
        if self.cases < 60:
            out.append(
                f"{self.cases} cases. Truth cells are clustered -- the fields of one part "
                "come from one table and fail together -- so the effective n is the case "
                "count, not the cell count. Below about 60, only a large difference "
                "between models is resolvable; bootstrap over cases, never over cells."
            )
        if not self.absent_cells:
            out.append(
                "No `absent` fields anywhere. Precision cannot be measured: a "
                "hallucinated value is indistinguishable from an unlabelled one."
            )
        if self.model_derived_fraction > 0.2:
            out.append(
                f"{self.model_derived_fraction:.0%} of truth cells came from a model. "
                "Redraw every chart excluding them; if the ranking changes, the "
                "benchmark has measured itself."
            )
        if not self.batched_cases:
            out.append(
                "No batched cases. Real extraction sends up to 24 variants per call, "
                "so a corpus of singletons measures a shape the system does not use "
                "and will understate the wrong-variant-row error."
            )
        return out


def summarise(cases: list[Case]) -> CorpusSummary:
    by_source: dict[str, int] = {}
    templates: set[str] = set()
    cells = absent = 0
    for case in cases:
        for cell in case.expected.values():
            by_source[cell.truth_source] = by_source.get(cell.truth_source, 0) + 1
            templates.add(cell.template_name)
            cells += 1
        absent += len(case.absent)
        templates.update(case.absent)
    return CorpusSummary(
        cases=len(cases),
        cells=cells,
        absent_cells=absent,
        batched_cases=sum(1 for case in cases if case.batched),
        by_source=by_source,
        templates=tuple(sorted(templates)),
    )


def iter_cases(root: Path, limit: int | None = None) -> Iterator[Case]:
    cases = load_corpus(root)
    yield from cases[:limit] if limit else cases
