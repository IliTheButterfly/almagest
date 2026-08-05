# ADR 0018 — Two chat surfaces, writeups that move between them, and export

**Status:** proposed
**Date:** 2026-08-04

## Context

Two conversational surfaces are wanted, with different lifetimes:

- **Search chat** — "do I have something that can level-shift 3.3 V to 5 V?",
  "how many 0805 100 nF are left and where?". Short, disposable, inventory-facing.
  This is `PLAN.md`'s *fuzzy front door*.
- **Project chat** — a running technical conversation attached to one project,
  which knows its BOM, its builds and its allocations, and persists for months.

They need **separate history lists**, because a project's thread list being
polluted by fifty throwaway lookups makes it useless. But work has to be able to
cross the boundary in one direction: ask in a search chat for a writeup, and send
it to an existing project chat or to a new one.

Separately, the conversation must be exportable, so that a bigger hosted model
(ChatGPT, Claude) can be brought in for the parts a local 30B is not good at.

Three existing constraints shape the implementation more than the feature does:

- The API is **one replica on an RWO volume with a single SQLite writer**. An
  agent loop that calls tools which call back into the API, inside that same
  process, is a self-deadlock waiting for a busy connection pool.
- **ADR 0005:** models do not run in the API process.
- **`mcpserver/`** already exists and is exactly the right tool surface — a
  curated tool set over the HTTP API, whole units instead of `qty_milli`, with
  `coverage.py` forcing a disposition for every route so it cannot go stale.
  Building a second tool layer for chat would be building the same thing twice
  and letting one of them rot.

## Decision

### The agent loop lives in a new service, not in the API

A new `chatagent/` component — its own distribution, its own venv, its own
deployment, **no GPU**. It holds the agent loop, speaks MCP to `mcpserver`, calls
the model over the OpenAI-compatible endpoint from ADR 0016, and streams tokens to
the PWA over SSE. It persists threads and messages **through the API over HTTP**,
never by opening the database — the same rule ADR 0005 imposed on the extraction
worker, for the same reason.

The API's role is storage and retrieval: thread and message tables, CRUD routes,
export rendering. No model client, no tool loop, no streaming inference.

This is a third service, and that cost is real. It buys the one thing that
matters: the agent's tool calls are ordinary HTTP requests arriving at the API
from outside, indistinguishable from the PWA's, so they queue and fail like any
other request instead of deadlocking the process making them.

### One thread table, discriminated by kind

```
chat_threads(id, kind, project_id NULL, title, created_at, updated_at, archived_at)
chat_messages(id, thread_id, seq, role, content, tool_calls_json, model, created_at)
chat_writeups(id, title, body_md, origin_thread_id, project_id NULL, created_at)
chat_writeup_posts(writeup_id, thread_id, message_id)
```

`kind` is `search` | `project`, stored as `sa.String` + `StrEnumType` — **never
`sa.Enum`**, which emits a `CHECK` a test greps for. A third kind later (a
per-part thread, a build thread) is then a purely additive change, which is the
whole reason that rule exists.

Two history lists is a `WHERE kind = ?`, not two tables. The lists differ in the
UI and in retention, not in shape.

**`chat_messages` is append-only.** Not trigger-enforced like `stock_ledger` —
this is not a correctness invariant, it is a product one — but editing a message
creates a new message rather than mutating one, so a thread's history matches what
the model was actually shown. A transcript that silently rewrites itself is a
transcript that cannot explain a bad answer.

### A writeup is a first-class row, and sending it is a tool call

"Make a writeup of this and send it to the Nixie clock project" is one tool the
agent can call:

```
create_writeup(title, body_md, destination: {existing_thread_id} | {new_project_thread: {project_id, title}})
```

It writes a `chat_writeups` row, then posts it as a message into the destination
thread, recording the link in `chat_writeup_posts`. The writeup exists
independently of both threads, so it survives either being archived, and the same
writeup can be posted to two projects without being duplicated.

