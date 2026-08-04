# Phase 6 — Capture to a defined part, and the two chat surfaces

Build plan. Architecture and the reasoning behind it are in
[ADR 0016](adr/0016-local-models-and-where-they-run.md),
[ADR 0017](adr/0017-the-researcher-proposes-and-never-asserts.md) and
[ADR 0018](adr/0018-chat-threads-writeups-and-export.md). Read those first; this
file is the order of work and nothing else.

## What already exists

More than `CLAUDE.md` admits. Verified against the tree on 2026-08-04:

| Piece | State |
|---|---|
| Captures, regions, OCR, `extract.ts` field suggestions | built |
| Content-addressed blob store, `documents`, `document_links` with roles + primary | built |
| Extraction work queue — `POST /api/extraction/{claims,results,requeue}`, leases | built |
| `Extractor` Protocol, `PyPdfExtractor`, `DoclingExtractor` (uninstalled) | built |
| `ExtractionProvider` Protocol, `schema_for`, `parse_response`, `FakeExtractionProvider` | built, **no real implementation** |
| MPN decoders — Murata, TDK, Samsung, Yageo, EIA, SMD resistor | built |
| `cross_check.ingest` — decoder vs model, conflict clamping | built |
| `parameter_value_candidate`, promotion rules, `/api/enrichment` review queue | built |
| Review screen, intake queue screen, captures screen | built |
| `mcpserver` — curated tool set, `coverage.py` forcing a disposition per route | built |
| Cluster — `almagest-api`, `almagest-web`, nightly backup CronJob, all running | deployed |

**The pipeline tail is done.** What is missing is the head (find and fetch the
datasheet), the worker process that drives both ends, the model serving, and all
of chat.

## What is missing

1. No worker process exists. The extraction queue has never been drained by
   anything but a test.
2. No research stage, no provider interface, no datasheet acquisition.
3. No real `ExtractionProvider` — only the fake.
4. No model serving in the cluster, no GPU workload at all.
5. No chat: no tables, no routes, no service, no UI.

---

## Track A — the intake pipeline

Each chunk is one PR. `make check` green is the gate; every new route needs its
`coverage.py` line in the same PR.

**A1. `enrichmentworker/` scaffold.**
Own distribution (`almagest-enrichment-worker`), own venv, own lock, own
`make worker-check` folded into `make check`. Contains: the API client (HTTP
only — never opens the database), an OpenAI-compatible model client behind a
Protocol with a fixture-replaying fake, the claim/submit loop, and a CLI entry
point. Drains the **existing** extraction queue using `PyPdfExtractor`, so this
chunk alone makes text extraction real with no model and no GPU. Ends with a
worker that can be run by hand against a local API and demonstrably fills
`documents` text.

**A2. The research queue in the API.**
Mirror of the extraction queue, same claim/lease/submit shape, idempotent on a
natural key. New table for research attempts and their outcomes, so "looked and
found nothing" is distinguishable from "not looked at yet". Routes:
`POST /api/research/claims`, `POST /api/research/results`,
`GET /api/research/status`. `Excluded` in `coverage.py` — an agent has no
business claiming worker leases.

**A3. Datasheet acquisition, with validation.**
The fetch-and-validate path from ADR 0017: PDF magic bytes, size ceiling, parses,
**and the normalised MPN appears in the extracted text**. Lands a validated PDF as
a `documents` row linked to the part with role `datasheet`. Refuses and records
everything else. This is the single most important chunk in Track A — it is what
makes a hallucinated URL harmless.

**A4. The provider interface and the deterministic providers.**
`PLAN.md`'s Phase 5 interface, built here because research needs it.
`ManualProvider` (priority 0), `JlcpartsProvider` (offline SQLite dump),
manufacturer URL-pattern table, `MouserProvider` (free key, `provider_cache` with
90-day TTL). Each gets a `Fake*Provider` replaying a recorded fixture plus one
`@pytest.mark.live` contract test skipped by default. **After A4 a Murata passive
resolves end to end with no model involved at all** — that is the checkpoint worth
hitting before any GPU work.

**A5. Web search as a last-resort provider.**
SearxNG in-cluster (no API key, no per-query cost), behind the same provider
interface. Returns pages to fetch and validate, never answers.

**A6. The real `ExtractionProvider`.**
One implementation against an OpenAI-compatible base URL using
`response_format`/`guided_json` with `schema_for()`. `parse_response` and
`cross_check.ingest` are untouched — this is the drop-in the Protocol was written
for. Live-marked contract test.

