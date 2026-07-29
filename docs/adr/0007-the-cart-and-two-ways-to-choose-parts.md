# ADR 0007 — The cart, and the two ways of choosing parts

**Status:** accepted
**Date:** 2026-07-29
**Supersedes:** the recommendation in the UI design study (project-as-a-mode)

## Context

The design study compared four mechanisms for getting parts onto a project and
recommended **project-as-a-mode** — an active target held in a store, with a
"send to this" action wherever a part appears. It ranked the shopping basket
second, cheaper and safer but worse for the single-part case.

Iliana chose the cart, and supplied the observation the study was missing:

> "there are two ways to select parts. Either you have a BOM coming from another
> source, or you are making the project and need to look at what parts we already
> have. in the latter case, you already have an idea of what you have, but need to
> pick them out from the list."

That reframes the problem. The study had been optimising a *tap count* for
"add one part to a project". The real second mode is **browsing your own stock to
decide what to build with** — a session, not a gesture. You do not know the part
number; you know roughly what you own and you are choosing among it. That is
inherently multi-item and inherently exploratory, and a mode with a per-row
"send" button models it as a series of unrelated single decisions.

She also extended it past projects:

> "you should also be able to just pick a container, scan it and say how many parts
> you took or put back."

## Decision

**One cart, used for both project allocation and plain stock movement.**

### The two entry paths stay distinct, because they carry different information

| | A BOM from elsewhere | Choosing from what you own |
|---|---|---|
| You start with | part numbers and quantities | a rough intent |
| Lines arrive | matched or unmatched, all at once | one at a time, as you decide |
| The unknown | *do I have these?* | *what do I have?* |
| Surface | import, then the shortage view | **search, then the cart** |

The import path already exists and is unchanged. The cart serves the second path,
and it is the one the app had no answer for.

Critically, the cart is **populated from the ordinary search screen** — not a
cut-down picker. That is the whole point of Iliana's complaint that the BOM picker
"is still not the same view as the search tab": when the question is *what do I
have*, the facet counts, the category rail and the stock-per-row **are** the
answer. A one-field search box cannot express it.

### The cart is a staging area, not a commitment

Adding to the cart writes nothing. It is deliberately the same shape as the intake
queue (`lib/intake/queue.ts` + `sync.ts`): a local list, a visible count, and one
screen that drains it. Nothing touches the ledger until checkout, which is what
makes it safe to browse with.

### Checkout targets are a small closed set, and that is the unification

A cart drains to exactly one of:

1. **A project's BOM** — becomes `bom_lines`, matched where a part was chosen.
2. **A build** — becomes allocations, and then staged parts per ADR 0004.
3. **Stock movement** — take or return against a container, with no project at
   all. This is the "pick a container, scan it, say how many" case, and it is why
   the cart is not a projects feature.

One mechanism, three destinations. The alternative — a project cart plus a
separate take/return basket — would be two lists to keep in step and two places to
learn.

### Per-build quantities are already modelled; the UI has to expose them

> "you made a working pcb and you want to make more, you just request parts for
> more pcbs and transfer them when you get the chance. So you get to know how many
> is needed per build and how many are being used."

This needs **no schema change**. ADR 0004 already made demand derived:

```
demand    = qty_per_assembly_milli × assembly_count
accounted = reserved + staged + consumed
needed    = max(0, demand − accounted)
```

So "request parts for three more boards" is raising `assembly_count`, and the
shortfall grows on the next read with nothing backfilled. "Transfer them when you
get the chance" is the existing pick list and staging flow. What is missing is
purely presentational: *per build* and *in use* are not currently legible
anywhere, and they are the two numbers Iliana names.

## Consequences

- The cart must survive navigation and a reload, so it is persisted — and it must
  therefore be **explicitly clearable**, or a forgotten cart becomes the invisible
  state that made project-as-a-mode risky in the first place. The failure the
  study warned about is not avoided by choosing the cart; it is *moved*, from "a
  mode I forgot is set" to "a cart I forgot is full". A visible count is the
  mitigation, and it must be visible from every screen that can add to it.
- `localStorage` does not cross devices. Gathering on a phone at the shelf and
  checking out at the desktop will not work until the cart is server-side. This is
  a known cliff, stated here rather than discovered later; the intake queue hit the
  same wall and was moved server-side for exactly this reason, so there is a
  precedent to follow when it matters.
- Cached part names and quantities go stale. The cart shows what it captured and
  reconciles at checkout, where a line whose stock has moved must fail *that line*
  and not the batch — the same rule the intake sync already follows.
- A cart holding a part that has since been deleted must degrade to a named,
  removable row rather than an error that blocks the whole checkout.