**Creating a new *project* is not part of this.** "Send it to a new project chat"
creates a new *thread*; if no project exists yet, the agent proposes creating one
and the user confirms in the UI. Letting a chat mint project rows on inference is
how a projects list fills with `Untitled 4`.

### Export renders facts, not just prose

`GET /api/chat/threads/{id}/export?format=md|json`, and the same for a writeup.

- **`md`** — the transcript or writeup as markdown with a YAML front-matter
  header, sized for pasting into another model's context window.
- **`json`** — messages with roles and tool calls intact, for round-tripping.

Both accept `include=context`, which appends the **resolved facts** the
conversation referenced: the parts as a table with their parameters, the BOM, the
lot quantities and locations. Exporting only the prose exports the agent's
paraphrase of the inventory, and a hosted model reading that paraphrase has no way
to know which numbers were real. The point of the export is to hand another model
the evidence.

Export is a read of already-stored rows, so it lives in the API. No model
involved.

### Chat may author; it may not keep records

The first draft of this ADR said "chat proposes; it does not commit", with a flat
read-only tool surface. **That line was drawn in the wrong place** — it split
tools by *read versus write*, and the distinction that actually matters is
**reversible authoring versus irreversible record-keeping**.

Creating a container is the case that shows it. It writes rows, so the flat rule
forbade it; but a container created wrongly is empty, visible in the tree, and
deletable — `app.models.events` already releases its identity on delete. Nothing
is lost and nothing is silently wrong. Meanwhile "make me eight Gridfinity 1×1
bins in the second drawer" is exactly the tedium a conversational interface should
absorb, and refusing it on a technicality that also forbids nothing dangerous is
the kind of rule that gets a feature disabled wholesale rather than obeyed.

So the surface is:

**Allowed — reversible authoring.** Creating containers and container types,
creating a project, drafting a BOM, creating a writeup. Each produces rows a
person can see and delete, and none of them asserts anything about the physical
world.

**Refused — irreversible record-keeping.**

- **Stock movements.** The ledger is append-only and undo is a compensating row,
  so a wrong movement is permanent history rather than a mistake. A conversational
  interface that can decrement a lot makes the ledger a record of what a model
  believed.
- **Accepting parameter candidates.** They go to `parameter_value_candidate` and
  the review queue like everything else. ADR 0017's rule is not suspended because
  the request arrived as a sentence.
- **Tag provisioning.** Physical, and needs a person holding the tag.

**Every authoring action is confirmed before it runs.** The agent returns a
described action; the UI renders it as a card with an explicit button, and nothing
is written until it is pressed. That is cheap, it keeps the human the one who
acts, and it makes the reversibility argument above a second line of defence
rather than the only one.

**Substitution answers still come from `suggest_parts_for_requirements` and
`search_parts`, never from the model's own judgement.** Unchanged, and unaffected
by any of the above: the model may phrase and rank, the SQL filter decides. A
plausible substitute with the wrong voltage rating is a field failure.

## Consequences

- Four new tables, all additive, no `CHECK` constraints, one migration.
- New API routes for threads, messages, writeups and export — **every one needs a
  line in `mcpserver/almagest_mcp/coverage.py`** or `make check` goes red. Most
  should be `Excluded`: an agent driving its own transcript store is a loop.
- A third service to deploy, and a third `make *-check` target folded into
  `make check`. Its tests use a fake model client and a fake MCP transport, so CI
  stays offline.
- SSE through whatever fronts the cluster. Note `deploy/`'s 443 problem (ADR
  0009) applies unchanged; SSE over `:30443` works but buffering proxies break it,
  so this is worth verifying rather than assuming.
- Threads are not tags, not labels, and carry no short ID. Nothing physical points
  at a conversation.
- The search-chat history is expected to be noisy and disposable; an archive/prune
  policy is a later, purely additive decision. Project threads are never
  auto-pruned.

## Supersedes

Nothing. Applies ADR 0005's out-of-process rule to a second kind of model work,
and reuses ADR 0012's MCP tool surface rather than growing a parallel one.
