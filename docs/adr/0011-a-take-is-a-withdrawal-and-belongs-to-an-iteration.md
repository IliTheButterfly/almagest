# ADR 0011 — A take is a withdrawal, and it belongs to an iteration

**Status:** accepted
**Date:** 2026-07-31
**Amends:** [ADR 0010](0010-the-active-project-and-the-cart-as-a-running-record.md),
whose tab strip and per-tab record are unchanged — what changes is what
*committing* one does, and what a project tab may hold.
**Implements:** [ADR 0004](0004-staged-parts-and-project-iterations.md) on the
path ADR 0010 opened without reconciling against it.

## Context

Iliana, looking at the running demo:

> "parts landed in a project and not a revision get kinda lost"

They are, and tracing it turned up something worse than being hard to find.

ADR 0010 made `LotScreen`'s Take fill the focused tab's record. It inherited from
#40 the answer to *what committing that record does*, and that answer predates
ADR 0004 being wired to this gesture:

- a **build** tab committed to `allocate-batch`, which creates a **hold**. ADR
  0004's own state table says `reserved` means "still in the bin, held for this
  build";
- a **project** tab committed to `bom_lines`, which is a statement about what the
  design wants.

So in both cases the user had physically picked parts up and walked away, and
`stock_lots.qty_milli_cached` still counted those parts in the drawer. That is
precisely the failure ADR 0004 introduced the staging location to prevent:

> "If parts have physically left the drawer, then `stock_lots.qty_milli_cached`
> for that drawer is a lie until the ledger says otherwise. […] you go to the
> drawer and the number is wrong."

The project case then adds its own problem on top, which is the one that got
noticed first. A project is a *design*; the thing being assembled is a build. A
build has a staging location (`project_builds.staging_location_id`) and an
assembly count to measure progress against. A project has neither, so lines
committed there became BOM rows attached to no iteration — parts that had left
the shelf and were now accounted for nowhere. And the conversion was wrong in its
own right: `qty_per_assembly_milli` was set to the quantity taken, so picking up
four turned into "four **per assembly**", which a three-assembly build reads as a
demand for twelve.

The backend had the right answer the whole time and nothing was calling it.
`services/staging.py` and `reservations.py` implement withdrawal as an ordinary
ledger move, and the docstring describes this exact gesture:

> "Staging with no prior hold is equally legitimate — *take these out and put them
> in the project box* is one gesture at a bench, not two."

## Decision

### Committing a build's record stages the parts. It does not reserve them.

A cart line says **I picked this up**. The only honest record of that is a ledger
move, so committing a build tab calls `POST /api/builds/{id}/stage` for each line,
which moves the stock into that build's staging location and writes a `STAGED`
allocation.

Reserving does not disappear; it stops being what this gesture means. A hold on
stock you have *not* walked to the shelf for is a real and different operation,
and it stays where it already was — on the build screen, with the pick list.

**One request per line, deliberately.** There is no batch stage route and none is
needed: every line carries the `clientOpId` minted when it was added, so a resend
replays rather than moving parts twice, a refusal stops that line rather than the
loop, and a row leaves the record only once the server says it applied. Those are
the same three rules #40's batch endpoints gave, obtained without a new route.

### A staged part counts against a BOM line only when that is not a guess

Attribution is what turns "stock moved somewhere" into "progress against line 3",
so the commit reads the build's BOM to find it. But two BOM lines may legitimately
name the same part — `R1` and `R7` of one resistor are two requirements — and
picking either would credit work to a line the user never chose. So exactly one
candidate attributes; zero or several attribute to nothing and the part stages as
off-BOM, which ADR 0004 already treats as a first-class case ("the part nobody
planned for" is what the roster is for).

A failure to read the BOM does not fail the withdrawal. The parts moved; losing
the attribution is a far smaller loss than refusing to record that.

### A project tab cannot receive a take. It asks which iteration.

Pressing Take with a project focused does not attribute to the project — it asks
**which iteration these are for**, listing that project's open builds and offering
to start one. Choosing opens that build as a tab, focuses it, and puts the line in
*its* record; from then on takes go straight there without asking again.

Three details follow from what the question is for:

- **The button says so.** "Take 1 for an iteration…", never "Take 1 for Bench
  PSU". ADR 0010 forbids a take being attributable to a target the user cannot see
  named when they press the button, and naming the project would be worse than
  vague — it would be wrong.
- **A closed build is not offered**, because staging into one is refused
  server-side, and it is *said* rather than silently filtered: "my build is not in
  the list" is otherwise a mystery.
- **Starting an iteration is part of the question.** The parts are already in your
  hand; sending you to another screen to plan a build first is how they end up on
  the bench instead of in the record.

A project tab remains useful and remains a tab: it shows the BOM in the panel, and
it is still where a migrated v1 record drains to, which is why the BOM checkout
path survives rather than being deleted.

## Consequences

- **The drawer is right again.** The count drops when the parts leave, which is
  what makes every other number downstream trustworthy.
- **Undo is the ledger's, not ours.** A staged withdrawal reverses through the
  existing compensating-row mechanism (`unstage`), so "put it back" needed no new
  feature — but note the free undo ADR 0010 gained applies to an *uncommitted*
  line only. Once committed, undoing is a ledger operation, as it should be.
- **The parts are somewhere you can look.** `PROJECTS / <project> / Assembly n` is
  an ordinary location, so the bin screen, search and scanning all work on it with
  no special-casing — ADR 0004's whole argument for using the tree.
- **One extra read per commit** (the build, then its BOM) to attribute lines. It
  is cached for the whole commit, not per line, and it is skipped entirely if it
  fails.
- **`qty_per_assembly_milli` is no longer written from a take**, so the
  four-becomes-four-per-assembly bug goes with the path that caused it. The BOM
  checkout keeps the field, where the number really does mean per assembly.
- **Committing is now a real movement, so the wording changed too**: "Send these
  parts to Build #2" rather than "Reserve these lines against". A button that says
  reserve and writes a movement is one nobody can press confidently.

## Rejected alternatives

**Give projects their own staging location and let them take.** ADR 0004's diagram
does draw a project-level floating node, and `project_staging_location()` builds
it, so this was the cheap option — a new project-scoped stage route and no new
question. Rejected because it answers "where did the parts go" without answering
"what are they for": parts floating against a design, with no assembly count to
measure them against, are exactly the state that reads as lost. The project node
keeps its real job as the *parent* of the assembly nodes.

**Keep holds, and stage later as a separate step.** Truthful about intent —
sometimes you really are just earmarking stock — but it makes the common bench
case two gestures for one physical act, and the second is the one nobody does. The
staging docstring had already reached this conclusion for the same reason.

**Fix only the per-assembly arithmetic.** The smallest change, and it would have
left the thing that was actually noticed: parts carried off while the system says
they are still in the drawer.
