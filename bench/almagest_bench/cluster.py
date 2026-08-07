"""Getting one model onto the card, keeping it there, and giving the card back.

Three jobs, and the third is the one that matters most to everybody who is not
running the benchmark.

## The reaper will take the GPU out from under a long run

`deploy/base/llm-reaper.yaml` runs every ten minutes and scales the model
deployments to zero once **chat** has been idle for `IDLE_MINUTES` (45, as
deployed). A benchmark touches no chat threads, so from the reaper's point of view
it is idle from the moment it starts. It will be reaped mid-run, indefinitely.

Three ways to survive that were considered:

* *Poke a chat thread every ten minutes.* Writes rows into the live inventory
  database to keep a benchmark alive. Rejected outright -- isolation is the
  harness's whole premise.
* *Re-scale after each reap.* Costs a full weight load every ten minutes, which
  would dominate the measurement it is protecting.
* **Suspend the CronJob for the duration and restore it in a `finally`.** Chosen.

## Why suspending is safe enough to do

The obvious objection is that a crashed harness leaves the reaper off and the co-
tenant starved. That objection is answered by ordering rather than by hoping:
**`release_all()` runs before the restore**, so by the time anything could go
wrong with the restore, every model deployment is already at zero and the card is
already free. A stuck-suspended reaper then costs the co-tenant nothing -- it has
nothing left to reap.

Belt and braces on top of that: `atexit` and signal handlers rather than only a
`finally`, a token file on disk so a stale suspension is discoverable after the
fact, and `almagest-bench cluster release` as the one command that puts everything
back.

## `kubectl`, not a Kubernetes client library, and not `model_scaler`

The no-client-library half is the argument `model_scaler.py` already makes.

Not reusing `model_scaler` itself is the part worth explaining: it authenticates
with the **in-pod service account** (`/var/run/secrets/kubernetes.io/...`), which
does not exist on a laptop. This harness is not deployed. It runs against the
maintainer's own kubeconfig, needs no ServiceAccount, no Role and no RBAC change,
and `kubectl -n ili auth can-i patch cronjobs` was verified to answer yes before
this was written.

The readiness probe here is its own few lines for a related but narrower reason.
`model_catalog.probe` short-circuits to `False` whenever `ALMAGEST_LLM_BASE_URL`
is unset -- this harness's normal state -- which would make every model look
permanently down.

**ADR 0020 has since landed a `probe_server(..., force=True)` that skips exactly
that short-circuit, so this could now delegate.** It deliberately does not, for
one reason: `probe_server` lives beside `model_servers.start()`, and *that* is
the half this module genuinely cannot use. Importing one and reimplementing the
other would be the confusing arrangement. If the scaling ever moves off the
in-pod service account, delete `is_serving` and call `probe_server(force=True)`
instead -- the logic below is copied from it faithfully and the two should not
drift in the meantime.

That logic, in one line: ask `/v1/models` and look for the served name, never
trust a bare TCP connect, because the 4B and 8B share one Ollama listener and a
connect answers True for a model that was never pulled.
"""

from __future__ import annotations

import atexit
import json
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from urllib.parse import urlparse

from app.services import model_catalog

#: The CronJob that would otherwise reap the GPU mid-run.
REAPER = "almagest-llm-reaper"

#: How often to ask whether the model is serving yet. Ten seconds: a weight load
#: is measured in minutes, and a tighter loop only adds API calls.
POLL_SECONDS = 10.0

#: How long a server may sit with a replica requested and nothing listening before
#: it is called out as stuck. Not a failure by itself -- a large model legitimately
#: takes this long -- but past it the run should say so rather than look hung.
QUIET_START_SECONDS = 600.0


class ClusterError(RuntimeError):
    """A kubectl call failed. Never swallowed: this module's whole job is to
    leave the cluster in a known state, and a silent failure here is how it does
    the opposite."""


