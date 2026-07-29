# ADR 0005 — Datasheet extraction runs outside the API process

**Status:** accepted
**Date:** 2026-07-29

## Context

Phase 4 stores datasheets and makes them full-text searchable. `PLAN.md`'s
pipeline is:

> fetch → **Docling** (Apache-2.0, TableFormer) → fall back to pdfplumber +
> tesseract only when extracted-chars-per-page ≈ 0 → LLM structured extraction →
> MPN-decoder cross-check → confidence score → review queue below 0.8

Read naively that is a single pipeline, and the obvious implementation is to run
it inside the API when a PDF is uploaded. That would be a mistake, and the reason
is already written down elsewhere.

Docling depends on torch and transformers and downloads model weights — call it
2–5 GB. The API image is built in CI on every push, and the deployment is a
**single replica with `strategy: Recreate`** on a small ReadWriteOnce volume,
because the datastore is SQLite and two writers is corruption. Putting extraction
in that process means:

- CI builds and pushes a multi-gigabyte image to test a route that streams a PDF;
- the one API replica blocks on a CPU-bound table-parse while a phone at a shelf
  waits for a scan to resolve;
- and the GPU note in `CLAUDE.local.md` is violated outright. It says, of the
  co-tenanted GPU host, that **a free unit is a race, not a reservation** — run
  the extraction model as a Job/CronJob that *releases* the device, and keep only
  the small always-on embedding model resident.

That constraint already decided this. An always-on API holding a GPU-adjacent
extraction stack is exactly what it forbids.

## Decision

**Extraction is a separate process. The API owns storage and search; it never
parses a PDF.**

The split:

| Concern | Where |
|---|---|
| Content-addressed blob store, `documents` rows, dedup by hash | API |
| Streaming a PDF to the browser, redirecting a part to its primary datasheet | API |
| `datasheet_fts` population and querying | API |
| Docling / pdfplumber / tesseract / any model | **extraction worker, its own image** |

The API exposes a **work queue and a submit door**: documents needing text, and a
way to hand extracted text back. The worker claims a document, extracts, submits,
and exits — so it holds no device between runs, and can be a CronJob, a manual
CLI run on a laptop, or nothing at all for a while.

**Consequence that makes this safe rather than merely tidy: a document with no
extracted text is a first-class state, not a failure.** The PDF is already stored,
already served, already attached to its part. Only the *full-text search over its
contents* waits. So the whole extraction stack can be absent — never installed,
broken, or out of GPU — and Phase 4's standalone value ("full-text search across
every PDF you own") degrades to "every PDF you own, stored and viewable, with
search over the ones processed so far". Nothing is lost, nothing is blocked, and
no feature flag is needed.

### The extractor is an interface with a lightweight default

`Extractor` is a Protocol returning text plus a per-page character count. Two
implementations ship:

- **`PyPdfExtractor`** — pure-Python, no models, the default. Good enough for the
  many datasheets that carry a real text layer, and it is what CI runs.
- **`DoclingExtractor`** — optional, installed only in the worker image, for the
  multi-column tables `PLAN.md` wants TableFormer for.

`extracted-chars-per-page ≈ 0` remains the escalation signal exactly as specified
— a scanned-image datasheet — and it is also **the low-confidence flag**, so a
document that needed OCR is marked as such rather than silently trusted.

CI therefore installs no models and downloads no weights, and the test suite stays
offline. Per `PLAN.md`'s own testing rule, the heavy path gets a fixture-replaying
fake plus one `@pytest.mark.live` test skipped by default.

### Phase 6 inherits the same shape

Phase 6's LLM structured extraction is another worker stage on the same queue, and
`CLAUDE.local.md`'s Job/CronJob rule applies to it identically. Which means Phase 6
needs no new architecture — only a second extractor and the cross-check.

That ordering matters: **the MPN decoders are the cross-check target, so they come
first.** They are pure functions with no dependencies, they are the most testable
thing in either phase, and they are useful on their own — a decoded Murata GRM part
number fills in a dielectric and a voltage rating with no model and no network.

## Consequences

- One new nullable-ish state to model: a `documents` row whose text has not been
  extracted. Queried as "the work queue", which is a plain index, not a table.
- The worker needs credentials to reach the API, or direct database access. Direct
  access is refused: two SQLite writers is corruption, and that is the one rule the
  whole deployment shape exists to protect. **The worker goes through HTTP.**
- Nothing in Phase 4 requires the GPU host to exist. That is deliberate; the
  standalone value has to survive the model being unavailable, or it is not
  standalone.
- Extraction being retryable and idempotent is now load-bearing, since a worker
  can die mid-run. Keyed on the document's `sha256`, which is already its identity
  — the same trick `client_op_id` plays for movements.

## Supersedes

Nothing. It makes explicit a split `PLAN.md` left implicit and
`CLAUDE.local.md`'s GPU constraint already required.
