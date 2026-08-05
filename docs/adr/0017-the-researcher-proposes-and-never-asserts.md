# ADR 0017 — The researcher proposes candidates and never asserts a URL

**Status:** proposed
**Date:** 2026-08-04

## Context

The goal is that a capture taken at the bench becomes a fully specified part a
few minutes later, with datasheet attached and parameters filled. The pipeline
tail already exists and is tested:

```
document -> extraction queue -> text -> ExtractionProvider -> cross_check.ingest
         -> parameter_value_candidate -> review queue -> promotion
```

What does not exist is the **head**: given "the OCR on this capture says something
like `GRM188R71H104KA93D`", find the manufacturer, confirm the identity, and get
the actual PDF into the blob store. `PLAN.md` calls this the provider interface
and the datasheet store; neither the providers nor the acquisition step are built.

This is the step where a model is most useful and most dangerous. Asking a
language model "what is the datasheet URL for this part" produces a well-formed,
plausible, frequently nonexistent URL — and the failure is silent, because a 404
looks like a network problem rather than a fabrication. The same applies one level
up: a model asked to normalise a half-read part number will happily complete it
into a real part that is not the part on the bench.

The codebase already has the correct instinct written down in three places:
enrichment never writes `parameter_value` directly, an OCR'd or model-read part
number is never auto-accepted, and `cross_check.ingest` refuses a variant whose
part number the catalogue does not deterministically match.

## Decision

**The researcher's output is a ranked list of candidates, every one of which was
verified by fetching it. It never returns a fact the pipeline has not
independently confirmed.**

Concretely, three rules.

### 1. A URL is not a result until it has been fetched and validated

The research stage may *propose* URLs from any source — a distributor API, a
manufacturer URL pattern, a web search, or the model's own suggestion. None of
them is stored. Each is fetched and must pass, in order:

- the response is a PDF (content type *and* magic bytes — content type alone
  lies routinely on manufacturer CDNs);
- it is under the size ceiling and actually parses;
- the **normalised MPN appears in the extracted text**.

Only then does it become a `documents` row. The last check is the one that does
the work: it is what distinguishes the right datasheet from a plausible one, and
it is arithmetic on the text, not a judgement.

A candidate that fails validation is recorded as a failed attempt with its
reason, not discarded silently. A part with several validated candidates keeps
them all and ranks them; the human picks the primary, or the auto-promotion rule
picks it when exactly one candidate validates.

### 2. Deterministic sources are tried before the model, always

Ordering is by how falsifiable the source is, not by how convenient:

1. **`jlcparts` local dump** — an offline SQLite dump, zero network, exact MPN
   match. Free and instant.
2. **Manufacturer URL patterns** — a small table of "Murata part numbers live at
   this URL shape". Pure string construction; either the PDF is there or it is not.
3. **Mouser API** — free single key, exact MPN lookup.
4. **Web search** (self-hosted SearxNG, ADR 0016's cluster) — returns pages, not
   answers.
5. **The model** — last, and only for the two jobs it is actually good at.

If step 1 or 2 validates, the model is never called. That is the common case for
passives, and it is also the fastest case, which is the right way round.

### 3. The model's job is ranking and identity-matching, not recall

The two questions the model is asked are bounded and checkable:

- *Given this OCR text and these fetched candidate documents, which one is this
  part, and what is the canonical manufacturer and part number?*
- *Given this half-read marking, which of these catalogue entries could it be?*
  — answered as a ranked list with confidences, never a single value.

Both take the evidence as input and choose among things that exist. Neither asks
the model to produce a fact from memory. The distinction is the whole ADR: a model
choosing between four fetched PDFs can be wrong, and a reviewer can see how; a
model reciting a URL can be wrong in a way nothing downstream can detect.

### The identity it settles is still a proposal

A researched part number lands as a **stub part plus candidates**, exactly as an
OCR'd one does. `is_stub` already exists and already means this. The pipeline can
run end to end unattended and produce a part whose every field is populated and
whose every field is still marked as awaiting a human glance — and the review
screen, which exists, is where those minutes-later results show up.

**Auto-promotion is unchanged and is not relaxed for this path.** A field fills
itself only when it is empty and single-source at confidence >= 0.8, or when
sources agree. In practice a passive with a decoder hit and a matching datasheet
table clears that bar on both fields and needs no clicks; an IC read off a blurry
top mark clears neither and waits. That asymmetry is the feature.

## Consequences

- A new work queue and a new worker stage, both shaped exactly like the
  extraction queue that already exists (claim, lease, submit, idempotent on a
  natural key). No new architecture — ADR 0005 predicted this.
- A new `documents` acquisition path that fetches from the internet. It runs in
  the worker, never the API, so the single API replica never blocks on a slow
  manufacturer CDN.
- Failed research attempts are stored. That is deliberate: "we looked and found
  nothing" must be distinguishable from "nobody has looked yet", or the queue
  retries the same hopeless part forever.
- The provider interface `PLAN.md` specifies for Phase 5 gets built here rather
  than there, because research needs it. `ManualProvider` at priority 0 still wins
  over everything above.
- Egress: the worker needs outbound HTTPS. The API still does not.
- **A part can come out of this pipeline wrong.** The claim is not that it cannot;
  it is that every wrong value is traceable to a fetched document and a recorded
  source line, and that no wrong value reaches `parameter_value` without either
  agreeing with a second source or being clicked by a person.

## Supersedes

Nothing. Extends ADR 0005's worker/queue split to a second stage, and applies the
existing never-auto-accept rule to the acquisition step it did not previously
cover.
