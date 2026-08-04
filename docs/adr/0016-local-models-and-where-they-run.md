# ADR 0016 — Local models, which ones, and where they run

**Status:** proposed
**Date:** 2026-08-04

## Context

Two new capabilities want a model: the intake pipeline that turns a capture into
a fully specified part, and the chat surfaces (ADR 0018). The requirement is that
both run on open-source weights, self-hosted, on the co-tenanted RTX 4090 already
in the cluster.

Three constraints already decided most of this and are not negotiable:

- **ADR 0005:** the API never runs a model and never parses a PDF. It owns
  storage, search, and a work queue. Anything heavy is a separate process going
  through HTTP.
- **`CLAUDE.local.md`:** a free GPU unit is a **race, not a reservation**. The
  extraction model runs as a Job/CronJob that *releases* the device. Only a small
  (<2 GB) embedding model may be always-on. There is no ResourceQuota in the
  namespace, so every pod that touches the GPU sets explicit cpu/memory limits.
- **`CLAUDE.local.md`, settled 2026-07-28:** the model family is **Qwen**, because
  it is already running on this node. That is a starting point, not a benchmark
  result.

The tension this ADR has to resolve is that the second constraint was written for
**batch** work. Batch work tolerates a cold start. Chat does not — a 90-second
wait for a first token is not a chat feature, it is a broken one.

## Decision

### Three model roles, three different residency rules

| Role | Model | Serving | Residency |
|---|---|---|---|
| **Research + chat** — tool-calling, ranking, prose | Qwen3-30B-A3B-Instruct, 4-bit | Ollama | Deployment, `OLLAMA_KEEP_ALIVE=5m` — unloads on idle |
| **Extract + summarize** — schema-constrained JSON | Qwen3-8B, 4-bit | vLLM, in the worker Job's pod | Job only. Device released when the Job ends |
| **Embeddings** — semantic recall | Qwen3-Embedding-0.6B | Ollama or TEI | Deployment, always-on. <2 GB, explicitly permitted |

**The MoE choice is the load-bearing one.** Qwen3-30B-A3B activates ~3B
parameters per token, so it gives a 30B-class model's tool-calling and judgement
at roughly an 8B model's decode speed, and it fits 24 GB at 4-bit with context to
spare. That is what makes one model serve both "the decently big model that does
the research" and "the model behind the chat box" without a second set of weights
resident.

**The small model does extraction, and this is not a downgrade.** Structured
extraction is decoded against a JSON schema built per request
(`app/services/enrichment/extract.py:schema_for`), so the grammar — not the model
— is what makes an invented parameter name unrepresentable. What a bigger model
would buy on top of that is reading the *right row* of a multi-column variant
table, and the answer to that is already built and is not a model at all: the
MPN-decoder cross-check (`cross_check.py`). Spending the GPU budget on a 30B
extractor buys less than the deterministic check already delivers.

### `keep_alive` is how chat and the GPU rule coexist

`OLLAMA_KEEP_ALIVE=5m` means the weights load on the first request, stay resident
while a conversation is active, and are **evicted five minutes after the last
one**. Between sessions the deployment holds a pod and a few hundred MB of host
RAM, and holds **no GPU memory at all**.

That satisfies the rule as written — the device is not held between runs — while
making chat pay a cold start only on the first message of a session (roughly
15-30 s for 4-bit 30B off a local NVMe), and nothing thereafter. A Job-per-message
design would pay that cost on every single turn, and a plain always-resident
Deployment would violate the co-tenancy rule outright.

**This is the one place the constraint is read as "release on idle" rather than
"release on completion", and it is deliberate.** If the co-tenant builder starts
losing races for the device, the mitigation is to lower `keep_alive`, not to
redesign — the failure mode is a slower first token, never a wrong answer.

### Batch stages run vLLM inside the worker Job, not against a server

The research and extraction stages are a **two-container Job**: the worker, and a
vLLM sidecar it talks to over localhost. The Job requests `nvidia.com/gpu: 1`,
does its batch, and exits. The device is released by the Job ending, which needs
no lifecycle code and cannot leak a device if the worker crashes.

vLLM rather than Ollama here because batching is the entire point — a 400-variant
family is one continuous-batched run, and `MAX_BATCH_MPNS = 24` per call means
throughput dominates latency completely.

### Everything speaks one OpenAI-compatible interface

Ollama and vLLM both serve `/v1/chat/completions` with `response_format`.
`ExtractionProvider` (already a Protocol) gets one implementation pointed at a
base URL, so which server answers is configuration, not code. `PLAN.md`'s "local
first pass, frontier API as escalation" stays available for free: a frontier
endpoint is the same interface with a different base URL and key, and nothing but
a routing rule changes.

## Consequences

- Two new GPU-touching workloads, both prefixed `almagest-`, both with explicit
  cpu/memory limits alongside `nvidia.com/gpu: 1`.
- The always-on footprint of this decision is one embedding pod under 2 GB. The
  chat model's pod is always-on; its *GPU* usage is not.
- `make check` still installs no models and downloads no weights. Every model
  client ships with a fake, and the real path is a single `@pytest.mark.live`
  contract test skipped by default — the rule the whole codebase already follows.
- **Nothing here changes what is trusted.** A local model is not more trustworthy
  than a hosted one for having been self-hosted. The never-auto-accept rule, the
  MPN cross-check, and the deterministic substitution filter all apply unchanged,
  and are what make a mediocre local model acceptable in the first place.
- Model choice is reversible. The interface is a base URL and a model name; the
  4-bit quantisation and the specific Qwen point release are expected to be tuned
  against real datasheets, not settled here.

## Supersedes

Nothing. It makes concrete the "GPU on this node" section of `PLAN.md` and the
model-serving note in `CLAUDE.local.md`, and resolves the chat-latency case those
did not consider.
