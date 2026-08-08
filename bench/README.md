# `bench/` — which local model, and what it costs

Answers one question with evidence instead of taste: **which model should read
datasheets and photographs here, and is the big one worth the GPU handover?**

Its own distribution and venv, like `idcodec/` and `mcpserver/`, because
matplotlib, numpy and `kubectl` have no business in the API image. It adds **no
backend routes**, so `mcpserver/coverage.py` is untouched — benchmark results are
files on a laptop, not inventory.

## Read this before running anything

```bash
almagest-bench cluster release
```

That is the command that puts everything back: it scales every model deployment
to zero and un-suspends the reaper CronJob. A run suspends the reaper for its
duration (see `cluster.py` for why that is the least-bad option), and although the
harness restores it through `atexit`, signal handlers and a `finally`, a hard kill
can still leave it suspended. **The card is released before the reaper is
restored, deliberately**, so a stuck suspension costs the co-tenant nothing — it
has nothing left to reap. `almagest-bench cluster status` reports a stale token if
one is lying around.

## Status

Built: `record.py` (the JSONL format and `--resume` keys), `cluster.py` (the swap
protocol and reaper suspension), `corpus.py` (cases and truth).

Built since: the runner (`stages.py`), the CLI (`almagest-bench corpus check |
vision | score | plot`) and the outcome matrix (`plots.py`).

Not built: the extraction sweep and the research sweep. Both are blocked on a
decision nobody has taken — whether extracted datasheet text may be committed —
because a reproducible extraction run needs a `text.txt` per case rather than a
live fetch from a manufacturer CDN. See "Open decisions" in
`docs/HANDOFF-vision-and-bench.md`.

### `score` — what it refuses to tell you

```bash
almagest-bench score ../out/bench/<run>/records.jsonl        # a table
almagest-bench score ../out/bench/<run>/records.jsonl --json # for a script
```

No GPU, no model, no network: it reads the JSONL and re-judges every answer, so a
scoring change costs a re-score and never a re-run. That was always the design and
`metrics.py` was nevertheless unreachable — which meant every number anybody quoted
came off the run command's inline summary, computed *inside* the expensive step and
therefore uncorrectable.

Three refusals are the point of it:

- **No percentage below `MIN_CASES_FOR_RATES` (30).** `plots.refuse_rate_chart`
  already declines to draw a rate at twelve cases; printing "50%" as text instead
  would defeat that by the shortest possible route. You get counts and the floor is
  named. `--json` returns `correct_rate: null` rather than omitting the key, so a
  consumer cannot mistake the refusal for an older version of the command.
- **No ranking two models the noise cannot separate.** The interval bootstraps over
  **cases**, carrying every repeat of a drawn case with it — repeats of one
  photograph are not independent observations, and this model has been measured
  disagreeing with itself on a repeat. At twelve cases nearly nothing clears the
  gate, and reporting *that* is the finding.
- **`fabricated` on its own line, never folded into a wrong total.** A misread is
  repaired by the barcode anchor or a better photograph; an invention is repaired by
  not trusting the model. Those are opposite conclusions.

The verdict comes from `metrics.judge_records`, which `plot` also calls. It used to
live inside `plot`, meaning the table and the picture could disagree about one
file — and a chart disagreeing with the console is exactly how the
ranked-candidate bug was found.

## What the first real run found

Two cases, two models, two repeats, pixels only. Not a benchmark — a shakedown —
and it earned its keep three times over:

- **The same model, at temperature 0, is not stable on a hard case.** `qwen3-vl:8b`
  answered the XBee once and spent 25 431 characters of reasoning without
  answering the next time. The repeat pips in the chart exist because of this.
- **`sort_keys=True` was silently destroying candidate rank.** The vision stage
  put its ranked candidates in a dict keyed by part number; JSON serialisation
  sorted them, and the chart drew the alphabetically-first candidate as the
  model's top answer. Rank now lives in `CaseRecord.ranked`, where sorting cannot
  reach it, and a test pins it.
- **Reasoning cost, not image size, is what breaks a run.** The 4B failed the
  hard case *both* times the same way. Both models read the easy label in a few
  seconds; the ambiguous one costs an order of magnitude more and sometimes
  everything.

