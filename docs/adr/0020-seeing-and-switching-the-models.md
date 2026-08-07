# ADR 0019 — Seeing which models are running, and switching them from the app

**Status:** accepted
**Date:** 2026-08-06
**Amends:** [0016 — local models and where they run](0016-local-models-and-where-they-run.md)

## Context

ADR 0016 put the models on a shared GPU and drew the consequence honestly: both
model Deployments default to `replicas: 0`, a reaper scales them back down when
chat goes idle, and a send that finds its model down starts it.

That is the right shape, and it left one thing missing. **At any given moment most
of the model list is not running, and there was no way to see that or to change
it from inside the app.** The picker labelled each option "running" or "not
running", which was honest but read-only; everything else meant a terminal and
`make k8s-model`. Three concrete failures came out of that:

- Deciding whether to reach for the 27B needed a fact the UI would not tell you:
  what is loaded *now*, and is something already holding the card.
- Freeing the GPU for a co-tenant's build meant waiting out a fifteen-minute idle
  timer or leaving the app.
- A send auto-started the model behind the chosen model's URL and nothing else.
  With Ollama up, scaling the 27B left it `Pending` with
  `Insufficient nvidia.com/gpu` — so chat reported "starting now" about a pod that
  was never going to run.

## Decision

**A model server is a thing you can look at, start and stop, from `/models`.**

Three routes on the system surface — `GET /api/system/models`,
`POST /api/system/models/{id}/start`, `POST …/stop` — and a screen over them.
`/api/chat/models` is untouched: that answers "what may I pick for this message",
these answer "what is on".

Four decisions inside that are load-bearing:

### The unit is a server, not a model

Ollama holds the 4B and the 8B and swaps between them on demand. Starting either
is one cluster action and stopping it takes both. A per-model switch would have to
lie about one of them, so the control is per server and names what it holds — with
a per-model "loaded" flag, because a model that was never pulled 404s at
generation time even on a healthy server.

### Starting one releases the others

There is one card. `start()` scales every other known server to zero first, exactly
as `make k8s-model` does, and says so in the response. It does not wait — the old
pod's shutdown and the new one's weight load are minutes — so the same handover
logic now backs chat's auto-start, which previously scaled up without scaling down
and produced the `Pending` failure above.

### Four states, because "starting" and "unknown" are real

`running`, `starting`, `stopped`, `unknown`. vLLM binds its port and *then* spends
minutes loading, so "the pod exists" and "you can ask it something" are minutes
apart; a two-state view would show green next to a model that fails every
question. And a cluster this install cannot read is `unknown` with null replica
counts — **not** `stopped`, which would be a confident wrong answer on every dev
box. The screen polls only while something is `starting`, that being the one state
that changes on its own.

### It degrades to a read-only view rather than breaking

No ServiceAccount token means no scaling. Then `controllable` is false, the states
still render (they come from asking the servers, which needs no cluster rights),
and the buttons are replaced by the `make k8s-model` line that does work there.
Start and stop answer `200 {ok: false}` with that command in `detail` rather than a
5xx: nothing broke, this install simply cannot.

## Consequences

**The reaper is unchanged, and can still stop a model somebody started by hand.**
Its idle signal is the chat transcript, so a server started for something *other*
than chat — a datasheet extraction pass — is released within ten minutes. That is
ADR 0016's deliberate bias toward releasing ("the failure mode of releasing too
eagerly is a reload next message"), and it is stated on the screen rather than
worked around. Changing it would mean a new held-until marker and untestable shell
in the reaper's ConfigMap, which is a worse trade than a sentence of UI text. If
non-chat use of a model becomes routine, that is the ADR to revisit.

**No new RBAC.** The existing `almagest-api-scale-models` Role already grants `get`
and `patch` on the two Deployments' `scale` subresources, which is exactly what
reading a replica count and setting one need. The credential still cannot create,
delete, or touch a co-tenant's workload.

**No nav tab.** The strip already carries thirteen destinations and does not fit a
phone. `/models` is linked from the picker on Ask, which is where somebody is
standing when a model turns out not to be answering.

**Not exposed to MCP.** All three routes are `Excluded(MACHINE_DOOR)`: they
describe and move the GPU rather than the inventory, starting one server stops the
other, and the agent calling it is very likely running on the model it would be
turning off.
