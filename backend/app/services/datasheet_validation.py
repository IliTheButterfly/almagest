"""Is this fetched thing actually this part's datasheet? (ADR 0017)

The check that makes ADR 0017's central rule enforceable rather than aspirational.
The researcher may propose a URL from anywhere — a distributor API, a manufacturer
URL pattern, a web search, a model's own suggestion — and **none of those
proposals is believed**. Each is fetched and put through this module, and only what
survives becomes a `documents` row.

Nothing here runs in the API. It lives in `app/services/` beside `extractors.py`
for the same reason that module does: it is shared code the *worker* imports, and
the worker ships from a different image with the `datasheets` extra installed. ADR
0005's rule is that the API process never opens a PDF, and it still does not.

## The four checks, in the order they run, cheapest first

1. **Magic bytes.** `%PDF-` at the head. Content type is checked too but is not
   trusted alone: manufacturer CDNs mislabel routinely, and — the case that
   actually matters — a login wall or a "file not found" page returns
   `200 text/html` with a perfectly ordinary body. A content-type-only check calls
   that a datasheet.
2. **Size.** Above the ceiling and it is refused before a parser sees it.
3. **It parses.** Truncated, encrypted and malformed PDFs are all real, and a
   parser exception here is a verdict rather than a crash.
4. **The part number is in the text.** The load-bearing one.

## Why check 4 is the one that matters

Checks 1-3 establish that a PDF exists and can be read. They say nothing about
*whose* datasheet it is, and that is the failure this whole design is built
around: a model asked for a URL returns a plausible one, a search engine returns
the first vaguely relevant hit, and a distributor API returns the family sheet for
the wrong series. All three produce a real, parseable, entirely genuine PDF for
some *other* part.

Comparing the normalised MPN against the normalised text is arithmetic. It is not
a judgement, it has no confidence score, and it cannot be talked into agreeing.
That is precisely why it is the gate: **everything upstream of it is allowed to
guess, because nothing downstream of it is.**

## Normalisation is deliberately the catalogue's, not a new one

`normalize_mpn` from `app.services.scanning.codes` — the single definition of what
`parts.mpn_norm` contains — is applied to the *document text* as well as to the
part number. That is what makes `GRM188R71H104KA93D` match a datasheet that prints
it as `GRM188 R71H 104K A93D`, and it is why the worker is handed `mpn_norm` by the
claim rather than deriving its own: two normalisers is one too many, and the
failure mode of a second one is silent — every candidate rejected as `mpn_absent`,
which reads as "no datasheet exists" rather than "the comparison is broken".

## What this module does NOT do

It does not fetch. It takes bytes, so every test is offline and the one thing that
touches the network lives in the worker where it can be seen. It does not decide
what to do with a verdict either — `RESOLVED` versus `EXHAUSTED` is
`app.services.research.record_result`'s job, derived from the whole candidate list
rather than from any single document.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.extractors import ExtractedText, Extractor, ExtractorUnavailable
from app.services.research import RejectReason
from app.services.scanning.codes import normalize_mpn

#: The head of every PDF. Five bytes, and the whole reason a login-wall HTML page
#: cannot pass as a datasheet.
PDF_MAGIC = b"%PDF-"

#: Largest candidate accepted, in bytes. Datasheets run to a few megabytes; a
#: 400-page databook is the fat tail and fits comfortably. The ceiling exists
#: because the worker holds the body in memory and a mislinked ISO image is a
#: perfectly ordinary thing to find behind a URL.
MAX_DATASHEET_BYTES = 64 * 1024 * 1024

#: Content types that may carry a PDF. Advisory only — the magic bytes decide, and
#: a server that says `application/octet-stream` for a real PDF is common enough
#: that refusing on the header alone would reject good documents.
PDF_CONTENT_TYPES = frozenset(
    {"application/pdf", "application/x-pdf", "application/octet-stream", "binary/octet-stream"}
)


@dataclass(frozen=True)
class Verdict:
    """What became of one fetched candidate.

    `text` is carried on an accepted verdict so the worker can hand it straight to
    the extraction submit door instead of parsing the same bytes twice — the
    document is going into the extraction queue anyway, and it has just been read.
    """

    accepted: bool
    #: One of `RejectReason`, or None when accepted.
    reason: str | None = None
    #: Human detail — the byte size that blew the ceiling, the parser's message.
    #: For a person reading the candidate list; nothing branches on it.
    note: str | None = None
    text: ExtractedText | None = None


def looks_like_pdf(data: bytes) -> bool:
    """Magic bytes only. Deliberately not a parse — this is the cheap gate.

    Some real PDFs carry leading whitespace or a UTF-8 BOM before the header, which
    strict readers accept, so a small prefix is searched rather than requiring the
    signature at offset zero. Small: a `%PDF-` appearing a kilobyte into an HTML
    page is an HTML page that mentions PDFs.
    """
    return PDF_MAGIC in data[:1024]


def mpn_in_text(text: ExtractedText, *, mpn_norm: str) -> bool:
    """Does the catalogue's normalised part number appear in the document?

    Both sides go through `normalize_mpn`, so the datasheet's own typography —
    spaces, hyphens and case, which manufacturers vary freely within one document —
    cannot cause a miss. Pages are normalised individually and then joined, so a
    part number is never manufactured by two pages abutting.
    """
    if not mpn_norm:
        return False
    return any(mpn_norm in normalize_mpn(page) for page in text.pages)


def validate(
    data: bytes,
    *,
    mpn_norm: str,
    extractor: Extractor,
    content_type: str | None = None,
    max_bytes: int = MAX_DATASHEET_BYTES,
) -> Verdict:
    """Put one fetched candidate through the four checks. Cheapest first.

    `ExtractorUnavailable` is deliberately **not** caught. A missing parser is a
    deployment error, not a fact about this candidate, and reporting it as
    `parse_failed` would blame every provider in turn for a broken image — and burn
    the part's attempts doing it. Same reasoning, and the same escape, as
    `app.scripts.extract_datasheets.process_one`.
    """
    if len(data) > max_bytes:
        return Verdict(
            False, RejectReason.TOO_LARGE, f"{len(data)} bytes exceeds the {max_bytes} ceiling"
        )

    if not looks_like_pdf(data):
        # The content type is reported in the note rather than used as the reason,
        # because what was actually wrong is that the bytes are not a PDF — and a
        # server claiming `application/pdf` while serving a login page is the case
        # worth being able to see in the candidate list.
        claimed = content_type or "no content-type"
        return Verdict(False, RejectReason.NOT_PDF, f"served {claimed}, body is not a PDF")

    try:
        text = extractor.extract(data)
    except ExtractorUnavailable:
        raise
    except Exception as error:  # a parser's exception types are not enumerable
        return Verdict(False, RejectReason.PARSE_FAILED, f"{type(error).__name__}: {error}")

    if not mpn_in_text(text, mpn_norm=mpn_norm):
        # A real, parseable, entirely genuine datasheet — for something else. This
        # is the verdict that makes a hallucinated URL harmless rather than merely
        # unlikely, and it is why the rejections are stored: several of these in a
        # row is a provider bug, not an obscure part.
        return Verdict(
            False,
            RejectReason.MPN_ABSENT,
            f"{text.page_count} pages read, {mpn_norm!r} not among them",
        )

    return Verdict(True, text=text)
