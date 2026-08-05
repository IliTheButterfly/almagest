# ADR 0016 — Local models, which ones, and where they run

**Status:** proposed
**Date:** 2026-08-04

## Context

Two new capabilities want a model: the intake pipeline that turns a capture into
a fully specified part, and the chat surfaces (ADR 0018). The requirement is that
both run on open-source weights, self-hosted, on the co-tenanted RTX 4090 already
in the cluster.

Existing constraints:

- **ADR 0005:** the API never runs a model and never parses a PDF. It owns
  storage, search, and a work queue. Anything heavy is a separate process going
  through HTTP.
- **`CLAUDE.local.md`:** a free GPU unit is a **race, not a reservation**. The
  extraction model runs as a Job/CronJob that *releases* the device. Only a small
  (<2 GB) embedding model may be always-on. There is no ResourceQuota in the
  namespace, so every pod that touches the GPU sets explicit cpu/memory limits.
- **`CLAUDE.local.md`, settled 2026-07-28:** the model family is **Qwen**, because
  it is already running on this node.

### What the node actually allows

Measured on 2026-08-04 by scheduling two `pause` pods each requesting
`nvidia.com/gpu: 1`. The first scheduled; the second did not:

```
0/1 nodes are available: 1 Insufficient nvidia.com/gpu.
no new claims to deallocate, preemption: 0/1 nodes are available:
1 No preemption victims found for incoming pod.
```

**There is no time-slicing and no MPS. `nvidia.com/gpu` is capacity 1, integral
and exclusive.** Changing that is device-plugin configuration at cluster scope,
which this namespace's RBAC cannot even read, let alone set.

That measurement is what this ADR turns on, because it means **freeing VRAM does
not free the GPU.** A pod that has released every CUDA allocation still holds the
device from the scheduler's point of view, and the co-tenant is blocked exactly as
hard as if it were running inference flat out. Any design whose courtesy to the
co-tenant consists of releasing VRAM is courtesy the scheduler cannot observe.

### The cold start was mispriced

An earlier draft of this ADR put a Job-per-chat-turn at roughly 90 seconds to
first token and used that to justify an always-running server with an idle
eviction timer. That number conflated two different costs — scheduling and
starting a container, versus loading weights into VRAM — and then treated the sum
as irreducible. It is not. A resumed process that already holds its weights in
host RAM does neither.

**vLLM sleep mode** is the relevant mechanism and it is a first-class feature:

- **Level 1** offloads model weights to host RAM and releases the CUDA
  allocations. Waking is a host-to-device copy over PCIe — on the order of a
  second or two for an 18 GB 4-bit model, not a disk read and not a process start.
- **Level 2** discards the weights entirely. Waking re-reads from disk.

So the trade is not "fast chat versus a well-behaved GPU tenant". It is a
narrower and more honest one: **how long the device allocation is held.**

## Decision

### One pod holds the device, and sleeps between uses

A single `almagest-llm` deployment runs vLLM, holds `nvidia.com/gpu: 1`, and uses
sleep mode to swap between the models it serves. Chat, research and batch
extraction all go to it.

| Role | Model | Resident |
|---|---|---|
| **Research + chat** — tool-calling, ranking, prose | Qwen3-30B-A3B-Instruct, 4-bit | awake on demand, asleep on idle |
| **Extract + summarize** — schema-constrained JSON | Qwen3-8B, 4-bit | awake during a batch run |
| **Embeddings** — semantic recall | Qwen3-Embedding-0.6B | always-on, separate pod, <2 GB |

**Sleep mode earns its place inside this pod rather than between pods.** At 4-bit
the chat model is ~18 GB and the extractor ~5 GB; with KV cache for either, both
resident at once does not fit 24 GB with any comfort. Sleeping one to wake the
other is what lets a single device serve both roles, and it is a
second-scale operation rather than a minute-scale one. That is the real win, and
it is a win the exclusive-allocation finding makes necessary — since two pods
cannot share the card, one pod has to serve both jobs.

**The MoE choice is what makes one model serve two roles.** Qwen3-30B-A3B
activates ~3B parameters per token, giving 30B-class tool-calling and judgement at
roughly an 8B model's decode speed. Research and chat are then the same weights,
not two sets.

