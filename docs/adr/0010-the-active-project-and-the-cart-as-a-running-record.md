# ADR 0010 — Open projects as tabs, and the cart as a running record

**Status:** accepted
**Date:** 2026-07-29
**Supersedes:** ADR 0007's checkout model (the three doors chosen at the end)
**Amends:** ADR 0007's framing of *what the cart is for*

## Context

ADR 0007 read Iliana's cart request as a **chooser**: gather parts while browsing,
then at the end pick one of three destinations — a project's BOM, a build, or a plain
stock movement. That is what got built and merged in #40, and it is the wrong shape.
Her correction:

> "You misunderstood the cart idea. The point is to make it easier to **keep track of
> what were doing**. Lets say I select a project and I go grab some parts, those parts
> end up in the selected project since I am currently taking them out of stock to my
> project. On the right of the screen you should see a collapseable section of what was
> already in your project, and another of what you are currently adding. Essentially,
> the **"Take" button in the lots would put it in your cart**."

Three things in that are not what 0007 built:

1. **The destination is chosen first, not last.** You select a project and then work.
   Nothing at the end asks "where should this go?" — the answer was settled before you
   walked to the shelf, because in the real activity it always is.
2. **"Take" is the entry point, not a search result row.** The gesture that fills the
   cart is the one already on the lot screen: the thing you do when you physically pick
   a part up. 0007 hung the gesture off search results, which is where you go when you
   are *deciding*, not when you are *doing*.
3. **The cart's job is orientation, not accumulation.** "What was already in your
   project" beside "what you are currently adding" is a *diff view* — the thing that
   tells you where you are in a job you are halfway through. 0007's cart could only
   show the second half, so it could not answer the question the feature exists for.

The deep error in 0007 was reading "keep track" as "collect". The design study before
it had actually recommended **project-as-a-mode**, and 0007 rejected that in favour of
the basket on the grounds that a mode is invisible state you can forget. That concern
was real but the conclusion was wrong: the fix for invisible state is to *make it
visible* — which is exactly what the always-present side panel does — not to remove
the mode and lose the context with it.

She then extended it, and the extension is not cosmetic:

> "you can open multiple projects/builds at once, then they show as **tabs** on the
> right panel"

That settles a question a single active project would have forced badly. Real bench work
is not one job at a time: you are finishing rev B while kitting rev C, and a walk to the
shelf serves both. A single mode would make you switch it mid-walk — and switching a
mode that governs where stock gets attributed is exactly the operation you will get
wrong while holding parts in one hand.

## Decision

**A set of open targets shown as tabs; one is focused; the cart is per-target.**

### Open targets, and the focused one

A target is a **project or a build** — the tab strip is heterogeneous on purpose,
because "kitting rev C" is a build and "the rev B project" is a project, and forcing one
to be expressed as the other would mean inventing a build nobody wanted. Opening one is
a deliberate act; so is closing it.

Exactly one tab is **focused**, and the focused tab is what "take" attributes to. Held in
the same kind of store as the cart and persisted, so the strip survives a reload and a
walk to the shelf.

### The cart is per-target, not global

**Each tab owns its own uncommitted lines.** This is forced rather than chosen: if one
cart were shared across tabs, switching focus would silently re-aim everything you had
already gathered, which is the misattribution this whole design exists to prevent. So
"currently adding" is a fact about a tab, and committing one tab leaves the others
untouched.

The consequence to honour: **closing a tab with uncommitted lines must not silently
discard them.** Ask, or refuse and say what is in it.

### "Take" writes to the cart, not to the ledger

`LotScreen`'s take is redirected: with a tab focused, taking adds a line to *that tab's*
cart and writes nothing. This is the single most behaviour-changing line in this ADR, so
its two edges are stated plainly:

- **With no tab open, take still commits immediately.** That path is Iliana's other
  explicit request — *"you should also be able to just pick a container, scan it and say
  how many parts you took or put back"* — and it must not acquire a project it does not
  need. A take with nothing open is a take, not a cart line.
- **Return is symmetric.** Putting something back while a project is active is a
  negative line in the same record, because "I took four and put one back" is one
  activity and must read as one.

### The side panel is the feature

A right-hand panel holding the **tab strip**, and under the focused tab two collapsible
sections:

- **Already in this project** — what is reserved, staged and consumed. Derived, per
  ADR 0004; nothing new is stored to render it.
- **Currently adding** — that tab's cart, uncommitted.

Collapsible, because it must not fight the shelf-side phone layout, but never hidden by
default while anything is open: the panel *is* the mitigation for the invisible-mode risk
0007 named, and a mode with no visible indicator is the failure it warned about. A tab
with uncommitted lines must say so **in the strip**, not only when focused — otherwise
the second tab becomes exactly the invisible state the panel exists to prevent.

### Commit is one act with one meaning

Committing a tab applies its lines against that tab's target — through the batch
endpoints #40 already built, which keep the rule that matters: **a line whose stock has
moved fails that line and not the batch.** That machinery survives this ADR unchanged;
only the choice of destination is gone, because the destination is the tab.

## What survives from #40, and what does not

**Survives** — and this is why the correction is cheap rather than a rewrite:

- `lib/cart/cart.ts`: the staging list, versioned `localStorage`, merge rules, stale
  capture, and the deleted-part-degrades-to-a-removable-row rule.
- `lib/cart/checkout.ts`: per-line application, failed lines staying put with a reason,
  safe to press twice.
- The batch endpoints and their ~40 ledger-counting tests.
- The Enter fix in the BOM part picker, which was an unrelated bug.
- Per-build legibility (`per assembly × assemblies = needed`), which stands on its own.

**Does not survive:**

- The three-door checkout screen. The destination is no longer a question asked at the
  end.
- Add-to-cart as the primary affordance on search result rows. Search stays a place to
  *decide*; it may keep a secondary "add" for the case where you are planning at a
  desk, but it is no longer the way the cart is normally filled.

## Consequences

- **The wrong tab being focused is now the risk, and it is worse than a forgotten
  cart** — it silently reattributes a take, and with several tabs open the wrong one is
  a plausible mistake rather than an exotic one. The mitigations are cumulative and all
  three are required: the strip is always visible; the take control **names the target
  it will attribute to** ("Take for *Buck converter rev B*"); and a tab holding
  uncommitted lines is marked as such in the strip. A take must never be attributable to
  a target the user cannot see named at the moment they press the button.
- **Per-target carts multiply the stale-capture problem** by however many tabs are open,
  and a tab left open for a week is a cart left full for a week. The reconcile-at-commit
  rule from #40 is what makes that survivable, and it is why it survives here unchanged.
- **Take is no longer immediate while a mode is on**, so the ledger no longer records
  the moment of physical removal — it records the commit. That is a real loss of
  fidelity, accepted because the alternative (write immediately, reconcile later) makes
  every mistake a compensating ledger row instead of an edit to an uncommitted list.
  Undo of an *uncommitted* line is now free, which is the compensating gain.
- **`localStorage` still does not cross devices**, and this makes that cliff steeper
  rather than shallower: an active project set on the phone is not set on the desktop,
  so the same take means different things on the two devices. Same known wall as the
  intake queue; the fix is the same (server-side), and it is not done here.
- **Two ADRs are numbered 0007** (`container-pictures-glyph-and-photo` and
  `the-cart-and-two-ways-to-choose-parts`), from two branches that took the number
  concurrently. Recorded here rather than silently renumbered, because both are already
  referenced from committed code.
