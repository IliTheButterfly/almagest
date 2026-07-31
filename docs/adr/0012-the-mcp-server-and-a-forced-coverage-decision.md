# ADR 0012 — The MCP server, and why every route must be decided about

**Status:** accepted, 2026-07-31
**Supersedes / superseded by:** nothing

## Context

The questions this system exists to answer — *do I have a 100 nF 0603 X7R, where
is it, can I build this board, what am I short?* — get asked in the middle of doing
something else, often in a chat window and away from the bench. The PWA answers
them well when you are looking at it. Nothing answered them from anywhere else.

An MCP server is the boring way to close that: it is a subprocess speaking stdio
that turns tool calls into HTTP requests against routes that already exist. No new
storage, no new writer, no new schema.

Two things about it needed deciding rather than defaulting.

### 1. How much of the API to expose

`openapi.json` has 131 operations. Exposing all of them is the tempting default —
generate a tool per route and be done — and it is wrong twice over. A model handed
131 tools chooses badly among them. And most of those routes are not for an agent
at all: a provisioning walk is a person moving along a cabinet touching a phone to
each tag, the extraction worker's claim/submit pair is a work queue with a lease,
and a label sheet exists to be printed and stuck onto something. An agent calling
those produces a database that no longer describes the room.

The opposite default — hand-pick a dozen useful routes — has its own failure, and
it is the one that actually kills integrations like this. The API grows. The
server does not. Six months later it exposes a stale sixth of the routes, nobody
can say which sixth, and the only way to find out is to read both. Asking people
to remember in a contributing guide does not survive contact with anyone, human or
model.

### 2. Whether an agent may write

The ledger is append-only and undo is a compensating row, so a wrong movement is
always reversible. But it is not *invisible*: until somebody recounts that bin, the
balance is a confident lie about physical reality, and this project's entire value
proposition is that the balance can be trusted. A model that reads "I used 3" as 30
has made the inventory wrong in a way no test catches.

## Decision

**A curated surface of 25 tools, and a manifest that makes the curation
self-maintaining.**

`mcpserver/almagest_mcp/coverage.py` lists every operation id in `openapi.json`
exactly once, as either `Exposed("tool_name")` or `Excluded(Reason.X, "why not")`.
`tests/test_coverage_manifest.py` diffs the manifest against the committed schema
in both directions and fails on any difference, in `make check` and in its own CI
job.

So **adding a route to the backend breaks `mcpserver` until somebody decides what
to do about it.** That is the intended cost, and it is the whole point of the
design rather than a side effect. Deciding is one line and `Excluded` is a
perfectly good answer, but it cannot be skipped — and the reason gets written down
while the person writing it still knows it.

The exclusion reasons are a closed set (`HANDS_ON`, `MACHINE_DOOR`,
`HUMAN_JUDGEMENT`, `AUTHORING`, `SEQUENCED_WORKFLOW`, `BINARY_PAYLOAD`,
`SUBSUMED`). An open-ended "not needed" would become a dumping ground where every
future route lands by default; a closed set means an operation fitting none of the
reasons is a signal that it probably should be exposed.

**Writes are off unless `ALMAGEST_MCP_ALLOW_WRITES` says otherwise**, and when off
the write tools are *not registered* rather than registered-and-refusing — a tool a
model can see and cannot use costs it a turn to discover and reads like a bug. When
on, the five movement tools go through the same `/api/stock/...` routes the PWA and
the station use, carry `source="api"` and `device_id`, and mint a `client_op_id`
they return so a retry replays instead of double-taking and an undo has a handle.

**It is a translation layer, not a second API.** Nothing in the package imports
`app.models`, opens the SQLite file, or knows what a `parameter_value` row is.
`app/services/ledger.py` stays the sole writer. The translation is the actual work:
whole units in and out instead of `qty_milli` (integer thousandths are a storage
invariant, not something to make a model reason about — handed `qty_milli` a model
writes `2500` meaning 2500 pieces and takes 2.5), a `{template: value}` mapping
instead of a list of pairs, and `ApiError` kept apart from `ApiUnavailable` so a
refusal is not retried.

**Its own package and venv, no submodules, no backend dependency.** The MCP SDK
has no business in the API image. The contract test reads the committed
`openapi.json` and the tool tests drive a scripted transport, so the venv installs
in seconds and nothing here needs `elec-value-parser` or a migration run.

`.mcp.json` at the repo root configures it for any client that reads one, so in
this checkout it needs no setup beyond a running API. Transport is stdio: this
server holds no state, no session and no credentials, so a shared network
deployment would add a hop and an access-control question for nothing.

## Consequences

**A backend PR that adds a route now has a red check until `coverage.py` is
updated.** This is deliberate and should not be worked around. The failure message
names the operations and says what to do.

**The tool docstrings are load-bearing text**, not comments. They are the only
instructions a model gets, so they carry the honesty this project's user-facing
text is supposed to carry: that a parametric miss means "nothing *recorded as*
matching" rather than "nothing in the room", and that a suggestion is a proposal
because a substitute with the wrong voltage rating is a field failure.

**Two vocabulary tools exist purely to prevent a specific wrong answer.**
`list_filterable_fields` and `list_part_kinds` are cheap and slightly redundant
with the UI's needs, but without them a model invents the field name
`capacitance_uF`, gets `total: 0`, and reports "you have none" — a wrong answer
indistinguishable from a right one, which is the worst thing this server could do.

**The manifest is a second place the API surface is described.** It is a real cost
and the test is what makes it survivable: unlike a document, it cannot drift. It
also turned out to pay for itself immediately — the manifest was first written
against a slightly older `openapi.json` and the test named all 28 routes that had
landed since, which is precisely the drift it exists to catch.

**What is deliberately still missing:** nothing streams, nothing is cached, and
there is no auth. All three are fine while the transport is a local subprocess
talking to a LAN API; all three become real questions the moment anyone wants this
reachable over the network, and that should be a new ADR rather than a quiet
addition here.
