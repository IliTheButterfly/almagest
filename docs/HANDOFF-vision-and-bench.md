# Handoff — the vision path and the benchmark

Written at the end of the session that built them, 2026-08-07. Everything below
is **merged to `main`** unless it says otherwise.

The point of this document is the things a reader cannot recover from the diff:
what was measured, what turned out to be false, and which of the remaining work
is load-bearing.

---

## What exists now

| PR | What |
|---|---|
| #105 | The vision path: `enrichment/vision.py` (pure) + `vision_openai_compat.py` (transport), the proving slice, `CallStats`, ADR 0021 |
| #106 | `bench/` — its own distribution and venv, in `make check` and CI |
| #107 | `app.scripts.upload_capture`, photographs gitignored, the bench runner and chart |
| #108 | Corpus 2 → 12 cases, the `misread` category, scoring made a separate pass |
| #109 | Removed a `use_hints` flag that was declared, documented and never read |

**Cluster:** `sha-d6b8a4ee1af2` on `almagest-api` and `almagest-web`, healthy,
recorded in `deploy/overlays/aether/kustomization.yaml`. Both LLM deployments at
0 replicas, `almagest-llm-reaper` un-suspended.

**Models pulled on the Ollama PVC:** `qwen3:4b`, `qwen3:8b`, `qwen3-vl:4b`,
`qwen3-vl:8b`. The PVC reports the node's disk (1.7 TB free), not the 20 Gi
claim, so headroom is a non-issue — an early worry that turned out to be wrong.

---

## What was measured

`qwen3-vl:8b`, 12 corpus cases, **pixels only**:

**6 correct · 2 misread · 2 distractor · 1 fabricated · 1 reasoning-budget error.**
Median 9 s, ~3 270 prompt tokens.

The score is the least interesting part. Two things matter more:

**Only one failure resembled invention.** Two were single-character misreads
(`PS1440PQ2BT` for `PS1440P02BT`), two were strings genuinely printed on the item
(an FCC ID; a URL slug), one was a reasoning overrun. That is a far more
tractable profile than "it hallucinates", and it points somewhere specific.

**Every figure is the unanchored worst case.** The harness feeds the model
nothing but the image. The deployed pipeline hands it whatever the browser
decoded, and on a bag with a readable Data Matrix that *is* the part number —
which is exactly the repair for the two misreads. See "the anchored variant"
below.

### Deployment facts that contradicted the documentation

Found by running `qwen3-vl:8b`, not by reading:

- **Both multimodal wire shapes work on Ollama.** Its OpenAI-compatible endpoint
  accepts the nested `{"url": "data:..."}` form despite the docs showing only a
  bare string. `ollama_native` remains the default for a *measured* reason: on
  the ambiguous case it completes inside an 8192-token budget and the OpenAI
  path does not.
- **Constrained decoding enforces a field's type, not its bounds.** The model
  answers `95` for a field declared `{"minimum": 0.0, "maximum": 1.0}`,
  reproducibly, through two revisions of the prompt *and* the schema
  description. `vision._confidence` normalises a percentage; the docstring
  argues why that is narrower than it looks. **This is the strongest argument
  for the absent `url` property: a bound is advisory, a missing property is
  not.**
- **The reasoning budget is spent before the answer.** Qwen3-VL thinks, out of
  `max_tokens`. At 1024 the hard case truncated; at 4096 it produced 12 318
  characters of reasoning and *no answer*; 8192 works. `think: false` is not the
  remedy — it returns an empty completion.
- **Temperature 0 is not stability.** The same model answered one case and
  exhausted its budget on the next repeat of it.

---

## Traps, and what they cost

Four bugs this session were found by **running** things. None would have been
caught by reading, and three were in code I had just written and believed.

1. **`json.dumps(..., sort_keys=True)` sorts nested dicts too.** The vision stage
   put ranked candidates in a dict keyed by part number; rank was destroyed on
   serialisation and the chart drew the alphabetically-first candidate as the
   model's answer. Rank now lives in `CaseRecord.ranked`. *Caught by a chart
   disagreeing with the console.*
2. **An idempotency test that checked only the already-idempotent fields.**
   `upload_capture` was idempotent on the blob and the intake entry and not on
   the capture row; the test asserted the first two and passed. Two photographs
   uploaded twice made four captures. *Caught by deploying and running it twice.*
3. **Scoring was baked into the expensive step**, despite a design that said a
   scoring change should cost a re-score. Adding the `misread` category would
   have meant another GPU sweep. `plot` now re-judges from `ranked`.
