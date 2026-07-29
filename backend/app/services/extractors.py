"""The `Extractor` seam: bytes in, per-page text out. **Worker-side only.**

ADR 0005 splits the datasheet pipeline in two. `app.services.document_text` is the
API's half — the queue, the lease, the judgement, `datasheet_fts`. This module is
the other half, and **nothing reachable from `app.main` imports it.** That is not a
tidiness claim, it is the property the ADR is about: the API image is rebuilt in CI
on every push and the deployment is one replica on a small RWO volume, so the
process that streams a PDF must not install a parser, let alone Docling's torch and
transformers.

`tests/integration/test_extraction.py` asserts the separation by importing
`app.main` in a subprocess and checking that `pypdf` never reached `sys.modules`.
A convention nobody can verify would have been repaired by the first person who
found it convenient to call an extractor from a route.

## The Protocol returns pages, not a blob plus a count

`ExtractedText` carries `pages`, and `chars_per_page` is derived from it. The
obvious alternative — text plus a separately reported per-page character count —
admits a state that cannot be reconciled: a count that disagrees with the text.
Since `extracted-chars-per-page ≈ 0` is simultaneously the OCR escalation signal
and the low-confidence flag, a wrong count is not a cosmetic bug, it is a scanned
datasheet stored as extracted, confident and empty, with nothing downstream able to
notice. Pages on the wire make the count a function of the text at every hop:
here, over HTTP, and in `document_text.record_text`, which recounts rather than
trusting anybody.

## Two implementations, and why only one of them is tested in CI

* `PyPdfExtractor` — pure Python, no models, no weights, the default and what CI
  runs. `pypdf` lives in the `datasheets` extra, so it is absent from the API
  install and imported lazily even here.
* `DoclingExtractor` — the TableFormer path `docs/PLAN.md` wants for multi-column
  electronics tables. **Deliberately not installed anywhere in this repo**, and
  its one test is `@pytest.mark.live`, per `PLAN.md`'s own rule for heavy paths
  ("a fixture-replaying fake plus one `@pytest.mark.live` test skipped by
  default"). It is written out rather than left as a comment because the point of
  the Protocol is that a heavier extractor is a drop-in, and a seam nobody has
  tried to write through is a seam that turns out not to fit.

Both are constructed through `build_extractor`, so the worker's `--extractor` flag
is a name and not an import path.
"""

from __future__ import annotations

import importlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class ExtractorUnavailable(RuntimeError):
    """The named extractor's dependencies are not installed in this process.

    Distinct from an extraction *failure*: nothing was wrong with the document, so
    the worker must not report a per-document failure and burn its attempts. It is
    a deployment error, and the right response is to fix the image and start again.
    """


