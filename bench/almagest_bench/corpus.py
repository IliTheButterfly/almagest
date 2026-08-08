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
      case.json     identity, expected cells, absent fields, truth sources,
                    and the sha256 of the datasheet text
    bench/corpus/_text/<sha256>.txt      the datasheet text — gitignored
    bench/corpus/_captures/<sha256>.jpg  the photograph — gitignored
    bench/corpus/_pdfs/<sha256>.pdf      the PDF it came out of — gitignored

**Nothing a manufacturer wrote is committed.** Settled 2026-08-08, and this
paragraph is a correction: an earlier version of this module said `text.txt` *is*
committed, and carried a `Case.text_path` pointing beside `case.json` to match. It
was never populated, and the arrangement was refused before it could be — on the
same ground as the PDFs. Redistribution rights are not ours to assume, and the XBee
photograph still in this repository's history is the standing reminder of what
"forever" costs.

The reproducibility argument for committing it was real and is not dismissed: a
sweep that re-fetched PDFs would spend the night benchmarking manufacturer CDNs and
would hand different models different inputs on different days. What preserves it
instead is the **hash**. `case.json` records `text_sha256`; the text is fetched
once into `_text/` and verified against that hash before anything scores against
it, so two runs on two machines are provably reading the same document.

The cost is stated rather than hidden: **a fresh clone cannot reproduce an
extraction run until it re-fetches the text.** `Case.extractable` is false until it
does, and the sweep skips such a case loudly instead of feeding a model an empty
string and recording every field as missing.
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
    #: The part number, or **None when the item carries no part number at all**.
    #:
    #: A retail box is the case: an Arduino Nano Every says `ATMEGA4809` (the
    #: chip on the board) and a URL, and nowhere states the product's own part
    #: number. The correct answer for such a photograph is **no candidates** --
    #: `VisionResult.candidates` being empty is a first-class answer and settles
    #: a queue entry as UNIDENTIFIED.
    #:
    #: Including these is what stops the corpus rewarding a model for always
    #: guessing. A corpus made only of labelled bags cannot tell an identifier
    #: from a machine that says a plausible part number whatever it is shown.
    mpn: str | None
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

    #: Where the photograph lives when it is **not** beside `case.json`, as a path
    #: from the repository root.
    #:
    #: Exists for one real case: the DigiKey resistor bag is already committed as
    #: `frontend/src/lib/capture/fixtures/digikey-creased-datamatrix.jpg`, and
    #: `test_vision.py` asserts its sha256. A second copy here would be 240 KB of
    #: duplication that can silently diverge from the file those tests pin --
    #: which is exactly the kind of drift a corpus must not have.
    capture: str | None = None
    #: The photograph's sha256 -- **the normal way a case names its image**.
    #:
    #: Photographs are never committed (see `.gitignore`): a capture is a picture
    #: of somebody's own bench and a git history is forever. So the case records
    #: the hash and the bytes live somewhere git does not see -- the local cache
    #: below, or a running Almagest, which is what
    #: `app.scripts.upload_capture` puts them in.
    #:
    #: The hash is also what makes that safe. An image fetched from anywhere is
    #: verifiable against the case, so a corpus cannot silently start scoring a
    #: different photograph than the one its truth was written for.
    capture_sha256: str | None = None

    #: The sha256 of this case's extracted datasheet **text**, for the extraction
    #: sweep. Same arrangement as the capture and for the same two reasons: the text
    #: is not ours to redistribute, and a git history is forever.
    #:
    #: **Settled 2026-08-08.** The alternative — a committed `text.txt` per case —
    #: would make a bare clone reproduce a run with no network at all, which is what
    #: `docs/HANDOFF-vision-and-bench.md` said the sweep wanted. It was refused on
    #: the same ground as the PDFs: redistribution rights are not ours to assume,
    #: and the XBee photograph still sitting in this repository's history is the
    #: standing reminder of what "forever" costs.
    #:
    #: The price is real and is not hidden: **a fresh clone cannot reproduce an
    #: extraction run until it re-fetches the text.** The hash is what keeps that
    #: honest — text fetched from anywhere is verified against the case before it is
    #: scored, so a corpus cannot quietly start measuring a different document than
    #: the one its truth was written for.
    text_sha256: str | None = None

    @property
    def text_path(self) -> Path | None:
        """The extracted datasheet text, if this machine has it. `None` is normal.

        One source only, unlike `capture_path`: the gitignored cache, keyed by hash.
        There is deliberately no "committed beside `case.json`" fallback, because
        that is precisely the arrangement the decision above rejected — offering it
        as a fallback would let it happen by accident.
        """
        if not self.text_sha256:
            return None
        cached = self.directory.parent / "_text" / f"{self.text_sha256}.txt"
        return cached if cached.exists() else None

    def verify_text(self) -> bool:
        """Is the text on disk the one this case's truth was written against?

        `True` when there is nothing to check, matching `verify_capture`:
        "unverifiable" and "wrong" want different handling and only the second
        should stop a run.
        """
        from hashlib import sha256

        path = self.text_path
        if path is None or not self.text_sha256:
            return True
        return sha256(path.read_bytes()).hexdigest() == self.text_sha256

    @property
    def extractable(self) -> bool:
        """Can the extraction sweep run this case at all?

        Truth to score against **and** text to score from. A case with truth and no
        text is not an error — it is the ordinary state of this corpus today — but it
        must be skipped loudly rather than counted as a model failure, which is what
        would happen if the sweep fed an empty string to a model and marked every
        field missing.
        """
        return bool(self.expected or self.absent) and self.text_path is not None

    @property
    def capture_path(self) -> Path | None:
        """The photograph, if this machine has it. `None` is a normal answer.

        Checked in order of how specific each source is:

        1. an explicit `capture` path, for an image that is legitimately in the
           repository already (the DigiKey bag is, as a frontend test fixture);
        2. the gitignored cache, keyed by hash -- where `upload_capture --cache`
           and `almagest-bench corpus fetch` put things;
        3. `capture.jpg` beside `case.json`, which git ignores but a person may
           well have dropped there by hand.
        """
        if self.capture:
            # `resolve()` first, and it is load-bearing rather than tidiness: the
            # case directory is often reached by a relative path (`corpus/0001-x`),
            # which has fewer than three parents and raises IndexError. Found by
            # running it, not by reading it.
            #
            # Three parents up from `<root>/bench/corpus/<case>` is `<root>`.
            path = self.directory.resolve().parents[2] / self.capture
            if path.exists():
                return path
        if self.capture_sha256:
            for suffix in (".jpg", ".png"):
                cached = self.directory.parent / "_captures" / f"{self.capture_sha256}{suffix}"
                if cached.exists():
                    return cached
        local = self.directory / "capture.jpg"
        return local if local.exists() else None

    def verify_capture(self) -> bool:
        """Is the image on disk the one this case's truth was written against?

        `True` when there is nothing to check -- no recorded hash, or no image
        present -- because "unverifiable" and "wrong" want different handling and
        only the second should stop a run.
        """
        from hashlib import sha256

        path = self.capture_path
        if path is None or not self.capture_sha256:
            return True
        return sha256(path.read_bytes()).hexdigest() == self.capture_sha256

    @property
    def unidentifiable(self) -> bool:
        """Is the right answer "I cannot name this"?"""
        return self.mpn is None

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
        """The datasheet text this case's truth was written against.

        Raises rather than returning `""`. An empty string would run the model on
        nothing and record every field as missing, which reads in a chart as a model
        that declined to answer — the most expensive possible way to be wrong about
        one's own harness. Callers check `extractable` first.

        The hash is verified here rather than trusted, because the cache is a
        gitignored directory anybody may have dropped a file into and a corpus
        scoring the wrong document is worse than one that will not run.
        """
        path = self.text_path
        if path is None:
            raise CorpusError(
                f"{self.case_id}: no datasheet text on this machine. "
                f"text_sha256={self.text_sha256!r}; fetch it into bench/corpus/_text/ "
                "(it is gitignored — see the module docstring)."
            )
        if not self.verify_text():
            raise CorpusError(
                f"{self.case_id}: the cached text does not hash to {self.text_sha256!r}. "
                "This case's truth was written against a different document."
            )
        return path.read_text(encoding="utf-8")

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
    if mpn is None:
        # Explicit null means "nothing on this item names it". Distinct from a
        # missing key, which is a case somebody forgot to finish.
        if "mpn" not in body:
            raise CorpusError(
                f"{directory.name}: case.json needs an mpn, or an explicit null "
                "if the item carries no part number"
            )
    elif not isinstance(mpn, str) or not mpn.strip():
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
        mpn=mpn.strip() if isinstance(mpn, str) else None,
        manufacturer=body.get("manufacturer"),
        expected=expected,
        absent=absent,
        datasheet_url=body.get("datasheet_url"),
        document_sha256=body.get("document_sha256"),
        sibling_mpns=tuple(body.get("sibling_mpns") or ()),
        distractors=tuple(body.get("distractors") or ()),
        capture=body.get("capture"),
        capture_sha256=body.get("capture_sha256"),
        text_sha256=body.get("text_sha256"),
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
    #: Cases with truth to score but **no datasheet text on this machine**. Not an
    #: error — it is the ordinary state of a fresh clone, since the text is
    #: gitignored — but it is the extraction sweep's denominator, so it is counted
    #: here rather than discovered when the sweep silently runs over nothing.
    extractable_cases: int = 0

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
        if self.cells and not self.extractable_cases:
            out.append(
                "No case has datasheet text on this machine, so the extraction sweep "
                "would run over zero cases. The text is gitignored by design (see "
                "`corpus.py`); fetch it into `bench/corpus/_text/` and record each "
                "`text_sha256` in `case.json`."
            )
        elif self.extractable_cases < self.cases:
            out.append(
                f"{self.extractable_cases} of {self.cases} cases have datasheet text. "
                "The extraction sweep will skip the rest, so its denominator is not the "
                "corpus size — do not quote an extraction rate against the case count."
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
        extractable_cases=sum(1 for case in cases if case.extractable),
    )


def iter_cases(root: Path, limit: int | None = None) -> Iterator[Case]:
    cases = load_corpus(root)
    yield from cases[:limit] if limit else cases