4. **A parameter that documented behaviour it did not have** (`use_hints`).
   Worse than dead code: the next reader passes `True` and believes the result.

The pattern is the argument for the e2e run and the charts, not just the unit
tests.

---

## What is NOT built

**The unattended path.** There is no dispatch queue, no worker and no UI panel,
so nothing invokes the vision model outside a test and the bench CLI. A capture
parked in the intake queue sits there. This is the biggest remaining chunk and
the design is settled — see ADR 0021 and the plan for the queue shape (a copy of
`research.py`'s lease machinery, six columns on `pending_intakes`, five routes,
five `coverage.py` lines, and `dispatch_captures.py` as the third sibling
worker).

**The extraction and research sweeps.** `bench/` runs the vision stage only.
`metrics.py` exists and is unwired to a `score` command.

**The 30B-A3B VL and the CPU-only embedding pod.** Both were in the original
plan; neither is deployed. The embedding pod must be **CPU-only** — an always-on
GPU pod denies an exclusive card to the co-tenant, which is the thing ADR 0016's
own argument implies and did not say.

**The anchored benchmark variant.** It needs a capture whose regions the browser
filled in, and `upload_capture` deliberately does not decode barcodes or run OCR
(ADR 0015 — a Python approximation would drift from the readers the PWA uses).
The honest way to run it is to scan a label with the PWA and then benchmark that
capture. This is probably the **highest-value single experiment left**, because
it directly tests whether the anchor repairs the misreads.

---

## The corpus is the bottleneck

12 cases across four classes: anchored-with-distractors, rotated/multi-label,
bare component marking, and **no part number printed at all**.

`plots.refuse_rate_chart` raises below 30 cases, so nothing here can be drawn as
a percentage. Roughly 60 cases makes a rate defensible; below that, bootstrap
over *cases* and expect a ±10 point interval.

**When adding cases, weight toward no-part-number items and bare markings.** A
clean distributor bag tells you almost nothing — both models read those in
seconds. The failures concentrate where there is no barcode and no heading.

The no-part-number class is worth defending: without it, *always guessing* is a
winning strategy, because every other case has an answer. `mpn: null` in
`case.json` means "nothing on this item names it" and returning no candidates
scores as correct.

### Adding a case

```bash
# 1. get the photograph onto the server and into the local cache
cd backend && uv run python -m app.scripts.upload_capture \
    --park --cache ../bench/corpus/_captures <files>

# 2. write bench/corpus/NNNN-slug/case.json with mpn (or null),
#    manufacturer, distractors, and the capture_sha256 the upload printed

# 3. check it, then run
cd bench && uv run almagest-bench corpus check
uv run almagest-bench vision --model qwen3-vl:8b --run-id <id>
uv run almagest-bench plot ../out/bench/<id>/records.jsonl out.png
```

Photographs are **never committed** — `.gitignore` covers them and the case
names its image by hash.

---

## Operational notes

**The GPU.** One card, co-tenanted, exclusive. `kubectl -n ili scale
deploy/almagest-llm --replicas=1` to take it, `--replicas=0` to give it back.

**The reaper will take it from you mid-run.** `almagest-llm-reaper` scales the
LLM deployments to zero after 45 idle minutes measured on *chat threads*, and a
benchmark touches none — so it looks idle from the moment it starts. This was
observed live, not theorised. Suspend it for a run:

```bash
kubectl -n ili patch cronjob almagest-llm-reaper -p '{"spec":{"suspend":true}}'
```

**and always release the card before restoring it** — that ordering is what
makes suspension safe, because a stuck-suspended reaper then has nothing to
reap. `bench/almagest_bench/cluster.py` implements this properly with `atexit`
and signal handlers; the manual path above is what I actually used.

**Builds go through windo-lab**, `--local` for this repo (editable path deps on
the submodule libraries, and host-absolute worktree paths break the remote
rsync):

```bash
export WINDO_AGENT="claude-<task>"
windo-lab build run be:almagest-check -p bench --local -- make check
```

---

## Open decisions I deliberately did not take

**The XBee photograph is still in git history** from an early merge. Gitignoring
it since does not remove it, and stripping it means rewriting a merged `main`.
That is the owner's call.

**Two result charts are in the pic-review queue**, unreviewed. The 12-case one
supersedes the 2-case one.

**Whether committing extracted datasheet text is acceptable** — asked in the
original plan, never answered. It matters for the extraction sweep, which wants
a committed `text.txt` per case so a run is reproducible without hitting
manufacturer CDNs.