**A7. The research stage in the worker.**
Ordered provider cascade from ADR 0017, model called last and only to rank fetched
candidates and match identity. Emits a stub part plus candidates, never a
promotion.

**A8. Docling in the worker image.**
Optional extra, worker image only. The `chars-per-page ~ 0` escalation ladder is
already specified and already flags low confidence; this chunk makes the escalation
land somewhere.

**A9. Cluster manifests.**
`almagest-llm` (Ollama Deployment, `OLLAMA_KEEP_ALIVE=5m`, explicit cpu/memory
limits beside `nvidia.com/gpu: 1`), `almagest-embed` (always-on, <2 GB),
`almagest-enrichment` (CronJob: worker + vLLM sidecar, exits and releases the
device). Every name `almagest-` prefixed. No `--prune`, ever.

**A10. Auto-enqueue from a capture.**
Committing a capture's suggested MPN enqueues research. This is the chunk that
turns the whole track into the feature actually asked for: put a part on the
scanner, come back in a few minutes, review a fully populated part.

## Track B — chat

Independent of Track A except for A9's model server. Can start in parallel.

**B1. Schema.** `chat_threads`, `chat_messages`, `chat_writeups`,
`chat_writeup_posts`. One migration. `StrEnumType`, no `CHECK`, no `sa.Enum`.
FTS over writeups.

**B2. API routes.** Thread CRUD scoped by `kind`, message append, writeup create
and post. `coverage.py` lines — mostly `Excluded`.

**B3. `chatagent/` service.** Agent loop, MCP client against `mcpserver` with
writes off, model client from ADR 0016, SSE streaming, persistence via the API.
Fake model client and fake MCP transport so CI stays offline.

**B4. PWA — search chat.** `/chat` tab, thread list, streaming transcript, tool
calls visible rather than hidden. A tool call the user cannot see is a fact they
cannot check.

**B5. PWA — project chat.** Tab on `ProjectScreen`, its own history list, project
context (BOM, builds, allocations) in the system prompt.

**B6. Writeups.** The `create_writeup` tool, send to an existing thread or a new
project thread, writeup view. Creating a *project* stays a confirmed UI action.

**B7. Export.** `?format=md|json`, `include=context` appending resolved facts as
tables. Pure API read.

**B8. Semantic recall.** Embeddings over part descriptions and datasheet text,
brute-force cosine in numpy — no vector database at a few thousand parts. Feeds
chat retrieval and `PLAN.md`'s fuzzy front door.

---

## Invariants this phase must not break

Each has a test that fails loudly, or should get one.

- **Never auto-accept a model-read part number.** Research output is a stub plus
  candidates. Promotion rules are unchanged and are not relaxed for this path.
- **Substitution stays deterministic.** The model ranks and phrases; the SQL
  filter decides. Chat gets no exception.
- **The API runs no model and parses no PDF** (ADR 0005). Both new services go
  through HTTP; neither opens the database.
- **The GPU is released.** Batch work is a Job that exits; the chat model unloads
  on idle; only the <2 GB embedding model is resident.
- **Every new route gets a `coverage.py` disposition.** `Excluded` is usually the
  right answer.
- No `CHECK` constraints, no `sa.Enum`, `UtcDateTime`, `*_milli` / `*_micro`.
- Migrations must not import from `app`.

## Open questions, with the assumption being built to

Flagged rather than blocking. Each is cheap to revisit; none blocks A1-A4.

1. **Chat GPU residency.** Assumed: Ollama with `OLLAMA_KEEP_ALIVE=5m`, so chat
   pays ~15-30 s on the first message of a session and nothing after. The
   alternative is a Job per message (unusable latency) or an always-resident
   server (breaks co-tenancy). If the builder starts losing races for the device,
   lower `keep_alive` — the failure mode is a slower first token, never a wrong
   answer.
2. **Outbound egress for research.** Assumed: the worker gets outbound HTTPS and
   an in-cluster SearxNG. If egress is not acceptable, A1-A4 still deliver the
   whole pipeline over the offline `jlcparts` dump plus Mouser, and A5 drops out.
3. **Can chat write?** Assumed: no. Read tools plus writeup creation; parameter
   fills go to the review queue, stock movements are out entirely. Relaxing this
   later is a config change (`ALMAGEST_MCP_ALLOW_WRITES`), not a redesign.
4. **Model point releases and quantisation.** Assumed Qwen3-30B-A3B 4-bit for
   research and chat, Qwen3-8B 4-bit for extraction, Qwen3-Embedding-0.6B. To be
   tuned against real datasheets, not settled on paper.