@dataclass(frozen=True)
class ExtractedText:
    """One document's text, one string per page.

    Pages are kept apart all the way to the submit door because the per-page
    character count is the escalation signal, and a single joined string cannot be
    counted per page after the fact without guessing at the separator.
    """

    pages: tuple[str, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def chars_per_page(self) -> tuple[int, ...]:
        """Characters found on each page. Zeros are the interesting values: a run
        of them is a scanned-image datasheet, which is the OCR escalation signal
        and the low-confidence flag at once."""
        return tuple(len(page) for page in self.pages)

    @property
    def char_count(self) -> int:
        return sum(self.chars_per_page)


class Extractor(Protocol):
    """What a text extractor has to look like. Bytes in, pages out.

    Bytes rather than a path, because the worker fetches the blob over HTTP (ADR
    0005 refuses it direct database *or* filesystem access) and a temp file would be
    a second thing to clean up after a crash.

    The whole abstraction, exactly as `LabelBackend` and
    `enrichment.extract.ExtractionProvider` are: pypdf, Docling, a tesseract pass
    and a fake all satisfy it, and `app.scripts.extract_datasheets` — their only
    caller — changes for none of them.
    """

    #: Recorded in `documents.extractor`, which is how "re-read everything the
    #: cheap extractor did" becomes a query rather than a guess.
    name: str

    def extract(self, data: bytes) -> ExtractedText: ...


class PyPdfExtractor:
    """The default: `pypdf.PdfReader.extract_text`, page by page.

    Good enough for the many datasheets that carry a real text layer, and it is
    what CI runs — no models, no weights, no network, one pure-Python wheel in the
    `datasheets` extra.

    The import is **lazy**, so this module can be imported (and type-checked, and
    `--help`ed) without the extra present, and a missing dependency surfaces as one
    actionable sentence instead of a traceback at process start.

    `extract_text()` is called with no arguments on purpose: pypdf's layout modes
    reorder text to preserve visual columns, which reads better to a human and is
    strictly worse for a search index, where an FTS5 token is a token wherever it
    sat on the page. The layout work would cost time per page to buy nothing this
    index can use.
    """

    name = "pypdf"

    def extract(self, data: bytes) -> ExtractedText:
        try:
            pypdf = importlib.import_module("pypdf")
        except ImportError as error:  # pragma: no cover - exercised by the worker image
            raise ExtractorUnavailable(
                "pypdf is not installed; the extraction worker needs the 'datasheets' "
                "extra (uv sync --extra datasheets). The API deliberately does not "
                "have it — see docs/adr/0005-extraction-runs-outside-the-api.md."
            ) from error

        reader = pypdf.PdfReader(io.BytesIO(data))
        # `or ""`: pypdf returns None for a page with no content stream, and a page
        # that yielded nothing must count as zero characters rather than crash the
        # run — a document of such pages is exactly the scanned sheet the
        # low-confidence flag is for.
        return ExtractedText(pages=tuple(str(page.extract_text() or "") for page in reader.pages))


class DoclingExtractor:
    """`docs/PLAN.md`'s TableFormer path. **Never installed by this repo.**

    Docling depends on torch and transformers and downloads model weights — 2–5 GB
    — which is the entire reason ADR 0005 moved extraction out of the API process.
    So it is absent from `pyproject.toml`'s `datasheets` extra by design, and that
    extra's own comment forbids adding it: a heavier extractor belongs in the worker
    image's requirements, where the API's CI build never sees it.

    Unverified in CI, and honest about it: the only test is
    `@pytest.mark.live`-marked and skipped by default, which is the rule
    `docs/PLAN.md` sets for every heavy path. What *is* verified without the
    package is that this class satisfies `Extractor` — mypy checks the assignment
    in `_BUILDERS` — so the seam is known to be the right shape even where the
    implementation cannot be exercised.

    `import_module` rather than a real `import` for a specific reason: Docling ships
    no stubs this repo could install, so a static import would need a
    `type: ignore` that then hides genuine mistakes in these few lines forever.
    """

    name = "docling"

    def extract(self, data: bytes) -> ExtractedText:
        try:
            converter_module = importlib.import_module("docling.document_converter")
        except ImportError as error:
            raise ExtractorUnavailable(
                "docling is not installed, and this repo never installs it: it pulls "
                "torch + transformers and model weights (2-5 GB). Build the worker "
                "image with it and run the worker there — see "
                "docs/adr/0005-extraction-runs-outside-the-api.md."
            ) from error

        # Typed as Any because the module is loaded dynamically. Kept to three
        # lines for that reason — anything more elaborate would be unchecked code
        # that no test in this repo runs.
        converter: Any = converter_module.DocumentConverter()
        source: Any = importlib.import_module("docling.datamodel.base_models").DocumentStream(
            name="document.pdf", stream=io.BytesIO(data)
        )
        document: Any = converter.convert(source).document
        pages = [str(document.export_to_markdown(page_no=number)) for number in document.pages]
        return ExtractedText(pages=tuple(pages))


#: Name -> constructor. A registry so the worker's `--extractor` flag is a value
#: rather than an import path, and so adding the OCR fallback `docs/PLAN.md`
#: specifies (pdfplumber + tesseract, once chars-per-page comes back ~0) is one
#: entry here plus one class.
#:
#: mypy checks each value against `Callable[[], Extractor]`, which is what proves
#: both classes above satisfy the Protocol without either being instantiated.
_BUILDERS: dict[str, Callable[[], Extractor]] = {
    PyPdfExtractor.name: PyPdfExtractor,
    DoclingExtractor.name: DoclingExtractor,
}

#: What the worker uses when nobody says otherwise: the one with no model in it.
DEFAULT_EXTRACTOR = PyPdfExtractor.name


def extractor_names() -> tuple[str, ...]:
    return tuple(sorted(_BUILDERS))


def build_extractor(name: str = DEFAULT_EXTRACTOR) -> Extractor:
    """Construct an extractor by name, or say which names exist.

    Construction never imports the heavy dependency — that happens on the first
    `extract` — so a worker started with a name it cannot satisfy fails on its first
    document rather than at startup. That is the right way round: the failure then
    carries the document it was working on, and `--help` works in an image that has
    no parser at all.
    """
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ExtractorUnavailable(
            f"no extractor named {name!r}; available: {', '.join(extractor_names())}"
        )
    return builder()


__all__ = [
    "DEFAULT_EXTRACTOR",
    "DoclingExtractor",
    "ExtractedText",
    "Extractor",
    "ExtractorUnavailable",
    "PyPdfExtractor",
    "build_extractor",
    "extractor_names",
]
