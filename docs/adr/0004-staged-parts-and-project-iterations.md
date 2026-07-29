# ADR 0004 — Staged parts live at a project location; iterations are builds

**Status:** accepted
**Date:** 2026-07-29

## Context

The requested workflow is how prototyping actually goes: build one, revise it,
build it again, reuse some parts from the last iteration. Specifically:

- taking components out of stock should offer to **send them to a project, or to
  one of its assemblies**;
- those parts are **"floating"** — set aside for a project, not yet assembled
  into anything;
- there should be a **roster of parts used**, because in practice they will not
  always have been tracked correctly;
- **changing the assembly count** should mark the extra parts as *needed* until
  they are reserved, and eventually assembled.

Phase 2a already has `projects`, `project_builds` (which is what an iteration
is), `bom_lines`, and `stock_allocations` with states
`planned → reserved → consumed | released`.

The open question is what "floating" *is*. There are two candidate answers and
they are not equally valid.

## Decision

### Staged parts are a real location, and getting there is a ledger move

The tempting cheap answer is a pure allocation state: mark the allocation
"floating" and leave the stock where it is. **That is wrong, and the existing
invariants say so.**

If parts have physically left the drawer, then `stock_lots.qty_milli_cached` for
that drawer is a lie until the ledger says otherwise. CLAUDE.md is explicit that
balances are read from that cache and that the ledger is the only record of
movement. A state flag that quietly leaves 50 resistors counted in a bin they are
no longer in produces exactly the failure this whole design exists to prevent: you
go to the drawer and the number is wrong.

So **withdrawing parts to a project is an ordinary `move`**, written by
`services/ledger.py` like every other movement, to a destination location that
represents the project. Consequences that fall out for free, all of which the
cheap answer would have had to invent:

- the drawer's count is correct the moment the parts leave;
- the move is undoable by the existing compensating-row mechanism, so "put it
  back" is not a new feature;
- "where are my project's parts" is answered by the existing bin screen;
- a part sitting in a project box for six months is *visible* rather than
  implicitly missing.

### The granularity comes from the tree, not from new columns

"A project, or one of its assemblies" is two levels, and `locations` is already an
adjacency list with no depth limit and a working path cache. So:

```
PROJECTS (staging root)
└── Blinky v2                 ← the project's floating parts
    ├── Assembly 1            ← committed to a specific unit
    └── Assembly 2
```

These are ordinary `locations` with `is_staging` set — the same flag `INBOX`
already uses — so every existing screen, scan path and capacity rule works on them
with no special-casing. They are **not** `is_placeable`, so auto-assignment will
never propose one as a home for incoming stock.

Deliberately *not* new columns on `projects` or a parallel "project inventory"
table. A second place where quantity lives is the mistake PartKeepr made with
quantity-on-part, and it would need its own reconciliation forever.

### `AllocationState` gains `staged`

`planned → reserved → staged → consumed`, with `released` reachable from
`reserved` and `staged`.

| State | Physically | Counts against a lot's reserved? |
|---|---|---|
| `planned` | still in the bin, nothing held | no |
| `reserved` | still in the bin, held for this build | **yes** |
| `staged` | **moved to the project location** | no — it left the bin |
| `consumed` | soldered in; ledger row written | no |
| `released` | hold given back | no |

`staged` must **not** count as reserved, and that is the subtle part. The parts are
no longer in the source lot at all, so counting them there would double-count
them: once as reserved stock in a drawer that no longer holds them, and once as
real stock in the project location. The invariant that keeps the cache a single
indexable predicate — exactly one state holds stock — survives, because `staged`
holds stock at its *new* location in the ordinary way.

Adding a member is a one-line change precisely because there is no `CHECK`
constraint. This is the second time that rule has paid.

### Demand is derived, so changing the assembly count needs no backfill

```
demand(line)    = qty_per_assembly_milli × assembly_count
accounted(line) = reserved + staged + consumed
needed(line)    = max(0, demand − accounted)
```

Raise `assembly_count` from 1 to 3 and `needed` grows on the next read. Nothing is
migrated, no rows are rewritten, and lowering it again does not strand anything —
it just makes `needed` zero while leaving the physical facts alone. The
requirement "changing the number of assemblies marks those parts as needed until
they get reserved and eventually assembled" is satisfied by construction rather
than by an event handler that could be missed.

`assembly_count` stays on `project_builds`, not `projects`: iteration two
legitimately builds a different number of units than iteration one, and putting it
on the project would rewrite history when it changed.

### The roster is the allocation list, and it accepts corrections

The "roster of parts used" for a build is its allocations — but the requirement is
explicitly that reality will not always have been tracked. So the roster must
accept a line that never went through `reserved`: **record what was actually used,
after the fact.** That writes a ledger row and a `consumed` allocation in one step,
against a BOM line or against no line at all (the part nobody planned for).

Refusing to record an untracked part would guarantee the roster is wrong, which is
worse than a roster that admits it was edited. Every such row is attributable —
`stock_ledger` already carries `source`, so an after-the-fact correction is
distinguishable from a scan.

## Consequences

- One new `AllocationState` member, one new nullable column
  (`project_builds.staging_location_id`), no table rebuild.
- Staging locations are created lazily, on first stage-to-project, so a project
  that never withdraws anything creates no clutter.
- **Deleting a project cannot cascade to its staging location** if stock is
  sitting in it. That is a refusal, not a cleanup: the parts are real and on a
  shelf somewhere. Same reasoning as an over-capacity put-away being recorded
  rather than blocked — the physical world wins.
- A shortage report now has three numbers per line, not one, and the UI has to
  keep them distinguishable: needed, staged, and the pre-existing
  "nobody has said what this line is". Merging any of them lets a BOM look
  buildable when it is not.
- `is_staging` locations must be excluded from the "where is my stock" *totals* a
  part detail screen shows as available for other work — parts in a project box
  are spoken for. They are still stock and still findable; they are not free.