**The small model does extraction, and this is not a downgrade.** Structured
extraction is decoded against a JSON schema built per request
(`app/services/enrichment/extract.py:schema_for`), so the grammar — not the model
— makes an invented parameter name unrepresentable. What a bigger extractor would
buy is reading the *right row* of a multi-column variant table, and that is
already handled by something better than a model: the MPN-decoder cross-check
(`cross_check.py`).

### Yielding the device is explicit, not emergent

Since the scheduler cannot see a sleeping pod as idle, the co-tenant gets the card
only when `almagest-llm` is scaled to zero. That is a deliberate act, and this ADR
does not pretend otherwise:

- `octans-gpu-builder` currently sits at **0 replicas**, so there is no live
  contention to arbitrate today.
- Scaling it up is itself a human decision. The rule is that
  **`almagest-llm` scales to zero first**, and the deployment is written to make
  that a one-line operation with no state to drain — every request is stateless,
  and batch work is resumable from the queue by construction.
- Waking after such a yield is a full cold start. That is correct: yielding
  should cost the party that yielded, not the party that needed the device.

**This is a real departure from `CLAUDE.local.md`'s "run it as a Job that releases
the device", and it should be read as one.** That rule assumed releasing VRAM and
releasing the device were the same act. On this node, with capacity 1 and no
time-slicing, they are not — so a Job-per-batch releases the device between runs
but leaves chat with no way to be warm, while a resident sleeping pod keeps chat
usable but holds the claim. The rule's *intent* — do not starve the co-tenant —
is served by the explicit yield above, which is observable and operable, rather
than by a release the scheduler could not act on anyway.

If the co-tenant becomes actively contended, the correct fix is to enable
time-slicing in the device plugin, which makes both designs work. That is
cluster-scoped and out of this namespace's reach; it is the thing to ask the
cluster admin for, and it is the only change that dissolves the trade rather than
picking a side of it.

### Everything speaks one OpenAI-compatible interface

vLLM serves `/v1/chat/completions` with `response_format` / `guided_json`.
`ExtractionProvider` (already a Protocol) gets one implementation pointed at a
base URL, so which server answers is configuration, not code. `PLAN.md`'s "local
first pass, frontier API as escalation" stays available for free: a frontier
endpoint is the same interface with a different base URL and key.

Sleep and wake are driven by a thin gate in front of the server — wake before the
first token, sleep after an idle interval, and never sleep mid-batch. This is a
small amount of code and it is the only genuinely new operational machinery here.

## Consequences

- **One GPU-holding pod, prefixed `almagest-`, with explicit cpu/memory limits
  beside `nvidia.com/gpu: 1`.** Plus one always-on embedding pod under 2 GB.
- **Sleep level 1 parks weights in host RAM — call it ~18-23 GB held
  continuously.** Node memory capacity is not readable from this namespace
  (`get nodes` is Forbidden), and the node also runs the `windo-builder` KubeVirt
  VM. **This must be checked against real headroom before A9 is deployed**; if it
  does not fit, level 2 is the fallback and chat's wake becomes a disk read of a
  few seconds rather than a PCIe copy of one or two.
- Chat first-token latency: ~1-2 s from sleep, sub-second when already awake,
  full cold start only after an explicit yield or a restart.
- `make check` still installs no models and downloads no weights. Every model
  client ships with a fake; the real path is one `@pytest.mark.live` contract test
  skipped by default.
- **Nothing here changes what is trusted.** A local model is not more trustworthy
  for being self-hosted. The never-auto-accept rule, the MPN cross-check, and the
  deterministic substitution filter apply unchanged, and are what make a mediocre
  local model acceptable at all.
- Model choice is reversible — a base URL and a model name. The quantisation and
  the specific Qwen point release are to be tuned against real datasheets.

## Supersedes

Amends `CLAUDE.local.md`'s "run the extraction model as a Job/CronJob that
releases the device" for this node as measured: with `nvidia.com/gpu` at capacity
1 and no time-slicing, releasing VRAM does not release the device, so the
co-tenancy guarantee is provided by an explicit scale-to-zero yield instead. The
<2 GB always-on limit for the embedding model is unchanged and still honoured.
