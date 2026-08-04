# `mcpserver/` — Almagest over the Model Context Protocol

The inventory as tools an agent can call: *do I have a 100 nF 0603 X7R, where is
it, can I build this board, what am I short?*

Those are the questions this system exists to answer, and they get asked in the
middle of doing something else — in a chat window, away from the bench. This
server makes each one a single tool call instead of asking a model to learn 142
HTTP routes and invent a query language.

```bash
make mcp-check     # ruff, mypy --strict, pytest — folded into `make check`
make mcp-run       # stdio server against http://127.0.0.1:8000
```

## It is not a second API, and not a second writer

Every tool is a call to the same `/api/...` route the PWA and the bench station
call. `app/services/ledger.py` stays the sole writer, server-side idempotency
applies unchanged, and no invariant in `CLAUDE.md` gets a second implementation to
drift from. Nothing here imports `app.models`, opens the SQLite file, or knows
what a `parameter_value` row looks like.

It is a **translation layer**, and the translation is the work:

| The API speaks | A model is given |
|---|---|
| `qty_milli` — integer thousandths, exact ledger sums | `quantity` in whole units, converted at the boundary |
| `filters: [{template, value}]` | `filters: {"capacitance": "20-30uF"}` — one value per template is all `UNIQUE(part_id, template_id)` permits anyway |
| 142 operations | 26 tools, each named for a question |
| `{"detail": {"reason", "message"}}` | `ApiError` with the server's own reason, kept apart from `ApiUnavailable` so a refusal is not retried |

Quantities are the one worth spelling out. Milli-units exist so ledger sums stay
exact — a storage invariant, not something to make a model reason about. Handed
`qty_milli`, a model writes `2500` meaning 2500 pieces and takes 2.5 of them. So
every tool takes and returns units, and refuses anything finer than a thousandth
rather than rounding it to a movement that claims to have happened and moved
nothing.

## 26 tools, and 116 deliberate refusals

