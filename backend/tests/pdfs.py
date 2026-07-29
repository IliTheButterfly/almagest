"""Minimal PDF fixtures, assembled byte by byte. **No PDF library is used to build
these**, deliberately.

Two reasons, and the second is the one that matters.

1. `tests/integration/test_documents.py` must not need the `datasheets` extra to
   exercise the *store* — the API has no PDF library and a test suite that needed one
   to upload a file would be asserting a dependency that does not exist. That file
   uses `_pdf()`, twenty bytes of header and trailer, because five bytes of magic is
   all `blobstore` looks at.
2. The extraction tests need real PDFs, and a PDF *written by* the library that
   *reads* it back is a round-trip of one implementation against itself. Building
   the file by hand means `PyPdfExtractor` is tested against the format rather than
   against `pypdf`'s own writer — which matters most for the case this phase turns on:
   `no_text_layer()` has to be a document pypdf genuinely finds no text in, not one
   whose text a writer helpfully declined to add.

Every offset in the cross-reference table is computed from the bytes actually
emitted, so these are valid PDFs rather than files that survive a tolerant parser's
recovery pass. `strict=True` reads them.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Anything drawn on a page, as a PDF content stream. A page whose stream shows no
#: text is what makes a document look scanned to an extractor: the pixels are there,
#: the text layer is not.
_TEXT_STREAM = "BT /F1 12 Tf 72 720 Td ({body}) Tj ET"
_SHAPE_STREAM = "0.5 0.5 0.5 rg 72 600 400 120 re f"

#: `(`, `)` and `\` end or escape a PDF literal string, so they cannot appear raw.
_STRING_ESCAPES = str.maketrans({"\\": r"\\", "(": r"\(", ")": r"\)"})


def with_text(pages: Sequence[str]) -> bytes:
    """A PDF with one text-showing page per string. `extract_text` finds each one."""
    return _build([_TEXT_STREAM.format(body=body.translate(_STRING_ESCAPES)) for body in pages])


def no_text_layer(page_count: int = 2) -> bytes:
    """A PDF whose pages draw a rectangle and contain no text at all.

    The stand-in for a scanned datasheet — the case that must come out **flagged**
    rather than empty-and-trusted. A genuine scan differs only in drawing an image
    instead of a rectangle, and neither carries a text layer, which is the entire
    property under test. Embedding a real JPEG would add bytes and change nothing
    about what any extractor sees.
    """
    return _build([_SHAPE_STREAM] * page_count)


def _build(streams: Sequence[str]) -> bytes:
    """Assemble a one-font, N-page PDF around the given content streams.

    Object numbering: 1 catalog, 2 page tree, 3 font, then a page and a content
    stream per entry. Every `xref` offset is measured off the buffer as it is built,
    which is why these parse under `strict=True` instead of relying on pypdf
    rebuilding a broken table.
    """
    page_ids = [4 + 2 * index for index in range(len(streams))]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids [{kids}] /Count {count} >>".format(
                kids=" ".join(f"{page_id} 0 R" for page_id in page_ids),
                count=len(page_ids),
            ).encode()
        ),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page_id, stream in zip(page_ids, streams, strict=True):
        content_id = page_id + 1
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        body = stream.encode()
        objects[content_id] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body)

    out = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    # Entries are exactly 20 bytes each — the format is fixed-width and a parser is
    # entitled to seek into it by multiplication.
    out += b"0000000000 65535 f \n"
    for number in sorted(objects):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)
