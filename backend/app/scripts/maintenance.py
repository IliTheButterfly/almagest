"""The nightly cache-maintenance client: check, report, exit.

    python -m app.scripts.maintenance --base-url http://almagest-api:8000

## Why this is an HTTP client and not a database job

The obvious shape for this — a CronJob that imports `app.db.maintenance`, opens
the volume and rebuilds — is the one shape this deployment cannot have. SQLite on
a ReadWriteOnce volume has **exactly one writer**, that writer is the API, and a
rebuild writes. `deploy/base/backup.yaml` avoids the same hazard by opening the
database `mode=ro`; a rebuild has no such option, so it runs *inside* the API
process and this program only asks it to. Same division ADR 0005 draws for the
extraction worker, for the same reason.

Which also means this needs no database credentials, no volume mount, and no
migration-compatible schema — just a URL.

## Why a drift finding exits non-zero

There is no metrics stack here and no alertmanager. The one channel that surfaces
a nightly problem without one is a **failed Job**: it shows up in
`kubectl get jobs`, it is what `failedJobsHistoryLimit` retains, and it is
visible weeks later. Printing a warning and exiting 0 puts a correctness alert in
a log nobody reads.

So drift is a failure by default. The CronJob pairs that with `backoffLimit: 0`,
because drift is a state of the data and retrying the check three times only
records the same finding three times. `--allow-drift` exists for the operator
who is already looking at a known drift and wants the exit code to mean
"the check ran" instead.

## What it does and does not repair

Just the nightly pass: dirty occupancy is rebuilt, balances and reservations are
only checked. `--scrub` is the other job this program can run — re-hashing every
stored blob against its own name — kept out of the nightly pass because it reads
the whole blob store and would otherwise set the pass's duration.

`--rebuild` reaches the repair route, and is a deliberate, operator-run action —
never the schedule. Repairing drift every night erases the symptom and leaves the
write-path bug that caused it, so the wrong numbers come back tomorrow with
nothing recorded.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin

log = logging.getLogger("almagest.maintenance")

#: Generous for a full rebuild over a large ledger; a check that takes this long
#: is a broken link, and failing beats hanging until the next night's run.
DEFAULT_TIMEOUT = 300.0


def post_json(base_url: str, path: str, payload: dict[str, Any], *, timeout: float) -> Any:
    # A trailing slash on the base, or `urljoin` silently drops the last segment
    # of any path the install is mounted under.
    root = base_url if base_url.endswith("/") else f"{base_url}/"
    request = urllib.request.Request(
        urljoin(root, path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def run_check(base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Run the nightly pass. True when every checked cache agreed with its source."""
    body = post_json(base_url, "api/system/maintenance", {}, timeout=timeout)
    log.info("rebuilt occupancy for %s location(s)", body.get("occupancy_rebuilt", 0))

    # Logged at WARNING rather than folded into the exit code, and the distinction
    # is the point. A swept lease is a *repair* that succeeded, so it is not a
    # failure — but a queue with expired leases it could not repair is a queue
    # nothing is draining, and that is invisible everywhere else: the rows read as
    # `claimed`, which from the queue's own depth looks exactly like progress.
    #
    # It does not fail the Job because the drift channel has to keep meaning "a
    # number is wrong". A stopped worker and a wrong balance send whoever reads
    # this to different places, and one exit code cannot say both.
    for sweep in body.get("lease_sweeps", []):
        queue = sweep.get("queue", "?")
        failed, stalled = int(sweep.get("failed", 0)), int(sweep.get("stalled", 0))
        if failed:
            log.info("%s: swept %s abandoned claim(s) to failed", queue, failed)
        if stalled:
            log.warning(
                "%s: %s expired lease(s) with attempts left — nothing is draining this queue",
                queue,
                stalled,
            )
        if not failed and not stalled:
            log.info("%s: no abandoned leases", queue)

    clean = True
    for report in body.get("drift", []):
        name = report.get("cache_name", "?")
        count = int(report.get("drift_count", 0))
        if not count:
            log.info("%s: no drift", name)
            continue
        clean = False
        # The sample is bounded server-side, so this line cannot become a 200k-id
        # log entry however systemic the failure is.
        sample = ", ".join(str(i) for i in report.get("sample_ids", []))
        log.error(
            "%s: %s row(s) disagree with the source of truth (sample: %s)", name, count, sample
        )
    return clean


def run_scrub(base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Re-hash the blob store. True when every blob still hashes to its own name.

    Separate from the nightly pass and separately scheduled, because it reads
    every stored blob in full: folding it into `run_check` would make the pass
    that rebuilds occupancy take as long as the slowest disk in the deployment.

    A finding is a failed Job for the same reason drift is — there is no metrics
    stack here, and a corrupt blob is served as authoritative and cached
    `immutable`, so it is exactly the sort of thing that must not sit in a log
    nobody reads.
    """
    body = post_json(base_url, "api/system/blobs/scrub", {}, timeout=timeout)
    checked = int(body.get("checked", 0))
    missing = list(body.get("missing", []))
    corrupt = list(body.get("corrupt", []))
    log.info("scrubbed %s blob(s)", checked)
    if missing:
        log.error("%s blob(s) missing from the store: %s", len(missing), ", ".join(missing[:20]))
    if corrupt:
        log.error(
            "%s blob(s) do not hash to their own name: %s", len(corrupt), ", ".join(corrupt[:20])
        )
    return not missing and not corrupt


def run_rebuild(base_url: str, caches: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> None:
    body = post_json(base_url, "api/system/caches/rebuild", {"caches": caches}, timeout=timeout)
    for result in body.get("rebuilt", []):
        log.info(
            "rebuilt %s: %s row(s) recomputed",
            result.get("cache_name", "?"),
            result.get("rows_touched", 0),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.maintenance",
        description=(
            "Run Almagest's cache maintenance through the API. Rebuilds occupancy "
            "flagged dirty by triggers, checks balance and reservation caches "
            "against their sources, and exits non-zero if either drifted."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="API root. This program never opens the database — see the module docstring.",
    )
    parser.add_argument(
        "--rebuild",
        metavar="CACHE",
        nargs="*",
        default=None,
        help=(
            "Repair instead of check: reconstruct these caches from their sources "
            "(lot_balances, reservations, location_occupancy; all of them if named "
            "with no arguments). An operator action, never the schedule."
        ),
    )
    parser.add_argument(
        "--scrub",
        action="store_true",
        help=(
            "Re-hash every stored blob against its own name instead of running the "
            "cache pass. Reads the whole blob store, so it gets its own schedule."
        ),
    )
    parser.add_argument(
        "--allow-drift",
        action="store_true",
        help="Exit 0 even when drift is found. For a known, already-triaged drift.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        if args.rebuild is not None:
            run_rebuild(args.base_url, list(args.rebuild), timeout=args.timeout)
            return 0
        if args.scrub:
            clean = run_scrub(args.base_url, timeout=args.timeout)
        else:
            clean = run_check(args.base_url, timeout=args.timeout)
    except urllib.error.HTTPError as error:
        # Distinguished from drift on purpose: 2 is "the check could not run",
        # 1 is "the check ran and found something". An operator reading a failed
        # Job needs to know which, because only one of them is a data problem.
        detail = error.read()[:500].decode(errors="replace")
        log.error("%s %s: %s", error.code, error.reason, detail)
        return 2
    except (urllib.error.URLError, TimeoutError) as error:
        log.error("could not reach %s: %s", args.base_url, error)
        return 2

    if not clean and not args.allow_drift:
        log.error(
            "%s found; failing so this run is visible as a failed Job",
            "blob damage" if args.scrub else "cache drift",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