The curation *is* the product. A model handed one tool per operation chooses badly
among them,
and most of those routes are for a person holding a phone in front of a drawer (a
provisioning walk, a tag bind, a label sheet) or for another machine (the
extraction worker's claim/submit door). An agent calling those is a bug.

**Read [`almagest_mcp/coverage.py`](almagest_mcp/coverage.py) before adding
anything.** Every operation in `openapi.json` has exactly one entry there —
`Exposed("tool_name")` or `Excluded(Reason.X, "why not")` — and
`tests/test_coverage_manifest.py` fails on any difference between that manifest
and the schema.

That is deliberate, and it is the answer to the one failure mode a curated server
has: the API grows, the server does not, and six months later it exposes a stale
sixth of the routes with nobody able to say which sixth. **Adding a route to the
backend breaks this package until somebody decides what to do about it.** Nobody
has to remember; `make check` remembers.

Deciding is cheap — one line, and `Excluded` is a perfectly good answer — but it
cannot be skipped, and the reason gets written down while the person writing it
still knows it. The reasons are a closed set (`HANDS_ON`, `MACHINE_DOOR`,
`HUMAN_JUDGEMENT`, `AUTHORING`, `SEQUENCED_WORKFLOW`, `BINARY_PAYLOAD`,
`SUBSUMED`) so that "not needed" cannot become a dumping ground where every future
route lands by default. An operation that fits none of them is a hint that it
probably should be exposed.

### What is exposed

*Vocabulary* — `list_filterable_fields`, `list_part_categories`, `list_part_kinds`.
These exist because a model that invents the template name `capacitance_uF` gets an empty
result set and reports "you have none", which is the worst failure this server can
produce: a wrong answer indistinguishable from a right one.

*Catalogue* — `search_parts`, `get_part`, `get_part_parameters`,
`search_datasheets`, `suggest_parts_for_requirements`.

*Place and balance* — `resolve_short_id`, `browse_locations`, `get_location`,
`get_lot`, `get_lot_history`.

*Projects* — `list_projects`, `get_project`, `get_project_bom`,
`get_bom_suggestions`, `get_build_shortages`, `get_build_pick_list`.

*Movements, off by default* — `consume_stock`, `return_stock`, `move_stock`,
`recount_stock`, `undo_movement`.

*Diagnostics* — `check_health`.

## Writes are off unless you turn them on

`ALMAGEST_MCP_ALLOW_WRITES=true`. Not timidity — a read is recoverable by
definition, and a write here is recoverable too (the ledger is append-only and undo
is a compensating row, never a delete). But it is not *invisible*: until somebody
recounts that bin, a wrong balance is a confident lie about physical reality, and
the entire point of this system is that the balance can be trusted. A model that
misreads "I used 3" as 30 has made the inventory wrong in a way no test catches.

When writes are off the tools are **not registered**, rather than registered and
refusing — a tool a model can see and cannot use costs it a turn to discover, and
the refusal reads like a bug.

When they are on, every movement carries `source="api"` and
`device_id=ALMAGEST_MCP_DEVICE_ID` (default `mcp`), so "which of these movements
did an agent make" is a `WHERE` clause rather than an investigation. Each write
mints a `client_op_id` and returns it: a retry with the same key replays the
stored response instead of double-taking, and the same key is the handle
`undo_movement` accepts.

## Honesty about what an empty answer means

Both of these are in the tool docstrings, because docstrings are the model's only
instructions:

- **Parametric search is a deterministic SQL filter over recorded parameters.** A
  part whose parameters were never filled in (`is_stub`) cannot match a filter, so
  `total: 0` means "nothing *recorded as* matching" — not "nothing in the room".
- **Suggestions are proposals.** `suggest_parts_for_requirements` and
  `get_bom_suggestions` rank candidates and separate in-stock from not-stocked;
  neither chooses. `CLAUDE.md` is explicit that a plausible substitute with the
  wrong voltage rating is a field failure, so a substitution is decided by the SQL
  filter's `substitution_direction` and confirmed by a human — never by the model
  reading the list.

## Layout

| File | What it holds |
|---|---|
| `coverage.py` | **The map.** Every operation, and what this server decided about it. |
| `routes.py` | Every route this server calls, by operation id. Tools never write a URL. |
| `tools.py` | The tools. Docstrings are the model's instructions — write them for that reader. |
| `api.py` | `urllib` transport, and the `ApiError`/`ApiUnavailable` split. |
| `config.py` | `ALMAGEST_MCP_*` settings. |
| `server.py` | Wiring, and the write gate. |

`CLAUDE.md` says API clients are generated, never hand-written. This one is
hand-written for the same reason `deviceagent/agent/api.py` is — twenty-five
operations, no auth flow, no streaming — and with the same compensating control:
`tests/test_api_contract.py` reads the committed `openapi.json` and asserts every
route and every field the tools send or read. If `routes.py` outgrows a screen,
generate the client.

## Connecting a client

`.mcp.json` at the repo root configures this for any MCP client that reads it, so
in this checkout it needs no setup beyond a running API. For anything else, the
command is `uv run --directory /path/to/almagest/mcpserver almagest-mcp` over
stdio.

Stdio rather than a shared HTTP service on purpose: this server holds no state, no
session and no credentials, so a network deployment would add a hop and an
access-control question for nothing. `MCPServer` speaks streamable-http and this
code would work over it unchanged if that ever becomes worth it.

## Tests

No backend dev dependency, unlike `deviceagent` — the contract test reads the
committed `openapi.json`, and the tool tests drive a scripted transport. Nothing
here writes a row directly, so there is no ledger behaviour to assert against real
migrations that the backend's own suite does not already cover, and keeping the
backend out means this venv needs no submodules and installs in seconds.

The `live` marker is for a test that wants a real API on
`ALMAGEST_MCP_API_BASE_URL`; skipped by default, like the agent's hardware tests.