**The corpus has two cases and needs about sixty.** They are the right two to
start from — one at each end of the range — but two cases support no ranking and
no chart, and any number drawn from them would be noise with a decimal point.

| case | what it is | `qwen3-vl:8b`, pixels only |
|---|---|---|
| `0001-cf14jt100k` | DigiKey bag, part number printed *and* in a Data Matrix | correct, ~5 s |
| `0002-xb3-24z8um` | Digi XBee module soldered to a PCB, top marking only | reads the right string, glues the adjacent OUI onto it |

The second is worth more than the first. Its label carries an FCC ID, a Canadian
IC number, an OUI and a serial, all formatted more prominently than the part
number — and the first version of the prompt returned the **FCC ID at confidence
0.95**. A corpus of clean distributor bags would never have surfaced that. When
adding cases, weight them toward bare parts and unlabelled bags for exactly this
reason.

## Two sweeps, because they answer different questions

- **Sweep R (research)** — model-independent, no GPU, run once. Only
  `UrlPatternProvider` and `ManualProvider` are implemented, so on an arbitrary
  corpus most parts return `EXHAUSTED` before a model is ever called. This
  measures the ceiling every accuracy number below it sits under, plus the
  validation gate's false-accept rate.
- **Sweep X (extraction)** — the actual model comparison. Committed `text.txt`
  straight into `ExtractionProvider.extract()` → `cross_check.ingest()` →
  `candidates.evaluate()`. No network but the model endpoint.

Without that split, the sweep would spend the night benchmarking manufacturer
CDNs and would hand different models different inputs on different days.

## What the corpus has to carry

Two fields decide whether any of this is evidence, and both are tedious:

- **`absent`** — the fields the datasheet does *not* state. Without it a
  hallucinated value is indistinguishable from a value nobody labelled, and every
  hallucination scores as a missing label rather than as the error it is.
  **Precision is not measurable without this.**
- **`truth_source`** per cell — `barcode` > `distributor` > `human` > `model`. If
  truth came from the family of model being evaluated, every model in that family
  scores its own idiosyncrasies as correct. Recording the source means any chart
  can be redrawn excluding model-derived cells, and **if that exclusion changes
  the ranking, the benchmark has measured only itself.**

`almagest-bench corpus check` prints the warnings that matter before a night is
spent rather than after: too few cases, no `absent` fields, too much model-derived
truth, no batched cases.

## Statistical power, stated up front

Truth cells are **clustered** — the five fields of one part come from one table in
one document and succeed or fail together — so the effective n is the number of
**parts**, not cells. At 60 parts the 95% interval on an F1 is roughly ±10 points,
which means **a 5-point difference between a 4B and an 8B is not resolvable.**

Two honest responses: accept that "no measurable difference, take the cheap one"
is itself a useful result, or build 200+ cases. Either way, bootstrap over
**parts, not cells**, or the error bars come out at about half their true width
and you will report a difference that is not there.

## The metric that outranks accuracy

`wrongly_promoted` — fields written to `parameter_value` whose value disagrees
with truth. Everything else a model gets wrong is recoverable by looking at it: a
bad candidate sits in the review queue with its source line. A wrongly promoted
value is stored as fact and nobody checks it again.

**If one model has higher F1 and a higher wrong-promotion rate, it is the worse
model.** The charts are drawn so that is visible rather than buried.

## Things that will make the numbers lie

- **Temperature 0 is not determinism.** vLLM's continuous batching makes reduction
  order depend on what else is in the batch, and a restart changes CUDA graph
  shapes. The same case on the same model across a swap can score differently.
  The determinism subset measures it, and the reporting rule is: **refuse to
  report any between-model difference smaller than the observed within-model
  variance.**
- **These charts do not predict production intake latency.** Everything bypasses
  the API, uvicorn, the network and the queues. That is the correct trade — it
  isolates the model — but "the 8B does a part in 12 seconds" is not an SLA.
- **A missing slice is not a model's fault.** The card is co-tenanted; a swap can
  land while another namespace holds it, and a slice can be skipped. The manifest
  records that so the charts do not silently compare two models against a third's
  absence.
