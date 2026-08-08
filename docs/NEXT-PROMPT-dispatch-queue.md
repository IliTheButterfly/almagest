# Prompt for the next agent — the capture-dispatch queue

Paste the block below. It assumes `docs/HANDOFF-vision-and-bench.md` is in the
tree, which is where the context it refers to lives.

The task chosen is the dispatch queue, worker and intake panel, because it is the
largest remaining chunk and the one that turns the vision path from a CLI
experiment into something that runs unattended. If you would rather the next
session grew the corpus or deployed the 30B, swap the first paragraph and the
numbered plan — the environment notes and the non-negotiable rules apply either
way.

---

```
Build the capture-dispatch queue, worker and intake panel for Almagest —
the piece that makes the vision path run unattended instead of from a CLI.

READ FIRST, in this order:
  docs/HANDOFF-vision-and-bench.md   — what exists, what was measured,
                                       what turned out to be false
  docs/adr/0021-a-second-reader-for-the-frame-the-browser-already-read.md
  docs/adr/0017-the-researcher-proposes-and-never-asserts.md
  backend/app/services/research.py   — the lease machinery you are copying

The design is settled; this needs execution and a fresh context, not new
decisions. In outline:

1. Six dispatch_* columns plus an index on `pending_intakes`, and a
   `DispatchState` enum: NOT_REQUESTED (the default — dispatching costs a
   GPU handover, so it is opt-in, unlike research) → PENDING → CLAIMED →
   PROPOSED | UNIDENTIFIED | FAILED.
   UNIDENTIFIED must NOT be FAILED, for the same reason research.py keeps
   EXHAUSTED separate: "we cannot tell what this is" is a photograph problem
   whose fix is another photograph.
2. `services/dispatch.py` — a mechanical copy of research.py. Copy it; do
   not abstract. document_text.py and research.py are already two
   independent copies and both docstrings defend that.
   LEASE_SECONDS = 1800 (a run can sit behind a model swap),
   MAX_DISPATCH_ATTEMPTS = 2 (each attempt costs a GPU handover).
3. Five routes; five lines in mcpserver/almagest_mcp/coverage.py or
   `make check` goes red by design. Recommend all five Excluded.
4. `app/scripts/dispatch_captures.py` — the third sibling of
   extract_datasheets.py and research_datasheets.py. Same ApiClient
   Protocol so tests drive it with TestClient. It makes no research or
   extraction calls: it reads, proposes, creates stubs, submits.
5. `intake_identity_candidates` (ranked, keeps the losers, source_text NOT
   NULL) rendered as a section in the existing IntakeQueueScreen.tsx.
   Choosing one calls the EXISTING POST /api/intake/{id}/resolve — do not
   build a second acceptance mechanism.

Rules that are not negotiable and are easy to break by accident:
- The vision model must never write pending_intakes.mpn (that is what the
  BARCODE said) or resolved_part_id (what a PERSON decided).
- Vision confidence must never reach candidates.AUTO_PROMOTE_CONFIDENCE.
  Measured: the model reported 0.95 on a wrong answer.
- No model, image decode, base64 or /v1/ call under backend/app/api/routes/.
  There is a grep test; keep it passing.
- No auto-resolve of an intake entry at any confidence. PROPOSED is terminal
  for the machine only.

Environment:
- Work in a git worktree under .claude/worktrees/ (isolate before the first
  edit).
- Route `make check` through windo-lab with --local:
    export WINDO_AGENT="claude-dispatch-queue"
    windo-lab build run be:almagest-check -p bench --local -- make check
  Sub-5s commands run direct; full suites do not.
- The GPU is co-tenanted and exclusive. You probably do not need it — the
  whole thing is testable with FakeVisionProvider. If you do take it,
  suspend almagest-llm-reaper, and RELEASE THE CARD BEFORE RESTORING IT.
- One PR per chunk, branched from origin/main.

Two things worth knowing that cost me time:
- Squash-merged stacked branches conflict on every file they share. Branch
  from origin/main, not from a merged branch.
- Verify claims by running them. Four bugs last session were found that way
  and none by reading — including a test that asserted idempotency while
  checking only the fields that already had it.
```

---

## Alternative tasks, if the queue is not the priority

**Grow the corpus** (cheapest, and the bottleneck for any real benchmark). Weight
toward no-part-number items and bare component markings; a clean distributor bag
tells you almost nothing. `docs/HANDOFF-vision-and-bench.md` has the commands.

**The anchored benchmark variant** — probably the highest-value single
*experiment* left, because it directly tests whether the barcode anchor repairs
the two single-character misreads. It needs a capture scanned through the PWA so
the browser fills in the regions; `upload_capture` deliberately does not decode.

**Deploy the 30B-A3B VL and the CPU-only embedding pod.** Both were in the
original plan. The embedding pod must be CPU-only: an always-on GPU pod denies an
exclusive card to the co-tenant.
