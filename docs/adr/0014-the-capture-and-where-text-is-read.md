# ADR 0014 — The capture, and where text gets read

**Status:** accepted
**Date:** 2026-07-31
**Amends:** [ADR 0005](0005-extraction-runs-outside-the-api.md), which it does not
replace — the split it draws still holds for every PDF.

## Context

Iliana's request, verbatim:

> "I want to add a capture button for the scanner. When a capture is taken, it is
> saved and additional data is extracted, kind of like google lens and text, qr
> codes, bar codes and any other supported forms of data extraction are outlined
> and on clicking it, the value is copied so it can be used for data filling.
> This same workflow can be used to fill in details for the component itself"

The scanner today turns a frame into a payload and throws the frame away. That is
correct for the live loop and wrong for intake, because a distributor reel label
is **two labels**: a DataMatrix carrying an MPN, and printed ink carrying the
manufacturer, the date code, the packaging and very often the real quantity. Only
the first has ever been readable here. The second gets retyped from memory at a
desk hours later, or not at all — and "not at all" is the failure mode this whole
project is shaped to avoid.

The commit that taught the camera to escalate its decode passes
(`d6fcdfb`) left an explicit note on this: the server half — reading text — was
deliberately absent, and belonged in a worker under ADR 0005. **This ADR reverses
that note** and records why, because reversing a written-down decision silently is
how a codebase stops being trustworthy.

## Decision

### 1. A capture is a stored still plus derived regions

Two tables, shaped exactly like `scan_events` next door and for the same reason
its docstring gives: **the bytes are the asset**, so the image is `NOT NULL` and
every derived thing beside it is nullable and additive.

- `captures` — one row per still, pointing at the `documents` blob that holds the
  bytes. Dimensions are stored so an overlay can scale outlines onto any rendered
  size without the API ever decoding a JPEG.
- `capture_regions` — one outline each, as a quad in image pixel space.
- `pending_intakes.capture_id` — nullable, `SET NULL`. This is what makes
  deferring honest: the desk pass gets the photograph, not just the payload.

A capture whose barcodes decoded and whose text was never read is a **normal,
useful row**, not a half-finished one.

### 2. Text is read in the browser, not in the extraction worker

This is the part that amends ADR 0005. Two reasons, neither of which applies to
datasheets:

**The existing contract has no geometry.** `ExtractedText` is pages of plain
strings, deliberately — per-page character counts are the escalation signal for a
PDF. An outline a person can tap needs a box per line. Riding that queue would
mean widening a contract built for a different question, in order to serve a
consumer it was never for.

**Its worker does not exist yet, and ADR 0005 says it is allowed not to.** That
ADR's load-bearing consequence is that the extraction stack may be absent
indefinitely, because for a datasheet only *search over its contents* waits. For a
person standing at a shelf holding a reel, "the text will be readable at some
unspecified future point" is indistinguishable from never.

So this follows the precedent the scanner already set rather than the one the
datasheet pipeline set: `zxing-wasm` decodes in the browser, `images/resize.ts`
downsamples in the browser, and `app.services.blobstore` checks five bytes of
magic and touches no pixels. **The API stores an interpretation; it never performs
one.** ADR 0005's actual prohibition — no torch, no Docling, no model weights, no
CPU-bound parse in the one replica a phone is waiting on — is fully respected.

### 3. Nothing is ever filled in automatically

`CLAUDE.md` and `docs/PLAN.md` both state that an OCR'd or model-read part number
is **never** auto-accepted. The mechanism here is structural rather than a
convention someone has to remember:

- A chip derived from the resolver's parse of a **checksummed** symbology carries
  a target field, because an ECIA data identifier saying `1P` is a rule.
- A chip from an OCR'd line **never carries one**. It can still fill a field — but
  only the field the user pointed at before tapping. The tap is the acceptance,
  and it has to be aimed.

This was not a theoretical concern. The first end-to-end run of the pipeline read
a rendered `RC0805FR-0710KL` as `RCO805FR-0710KL` — letter `O` for digit `0` — at
85% confidence. A high-confidence, plausible, wrong part number is precisely the
documented failure mode, and it appeared immediately.

`CaptureTextStatus` keeps `empty` ("nothing readable in this frame") apart from
`unavailable` ("this device could not load the reader"), for the same reason
`ExtractionState.PENDING` exists: collapsing them would make a phone that could
not run OCR look like a statement about a label nobody checked.

## Consequences

**A ~1.9 MB model enters the repository.** `frontend/public/tessdata/eng.traineddata.gz`
(the `tessdata_fast` LSTM model) is committed. It is not in any npm package, it
never changes, and fetching it at build time would reintroduce the exact network
dependency `decoder.ts` already refused for `zxing-wasm`: *a scanner that only
works when the WAN is up is not a scanner.* The deployment is a LAN behind a
private CA with no promise of internet access.

**~9 MB of wasm does not.** The `tesseract.js-core` runtime is copied out of
`node_modules` at build time by the `almagest-ocr-runtime` plugin in
`vite.config.ts`, so it cannot drift from the installed package and costs nothing
in git. The asm.js fallbacks are skipped — a browser with no WebAssembly cannot
run `zxing-wasm` either, so it has no camera decode to begin with. `dist` grows to
~13 MB; the browser fetches ~3 MB of it, once, lazily, on the first capture.

**OCR is allowed to be absent, and says so.** Failure to load the model or the
core returns `unavailable` rather than throwing. Barcode outlines are already on
screen and useful by then, and a capture that reads its DataMatrix but not its ink
is a good outcome that must not be presented as a failure.

**The worker path stays open.** Nothing here forecloses a server-side pass later:
`POST /api/captures/{id}/regions` already exists to append regions found after the
fact, which is the same door an extraction worker would knock on. What this ADR
rejects is *waiting for that worker before the feature exists at all.*

**The MCP server is given none of it.** All five routes are `Excluded` in
`coverage.py`. Writing a capture means asserting geometry over an image the caller
never saw; reading one hands back exactly the OCR'd text that must not be
auto-accepted. The capture exists so a *person* can tap the value they recognise.