def _kubectl(*args: str, namespace: str, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "-n", namespace, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise ClusterError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


@dataclass(frozen=True)
class SwapOutcome:
    """What it took to get this model answering."""

    model_id: str
    base_url: str
    #: `already_ready` | `swapped` | `pending_or_starting` | `preempted`
    outcome: str
    ready_seconds: float
    released: tuple[str, ...] = ()
    note: str | None = None

    @property
    def usable(self) -> bool:
        return self.outcome in ("already_ready", "swapped")


# ---------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------


def _token_path(run_dir: Path) -> Path:
    return run_dir / "reaper-suspended.token"


def set_reaper_suspended(suspended: bool, *, namespace: str) -> None:
    body = json.dumps({"spec": {"suspend": suspended}})
    _kubectl("patch", "cronjob", REAPER, "-p", body, namespace=namespace)


def reaper_is_suspended(*, namespace: str) -> bool:
    out = _kubectl("get", "cronjob", REAPER, "-o", "jsonpath={.spec.suspend}", namespace=namespace)
    return out.strip() == "true"


@contextmanager
def reaper_suspended(run_dir: Path, *, namespace: str, dry_run: bool = False) -> Iterator[None]:
    """Hold the reaper off for the run, and put it back whatever happens.

    The token file is not bookkeeping for its own sake: it is what makes a
    suspension that outlived its harness *discoverable* by somebody who was not
    watching. `almagest-bench cluster status` reads it and complains.
    """
    if dry_run:
        yield
        return

    token = _token_path(run_dir)
    restored = False

    def restore(*_: object) -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        # Order is the safety property: the card goes back first, so a failure
        # in the line below leaves a suspended reaper with nothing to reap.
        try:
            release_all(namespace=namespace)
        finally:
            try:
                set_reaper_suspended(False, namespace=namespace)
                token.unlink(missing_ok=True)
            except (ClusterError, OSError):
                # Deliberately not re-raised during interpreter shutdown; the
                # token file left behind is the report.
                pass

    def on_signal(signum: int, frame: FrameType | None) -> None:
        restore()
        raise KeyboardInterrupt(f"interrupted by signal {signum}")

    set_reaper_suspended(True, namespace=namespace)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(
        f"suspended by almagest-bench at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"restore with: almagest-bench cluster release\n",
        encoding="utf-8",
    )
    atexit.register(restore)
    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        restore()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stale_suspension(run_root: Path) -> list[Path]:
    """Token files from runs that are no longer running. Reported, not deleted."""
    return sorted(run_root.glob("*/reaper-suspended.token"))


# ---------------------------------------------------------------------------
# Getting a model onto the card
# ---------------------------------------------------------------------------


def deployments() -> dict[str, str]:
    """Base URL -> the Deployment that serves it.

    Derived from the catalogue rather than written out, so a model added there
    with a new base URL does not silently fail to be scalable here. The name is
    the service hostname, which by convention is also the Deployment name.
    """
    mapping = {}
    for choice in model_catalog.CATALOG:
        host = urlparse(choice.base_url).hostname
        if host:
            mapping[choice.base_url] = host
    return mapping


def scale(deployment: str, replicas: int, *, namespace: str) -> None:
    _kubectl("scale", f"deployment/{deployment}", f"--replicas={replicas}", namespace=namespace)


def release_all(*, namespace: str) -> tuple[str, ...]:
    """Scale every Almagest model server to zero. The card goes back.

    Never raises past itself. This runs on the way out of a crashed run, and a
    failure to release one deployment must not stop the others being released.
    """
    released = []
    for deployment in sorted(set(deployments().values())):
        try:
            scale(deployment, 0, namespace=namespace)
            released.append(deployment)
        except ClusterError:
            continue
    return tuple(released)


def is_serving(choice: model_catalog.ModelChoice, timeout: float = 2.0) -> bool:
    """Is this exact model answering right now?

    Asks `/v1/models` and looks for the served name. **Not a bare TCP connect**:
    the 4B and 8B share one Ollama listener, so a connect answers True for a model
    that was never pulled, and the run would then spend its slice getting 404s.

    `model_catalog.probe` does exactly this and cannot be reused -- it returns
    False whenever `ALMAGEST_LLM_BASE_URL` is unset, which is this harness's
    normal state. See the module docstring.
    """
    parsed = urlparse(choice.base_url)
    host, port = parsed.hostname, parsed.port
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return False
    try:
        request = urllib.request.Request(choice.base_url.rstrip("/") + "/v1/models")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        # Listening but not answering: still loading weights. Deliberately not
        # "ready" -- that is the state a run must not start measuring in.
        return False
    served = {str(row.get("id", "")) for row in body.get("data", [])}
    return choice.served_name in served


def swap_to(
    choice: model_catalog.ModelChoice,
    *,
    namespace: str,
    deadline_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> SwapOutcome:
    """Make this model the one holding the GPU, or give up inside the deadline.

    Gives up rather than blocking forever, and that is the important behaviour: a
    large model's first start is a multi-gigabyte download, and a run that hangs
    on it burns the night holding a card it never used. `preempted` lets the
    caller skip that slice and finish on the models that do work -- a partial
    answer at 06:00 beats a hung harness.

    Every *other* model server is scaled to zero first, unconditionally. The card
    is integral and exclusive (ADR 0016), so this is not tidiness: without it the
    new pod sits Pending behind our own previous one.
    """
    if is_serving(choice):
        return SwapOutcome(choice.id, choice.base_url, "already_ready", 0.0)

    mapping = deployments()
    target = mapping.get(choice.base_url)
    if target is None:
        raise ClusterError(f"no deployment known for {choice.base_url}")

    released = []
    for deployment in sorted(set(mapping.values())):
        if deployment != target:
            scale(deployment, 0, namespace=namespace)
            released.append(deployment)
    scale(target, 1, namespace=namespace)

    started = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - started
        if is_serving(choice):
            return SwapOutcome(choice.id, choice.base_url, "swapped", elapsed, tuple(released))
        if elapsed > deadline_seconds:
            # Hand the card back rather than leave a wedged pod holding it.
            scale(target, 0, namespace=namespace)
            return SwapOutcome(
                choice.id,
                choice.base_url,
                "preempted",
                elapsed,
                tuple(released),
                note=(
                    f"not serving within {deadline_seconds:.0f}s; slice skipped. "
                    "This namespace cannot read nodes, so whether the cause was our "
                    "own download or another namespace holding the card is not "
                    "answerable from here."
                ),
            )
        time.sleep(poll_seconds)


def swap_count(choices: Sequence[model_catalog.ModelChoice]) -> int:
    """How many GPU handovers a plan costs, in the order it is written.

    Adjacent slices on the same base URL are free of a *cluster* swap -- the 4B
    and 8B share one Ollama deployment, so moving between them is a weight reload
    rather than a rollout. Counting by base URL rather than by model is what makes
    "put the two Ollama models next to each other" visibly worth doing.
    """
    swaps = 0
    previous: str | None = None
    for choice in choices:
        if previous is not None and choice.base_url != previous:
            swaps += 1
        previous = choice.base_url
    return swaps
