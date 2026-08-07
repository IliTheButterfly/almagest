# ADR 0021 — A second reader for the frame the browser already read

**Status:** proposed
**Date:** 2026-08-07

## Context

ADR 0015 settled where pixels are read: in the browser, by `zxing-wasm` for
barcodes and `tesseract.js` for printed lines. That decision stands and this ADR
does not reopen it. What it addresses is the case ADR 0015 named and could not
solve.

A distributor bag with a readable Data Matrix is a solved problem — the symbology
is checksummed and the part number falls out of DI `1P` deterministically. The
unsolved cases are the ones that actually reach the intake queue and stay there:

- **ink and no code.** A bag with the part number printed and no machine-readable
  symbol, or one whose symbol is creased past decoding.
- **a bare part.** A loose component with only a top marking.
- **an OCR near-miss.** The failure that is most annoying because it looks like a
  success: `digikey-label-26.json` in this repository records tesseract reading
  `CFI4JT100K` off a bag whose part number is `CF14JT100K` — a capital I for the
  digit 1. That string is not a part. Researching it finds no datasheet and the
  queue reports `EXHAUSTED`, which reads as "this part is obscure" when the truth
  is "we misread one character".

All three end the same way: a photograph sitting in the intake queue that a person
has to type a part number for. That is precisely the manual data entry
`CLAUDE.md` names as the dominant project risk.

`PLAN.md` anticipated a model here. What it did not settle is how a model reads an
image without acquiring the failure mode that makes models dangerous in this
system: producing a confident, well-formed, wrong part number.

## Decision

**A vision model reads the capture and proposes ranked identity candidates. It
never asserts one, and the schema is what stops it rather than a rule somebody has
to remember.**

### The prohibitions are unrepresentable, not merely forbidden

`vision.schema_for` builds the JSON schema per request, and what is *absent* from
it is the load-bearing part:

- **No `url` property.** Under a constrained decode the model cannot emit a
  datasheet URL at all. This is ADR 0017's rule, enforced by the decoder. A model
  asked for a datasheet URL produces a well-formed, plausible, frequently
  nonexistent one, and the failure is silent because a 404 reads as a network
  problem rather than a fabrication.
- **No `quantity`, `date_code` or `lot_code`.** Those come off the barcode
  deterministically. A second, worse source for a solved problem is not an
  improvement.
- **`source_text` is required and non-empty.** If the model cannot quote the
  characters it read, it did not read them — the same contract
  `ExtractedField.source_text` already enforces, and what a reviewer checks
  instead of taking the model's word.

This is the trick `extract.schema_for` already plays with its `template_name`
enum, applied to a second interface. A rule in a docstring survives until someone
edits the docstring; a property that does not exist in the schema survives until
someone adds it, which is a change a reviewer can see.

### An empty answer is a normal answer

`VisionResult.candidates` may be empty and that is not an error. It settles the
queue entry as `UNIDENTIFIED`, deliberately distinct from `FAILED`, for exactly
the reason `research.py` keeps `EXHAUSTED` apart from a run that broke: **"we
could not tell what this is" is a photograph problem whose fix is another
photograph**, while `FAILED` means something is wrong with the system. Collapsing
them puts two unrelated diagnoses in one bucket and the bucket stops being read.

A model pushed to answer anyway is a model inventing a part number, which is the
one outcome nothing downstream can recover from.

### The barcode anchors the read; the OCR is repaired by it

Both of the browser's readings go into the request, weighted honestly:

- A decoded **barcode** is stronger evidence than anything the model will produce,
  so when one is present the request narrows to a single candidate and the job
  becomes *confirm the manufacturer and package*. This is the common case, and
  saying so matters: the fan-out over several identities is for the minority of
  captures, not the majority.
- **OCR lines** go in labelled as unreliable rather than omitted. They are usually
  nearly right, and nearly right is exactly what a second reader can repair.

### The identity is still a proposal, always

A chosen candidate becomes a **stub part**, which — because `Part.research_state`
defaults to `PENDING` — is the same act as enqueuing it for research. The existing
chain then runs unattended and fills the fields.

What comes out is a part where **every field is populated and every field that was
not corroborated awaits a glance**, and where **the identity awaits a person at any
confidence**. `is_stub` stays true, the intake entry stays `pending`. That
asymmetry is ADR 0017's and it is what makes running any of this unattended
allowable at all.

## Two wire shapes, because the servers genuinely disagree

Text extraction gets one payload for every backend. Multimodal does not.

| | image | schema constraint | endpoint |
|---|---|---|---|
| **vLLM** | `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}` | `response_format: json_schema` + `guided_json` | `/v1/chat/completions` |
| **Ollama** | `images: ["<base64>"]`, **no `data:` prefix** | top-level `format` | `/api/chat` |

**`ollama_native` is the default for Ollama, and the reason is the constraint
rather than the image.** Ollama's compatibility documentation lists
`response_format` as supported without saying whether the `json_schema` variant
is, while `/api/chat`'s `format` field taking a whole JSON schema is documented
plainly. Constrained decoding is not a nicety here — it is the entire mechanism
above — so the path where it is documented to work is the path to use. The image
encoding differs too, and that difference is real: the same documentation shows
`image_url` as a bare string rather than the nested object the OpenAI spec uses.

**This choice is informed, not verified.** It was made from documentation, not
from a server that answered. `tests/integration/test_vision_live.py` is what
settles it, and one of its tests fails if the default is picking wrong. Until that
has run against both servers, treat this table as a claim to check rather than a
fact.

## Consequences

- **The first image-to-model code in the repository**, and it is fenced: the pure
  half (`vision.py`) has no transport, the transport is constructed only by a
  worker, and nothing under `backend/app/api/routes/` gains an image decode, a
  base64 encoder or a `/v1/` call. ADR 0005 holds structurally rather than by
  convention.
- **ADR 0015 is unaffected.** The browser still reads every frame. This is a
  second reader behind a queue, not a replacement, and it does not write
  `capture_regions`.
- **Vision confidence never enters the promotion rules.** Reading characters off
  a photograph and trusting a datasheet's statement of a value are different
  quantities that happen to share a 0..1 range. Mixing them would smuggle photo
  quality into a parameter's trust.
- **`CallStats` now rides on every model result** (`calls.py`), which is how the
  cost of sending a full-resolution frame becomes visible at all. `grab.ts` does
  not downscale, so a phone capture reaches the model whole; whether that is
  affordable is unmeasured, and the prompt-token count is the measurement. If it
  dominates, the resize belongs in the worker and not in the API.
- **A model swap is one per drain, not one per capture.** The stages are already
  separate workers, so staging the pipeline — vision reads, then research needs no
  model at all, then extraction — means exactly one model is resident at a time.
  On a card that is integral and exclusive (ADR 0016), that is not merely tidy, it
  is the difference between one weight load and one per part.
- A benefit worth stating plainly: `finish_reason` is now read, which fixes a
  live misdiagnosis. `max_tokens` truncating a batch produced invalid JSON that
  the extraction provider reported as *"does this model support constrained
  decoding?"* — sending whoever read it to investigate the serving stack when the
  fix was a smaller batch.

## Supersedes

Nothing. Extends ADR 0015 with a second reader for the frames its browser-side
pass cannot resolve, and applies ADR 0017's propose-never-assert rule to the
identity step it did not previously cover.
