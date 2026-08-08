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

**Verified on hardware, 2026-08-07, against `qwen3-vl:8b` on the cluster's
Ollama.** All five live contract tests pass, and the findings are not what the
documentation predicted:

- **Both wire shapes work on Ollama.** Its OpenAI-compatible endpoint does accept
  the nested `{"url": "data:image/jpeg;base64,..."}` form, despite the docs
  showing a bare string. The documentation is more conservative than the server.
- **`ollama_native` is still the right default, for a reason nobody predicted.**
  On the ambiguous corpus case the native path completed within an 8192-token
  budget and the OpenAI path did not. So the default now rests on a measurement
  rather than on a reading of the docs — it just happens to be the same default.
- **Constrained decoding constrains the shape and not the bounds.** The model
  answered `95` for a field declared `{"minimum": 0.0, "maximum": 1.0}`,
  reproducibly, across both transports and across two revisions of the prompt and
  the schema description. This is exactly the "server accepted the schema and
  ignored it" case `parse_response` re-validates for, and it is why the absent
  `url` property is a stronger guarantee than any `maximum` could be: a property
  that does not exist cannot be emitted, whereas a bound is advisory.
- **The reasoning budget is spent before the answer.** Qwen3-VL thinks, and that
  thinking comes out of `max_tokens`. At 1024 the hard case truncated; at 4096 it
  produced 12318 characters of reasoning and **no answer at all**; at 8192 it
  answered. Turning reasoning off is not the remedy — Ollama's `think: false`
  returns an empty completion with nothing in place of the reasoning, which is
  the failure this repository already met once in chat.

## What it actually reads, measured

Two photographs, `qwen3-vl:8b`, no barcode and no OCR hint — the model working
from pixels alone. Deterministic across both transports.

| | anchored distributor bag | bare module on a PCB |
|---|---|---|
| truth | `CF14JT100K` (Stackpole) | `XB3-24Z8UM` (Digi XBee 3) |
| answer | **`CF14JT100K`** | `XB3-24Z8UM 0013A2004` |
| confidence | 0.95 | 0.7, plus a second candidate at 0.6 |
| wall clock | ~5 s | ~39 s |
| prompt tokens | ~3 100 | ~7 700 |

**The easy case is easy and the hard case is the whole point.** On the bag it
picked the value under *"Manufacturer Part Number"* over the DigiKey ordering
code printed directly above it under *"Part Number"* — the exact discrimination
the prompt asks for.

On the bare module, the first prompt returned **`MCQ-XBEE3` — the FCC ID — at
confidence 0.95.** Confidently wrong, from a label that also carries a Canadian
IC number, an OUI and a serial, all formatted more prominently than the part
number. Naming those categories explicitly in the system prompt moved it onto the
right string at a *lower* confidence with a ranked alternative beside it, which
is the behaviour that makes a review queue useful rather than decorative.

Three things follow, and none of them are about this model being good or bad:

- **The `source_text` requirement is what caught the error.** The wrong answer
  quoted `'MODEL: MCQ-XBEE3'`, a line that does not exist — the label says
  `MODEL: MICRO` and `FCC ID: MCQ-XBEE3` on separate lines. A reviewer comparing
  the quote against the photograph sees that immediately. A bare assertion would
  have looked identical to the right answer.
- **Confidence is not calibrated and must not be trusted.** 0.95 on a wrong
  answer, before and after normalisation. This is the empirical case for the
  never-auto-accept rule, and for keeping vision confidence out of the promotion
  arithmetic entirely.
- **The remaining error is segmentation, not comprehension.** `XB3-24Z8UM
  0013A2004` is the part number with the adjacent OUI glued on, from a label
  rotated ninety degrees. That is a different problem from reading the wrong
  field, and it is the one `datasheet_validation`'s MPN-in-text check is placed
  to catch.

A corpus of two is not a benchmark and no ranking should be drawn from it. It is
enough to say the path works end to end against a real model, and to have found
four deployment facts that no amount of reading the documentation would have
produced.

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

## Amendment, 2026-08-08: the transcript, because `source_text` only half-works

`source_text` was placed here so a reviewer could check the characters the model
claims it read against the photograph, and the measurement above shows it earning
that: the wrong answer quoted `MODEL: MCQ-XBEE3`, a line that is not on the label.

Using it in anger exposes what it does not cover. It answers *what the model
said*. The question that follows every wrong reading is *what was it told* — was
the barcode anchor present, did the browser's OCR hand it `CFI4JT100K` and did it
copy the typo, did the reasoning budget run out before the answer began. None of
that is recoverable from a candidate row, and the run above that emitted 12 318
characters of reasoning and never answered leaves **no candidate row at all**,
which makes it precisely the run with the most to learn from and the least
recorded.

So `model_runs` stores the prompt as sent and the completion as returned, per call,
and `GET /api/intake/pending/{id}/activity` stitches one entry's whole story out of
it: capture, dispatch, runs, candidates, the part a person accepted, and that
part's research, extraction and field candidates. **The never-auto-accept rule is
only reviewable if the prompt is reviewable**, and that is the whole argument.

Four constraints came out of building it, each of which is a way it could have gone
wrong:

- **The image is replaced by `{"image_sha256": ...}` before the payload is stored.**
  A base64'd frame is megabytes, the blob already exists in the document store, and
  `model_runs` has no pruning — so a copy would be pure duplication in the one place
  nothing sweeps. The substitution is per wire shape, because the image lives in a
  different field in each and a sanitiser written for one would pass the other
  through silently.
- **A failed call is recorded too.** `ModelUnavailable` carries the sanitised
  request and whatever came back, so a run that broke leaves a transcript rather
  than a one-line message. Where the transport had nothing to report the columns are
  NULL, which is the honest shape.
- **`CallStats`' missing-versus-zero rule survives to the screen.** No column
  defaults to 0 and the UI says "not recorded", because a zero would read as an
  empty prompt and would pull any average over these rows toward whichever servers
  were quiet.
- **Retention is unbounded and no pruning is implemented.** A drain of forty
  photographs writes forty transcripts; the per-row bound is 200 000 characters,
  deliberately loose so the 12 318-character case is not the thing that gets cut.
  The table bound does not exist yet. Stated rather than implied.

The displayed confidence stays the stored, clamped one. Where the transcript's own
number is shown it is labelled as the model's claim about itself, since the two
differ by exactly the clamp this ADR argues for.

## Supersedes

Nothing. Extends ADR 0015 with a second reader for the frames its browser-side
pass cannot resolve, and applies ADR 0017's propose-never-assert rule to the
identity step it did not previously cover.
