# 0013 — The nightly pass repairs staleness and only reports drift

Date: 2026-07-31
Status: Accepted

## Context

`app/db/maintenance.py` has held a complete set of cache rebuilds and drift checks
since the core schema landed. **Nothing outside tests ever called any of it.**

For two of the three caches that was a missing safety net. For the third it was a
live defect. `locations.is_overfull` is written *only* by
`rebuild_location_occupancy`, so with nothing running it:

- the flag was permanently false, and CLAUDE.md's "capacity is advisory — accept
  the put-away, flag the location, generate a defrag suggestion" behaviour could
  never fire;
- `services/assignment.py` scored candidate locations against an always-false
  flag;
- `GET /api/locations/tree` served `fill_ratio` out of `location_occupancy`, a
  table that in production had no rows at all, so the storage map showed no fill
  for any container.

The DB triggers did their half — they mark rows dirty on ledger insert and lot
relocation — and nothing consumed the flag.

## Decision

### The work runs inside the API, reached over HTTP

The obvious shape, a CronJob that imports `app.db.maintenance` and opens the
volume, is the one shape this deployment cannot have. The datastore is SQLite on a
ReadWriteOnce volume with **exactly one writer**, that writer is the API, and a
rebuild writes. `deploy/base/backup.yaml` already contorts around the same hazard
by opening the database `mode=ro`; a rebuild has no equivalent option.

So `POST /api/system/maintenance` and `POST /api/system/caches/rebuild` do the
work in the API process, and `app/scripts/maintenance.py` is an HTTP client of
them — the same division [ADR 0005](0005-extraction-runs-outside-the-api.md) draws
for the extraction worker. The CronJob pod mounts no volume and needs no
credentials.

### Staleness is repaired; drift is only reported

The two halves of the pass are deliberately different operations:

- **`location_occupancy` is rebuilt.** It is *designed* to go stale. Triggers mark
  rows dirty and leave the recompute to a batch precisely so a tree walk is not on
  every ledger write. A dirty row is the mechanism working, so clearing it is
  routine.
- **Lot balances and reserved quantities are only checked.** Both are maintained
  incrementally on every write by `services/ledger.py` and
  `services/reservations.py`. Drift there is not expected staleness — it is a bug
  in a write path.

Rebuilding those two nightly was rejected. It would erase the symptom and leave
the cause, so the wrong numbers would return the next day with nothing recorded,
and the schedule would quietly convert a correctness bug into a permanent
background repair. `POST /api/system/caches/rebuild` is the repair, run
deliberately once someone knows why it drifted.

### Drift exits non-zero

There is no metrics stack and no alertmanager here. The only channel that surfaces
a nightly correctness problem without one is a **failed Job** — visible in
`kubectl get jobs`, retained by `failedJobsHistoryLimit`, and still there a week
later. A warning logged on a successful exit is a correctness alert in a file
nobody opens.

`backoffLimit: 0` pairs with this: drift is a state of the data, not a flake, so a
retry only records the same finding six more times. Exit 1 means the check ran and
found drift; exit 2 means it could not reach the API. An operator needs to tell
those apart, because only one of them is a data problem.

### The MCP server may read this, not run it

`check_caches` is exposed so an agent can answer "are these quantities
trustworthy?" — every quantity it reports comes from the cache. Running the pass
and rebuilding are `Reason.MACHINE_DOOR`: the pass is `concurrencyPolicy: Forbid`
because a second concurrent caller contends with the single writer, and a rebuild
destroys the evidence that a write path is broken.

## Consequences

- Occupancy, `is_overfull` and the map's fill ratios become correct within a day
  of any change, rather than never.
- A drifting balance cache is now loud. The first run against real data may fail,
  and that failure is information, not a regression in this change.
- Nothing here rebuilds `location_tree` or `category_tree`. Those are maintained
  by `services/tree.py` on the writes that invalidate them and have **no drift
  check to pair with**, so they are deliberately absent from the rebuild route
  rather than silently included — offering them would imply a check that does not
  exist. Writing those two checks is follow-up work.
- The occupancy rebuild is `only_dirty=True` nightly. A row that is wrong *without*
  having been flagged is not reached by the schedule; the unconditional rebuild
  behind `POST /caches/rebuild` is what covers that case.
