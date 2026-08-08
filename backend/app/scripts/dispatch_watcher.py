"""The watcher: queued photographs read themselves, and the card goes back after.

    python -m app.scripts.dispatch_watcher --base-url http://almagest-api:8000

ADR 0021 left the queue with no scheduler on purpose — a read costs a **GPU handover**
on a card that is integral, exclusive and co-tenanted (ADR 0016), so nothing was going
to drain it on a timer. The consequence was that pressing "Ask a model to read it" put
an entry in `pending` and nothing ever came for it.

This is the mechanical half automated with the **decision left where it was**: a person
still asks per photograph. Nothing in this module writes `dispatch_state`, and there is
no call here that could reach `POST /api/dispatch/requests` — see
`tests/unit/test_dispatch_watcher.py`, which asserts both.

## Shape: a Deployment that polls, not a CronJob

A CronJob would be the cheaper thing to write and the wrong thing to run. The drain has
to hold state across its own lifetime — *did I start this model server, or was it
already up?* — because the answer decides whether releasing the card is a courtesy or a
theft (see `Card.release`). A Job that dies mid-drain loses that answer and either pins
the GPU or kills somebody's chat model.

So: one replica, CPU only, `POLL_SECONDS` between polls. **Idle cost is one HTTP call
to our own API and nothing else** — no cluster call, no probe, no GPU. That is what
keeps ADR 0016's "do not hold the card 24/7" true with this running.

## It waits for the card. It never evicts.

`model_servers.start()` releases every *other* server first, unconditionally, which is
right for a person pressing Start and wrong for a background drain: the thing it would
release is a model somebody is talking to. So this **never calls `start()`**. It reads
`statuses()`, and if any other server holds the card it logs that and backs off.

The accepted consequence, stated plainly because it is a real one: **a queued
photograph can wait behind a chat model indefinitely.** There is no deadline after
which this takes the card. It is logged on every deferral so the wait is visible rather
than looking hung.

## Releasing only what it started

The Ollama server holds the chat models *and* the vision model. If it was already up
when a drain began, some person is very likely mid-conversation with it, and scaling it
to zero on the way out would end that conversation to save a GPU that was already
spent. So `Card` records whether *this* drain asked for the server, and releases only in
that case; otherwise the reaper's idle timer does its ordinary job.

## The reaper is taught about drains rather than suspended

The reaper measures **chat** activity, and a vision run touches no chat thread — so
left alone it scales the model to zero mid-drain. Observed during a benchmark, not
theorised.

Suspend-and-restore is the fragile fix: a crashed watcher leaves the reaper suspended,
which is the bad state, and that exact ordering has already failed in this repository.
So the reaper keeps being the single authority on releasing the card and simply asks one
more question first — `GET /draining` on this process, over the Service in
`deploy/base/dispatch-watcher.yaml`.

**That flag is bounded, or it would be a GPU leak.** `DrainFlag` answers true only while
a drain is *making progress*: every claim, submission and failure report refreshes a
heartbeat, and once `STALL_SECONDS` pass with no activity the flag reads false again even
though the drain has not returned. A wedged drain therefore stops holding the card off,
and a dead pod stops answering at all — which the reaper reads as "no drain", because its
own documented bias is toward releasing.

`GET /api/dispatch/status` is deliberately *not* what the reaper asks. `pending > 0` is
not a reason to hold a GPU — a queue nobody is draining is exactly the case where the
card should go back — and the route reports queue depth by state without saying whether
any claim's lease is still live, so it cannot express "a drain is in flight" at all.
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from app.scripts import dispatch_captures
from app.scripts.dispatch_captures import ApiClient, QueuedCapture
from app.services import model_scaler, model_servers
from app.services.enrichment.vision import VisionProvider

log = logging.getLogger("almagest.dispatch.watcher")

#: Seconds between polls of queue depth. Fifteen: fast enough that pressing the button
#: feels like it did something, and cheap enough that the resting cost is one grouped
#: count over an indexed column — which is what `GET /api/dispatch/status` already is.
DEFAULT_POLL_SECONDS = 15.0

#: How long a model server gets to answer after being asked for. Ollama loads an 8B in
#: well under a minute from a warm image; this allows for a cold pull. On expiry the
#: server this drain started is released again rather than left up, because a card held
#: by something that will not serve is the worst of both.
DEFAULT_READY_SECONDS = 600.0

#: How often readiness is re-probed while waiting. Not the poll interval: this is inside
#: a wait somebody is already paying for.
DEFAULT_READY_POLL_SECONDS = 5.0

#: How long a drain may go without activity before `DrainFlag` stops vouching for it.
#: ADR 0021 measured a hard read at 39 s of inference, so ten minutes of silence is a
#: wedged worker rather than a slow one — and the bound is what stops a wedged drain
#: pinning the GPU. See the module docstring.
DEFAULT_STALL_SECONDS = 600.0

#: Where the drain flag is served. Not 8000: this is not the API and must never be
#: mistaken for it.
DEFAULT_FLAG_PORT = 9099

#: Backoff bounds for the two things worth backing off from — a card another server
#: holds, and an API that is not answering. Capped so a long wait stays a poll rather
#: than becoming a silence.
BACKOFF_START_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 300.0


class Outcome(StrEnum):
    """What one pass of the loop did. Returned so the tests can assert on it."""

    #: Nothing queued. No cluster call was made and no GPU was touched.
    IDLE = "idle"
    #: The API did not answer. Backs off; says nothing about the GPU.
    UNREACHABLE = "unreachable"
    #: Another model server holds the card. Deferred — never evicted.
    DEFERRED = "deferred"
    #: Asked for the server and it did not come up in time. Released again.
    NOT_READY = "not_ready"
    #: A drain ran. The card is back if this drain was the one that took it.
    DRAINED = "drained"


# ---------------------------------------------------------------------------
# The flag the reaper reads
# ---------------------------------------------------------------------------


class DrainFlag:
    """Is a drain in flight *and making progress*? The reaper's one extra question.

    Progress rather than mere existence, and that is the whole design: an unbounded
    "a drain started" flag is a GPU leak wearing a helpful face, because the process
    holding it may be wedged inside a model call that will never return. Every claim and
    every submission calls `touch`, and `STALL_SECONDS` without one puts the flag back to
    false while leaving `begin`/`end` accounting intact — so the drain is still allowed
    to finish, it just stops being a reason to hold the card.

    A monotonic clock, so a container's wall clock stepping does not resurrect or expire
    a drain.
    """

    def __init__(
        self,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stall_seconds = stall_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self._touched_at: float | None = None
        self._drains = 0

    def begin(self) -> None:
        with self._lock:
            now = self._clock()
            self._started_at = now
            self._touched_at = now
            self._drains += 1

    def touch(self) -> None:
        with self._lock:
            if self._started_at is not None:
                self._touched_at = self._clock()

    def end(self) -> None:
        with self._lock:
            self._started_at = None
            self._touched_at = None

    def snapshot(self) -> dict[str, Any]:
        """The JSON body. `draining` is the only field the reaper's script matches on.

        The rest is for a person reading `wget -qO- .../draining` by hand: `stalled` is
        what distinguishes "no drain" from "a drain nobody should wait for any longer",
        which is precisely the state that would otherwise be invisible.
        """
        with self._lock:
            started, touched = self._started_at, self._touched_at
            drains = self._drains
            stall = self._stall_seconds
        if started is None or touched is None:
            return {"draining": False, "stalled": False, "drains": drains}
        idle_for = self._clock() - touched
        stalled = idle_for > stall
        return {
            "draining": not stalled,
            "stalled": stalled,
            "drains": drains,
            "running_for_seconds": round(self._clock() - started, 1),
            "idle_for_seconds": round(idle_for, 1),
            "stall_seconds": stall,
        }


def _handler(flag: DrainFlag) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # One path, and `/` aliased onto it so a person poking the port by hand is told
        # something rather than getting a 404 from their own service.
        def do_GET(self) -> None:
            if self.path.rstrip("/") not in ("", "/draining"):
                self.send_error(404)
                return
            # Compact separators, because the reaper matches this body with a shell
            # `case` pattern (`*'"draining":true'*`). It strips whitespace before
            # matching as well — belt and braces on a match that, if it ever stopped
            # working, would fail by reaping a live drain rather than by erroring.
            body = json.dumps(flag.snapshot(), separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            # The reaper polls this every ten minutes and a readiness probe rather more
            # often. Logged at debug so the pod's log stays the drain's story.
            log.debug("flag: " + fmt, *args)

    return Handler


def serve_flag(flag: DrainFlag, *, port: int = DEFAULT_FLAG_PORT) -> ThreadingHTTPServer:
    """Answer `GET /draining` on a daemon thread. Returns the server so a test can close it."""
    server = ThreadingHTTPServer(("", port), _handler(flag))
    threading.Thread(target=server.serve_forever, name="drain-flag", daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardState:
    """What the cluster says about the card, in the three terms the loop needs."""

    #: Can this install scale anything? False on a dev box and with no RBAC.
    controllable: bool
    #: Our server has `desired >= 1` — it holds the device even while loading weights.
    ours_up: bool
    #: Our server answered and named at least one model. The readiness signal.
    ours_serving: bool
    #: Ids of *other* servers holding the card. Non-empty means defer.
    held_by_others: tuple[str, ...]


class Card(Protocol):
    """The three things the loop does to a GPU. A Protocol so tests need no cluster."""

    def state(self) -> CardState: ...

    def ask_for_it(self) -> bool: ...

    def release(self) -> bool: ...


class ClusterCard:
    """`Card` over the existing model-server control. **No second scaler.**

    `model_servers.start()` is deliberately unused. It releases every other server
    first, which is correct for a person pressing Start — they want the card now — and
    is exactly the eviction a background drain must refuse. So the scale-up is
    `model_scaler.set_replicas(host, 1)`, which is the one call `start()` makes after
    the eviction it adds.

    `release` goes back through `model_servers.stop()`, which touches only the named
    server, and **only if this object asked for the server in the first place**. See
    the module docstring on why leaving a server somebody else started is the right
    default.
    """

    def __init__(self, server: model_servers.ModelServer) -> None:
        self.server = server
        self._we_started_it = False

    @property
    def we_started_it(self) -> bool:
        return self._we_started_it

    def state(self) -> CardState:
        ours_up = False
        ours_serving = False
        others: list[str] = []
        for status in model_servers.statuses():
            if status.server.id == self.server.id:
                ours_up = status.holds_gpu
                ours_serving = status.state is model_servers.ServerState.RUNNING
            elif status.holds_gpu:
                others.append(status.server.id)
        return CardState(
            controllable=model_servers.controllable(),
            ours_up=ours_up,
            ours_serving=ours_serving,
            held_by_others=tuple(others),
        )

    def ask_for_it(self) -> bool:
        if not model_scaler.set_replicas(self.server.host, 1):
            return False
        self._we_started_it = True
        return True

    def release(self) -> bool:
        if not self._we_started_it:
            # Nothing to give back. Debug rather than info deliberately: this is also
            # the path a *deferral* takes, where the card was never ours and an
            # "leaving it up" line would read as though a decision had been made about
            # somebody else's model. The two cases that are worth telling a person
            # about — using a server found already up, and deferring to another one —
            # are both logged where the decision is actually taken, in `_acquire`.
            log.debug("nothing to release: this drain never took the card")
            return False
        self._we_started_it = False
        result = model_servers.stop(self.server)
        log.info("released the card: %s", result.detail)
        return result.ok


def server_for_model_url(url: str) -> model_servers.ModelServer | None:
    """Which known server answers on this base URL, matched on hostname.

    Matched here rather than by adding a lookup to `model_servers`: the watcher is told
    a model URL on the command line exactly as the worker is, and the vision model is
    not in `model_catalog.CATALOG` — it is served by the same Ollama listener the chat
    models are, which is precisely why a drain may find that server already up.
    """
    host = urlparse(url).hostname or url
    for server in model_servers.SERVERS:
        if server.host == host:
            return server
    return None


# ---------------------------------------------------------------------------
# Queue depth
# ---------------------------------------------------------------------------


class QueueStatus(Protocol):
    """The one read the idle path makes. Its own Protocol so the loop needs no HTTP."""

    def pending_count(self) -> int | None: ...


class HttpQueueStatus:
    """`GET /api/dispatch/status`, over `urllib`, returning `pending` or None.

    None means the API did not answer, which is deliberately not zero: zero would send
    the loop to sleep as though the queue were empty, and an API that is restarting
    would look like a queue nobody had asked anything of.
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self.timeout = timeout

    def pending_count(self) -> int | None:
        request = urllib.request.Request(
            urljoin(self.base_url, "api/dispatch/status"), method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body: Any = json.loads(response.read() or b"{}")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
            log.warning("could not read queue depth: %s", error)
            return None
        if not isinstance(body, dict) or "pending" not in body:
            log.warning("queue depth response carried no `pending` field")
            return None
        return int(body["pending"])


class HeartbeatClient:
    """An `ApiClient` that refreshes the drain flag on every call it forwards.

    A wrapper rather than a callback threaded through `dispatch_captures.run`, because
    the worker's four calls already *are* the progress signal: a claim granted or a
    result submitted is the only evidence that the drain is moving, and inventing a
    second notion of progress alongside them would be a thing that could disagree.
    """

    def __init__(self, inner: ApiClient, flag: DrainFlag) -> None:
        self._inner = inner
        self._flag = flag

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedCapture]:
        self._flag.touch()
        return self._inner.claim(worker_id=worker_id, limit=limit)

    def fetch_image(self, sha256: str) -> bytes:
        self._flag.touch()
        return self._inner.fetch_image(sha256)

    def create_stub_part(
        self, *, name: str, mpn: str, client_op_id: str, device_id: str
    ) -> int | None:
        self._flag.touch()
        return self._inner.create_stub_part(
            name=name, mpn=mpn, client_op_id=client_op_id, device_id=device_id
        )

    def submit_candidates(
        self,
        *,
        intake_id: int,
        candidates: Sequence[dict[str, Any]],
        label_kind: str | None,
    ) -> None:
        self._flag.touch()
        self._inner.submit_candidates(
            intake_id=intake_id, candidates=candidates, label_kind=label_kind
        )

    def submit_failure(self, *, intake_id: int, error: str) -> None:
        self._flag.touch()
        self._inner.submit_failure(intake_id=intake_id, error=error)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class _Backoff:
    """Doubling delay with a ceiling, reset by any pass that got somewhere."""

    def __init__(
        self, start: float = BACKOFF_START_SECONDS, ceiling: float = BACKOFF_MAX_SECONDS
    ) -> None:
        self._start = start
        self._ceiling = ceiling
        self.delay = start

    def reset(self) -> None:
        self.delay = self._start

    def widen(self) -> float:
        current = self.delay
        self.delay = min(self.delay * 2, self._ceiling)
        return current


@dataclass
class Watcher:
    """Poll, drain, release. One instance per process.

    Every collaborator is injected — the queue read, the card, the provider, the worker
    client and even `sleep` — so the whole of this runs offline in a unit test with no
    cluster, no GPU and no clock.
    """

    status: QueueStatus
    card: Card
    client: ApiClient
    provider: VisionProvider
    worker_id: str
    flag: DrainFlag
    limit: int = dispatch_captures.DEFAULT_LIMIT
    poll_seconds: float = DEFAULT_POLL_SECONDS
    ready_seconds: float = DEFAULT_READY_SECONDS
    ready_poll_seconds: float = DEFAULT_READY_POLL_SECONDS
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self._backoff = _Backoff()
        self._warned_uncontrollable = False

    # -- the card -----------------------------------------------------------

    def _wait_ready(self) -> bool:
        """Poll until our server is serving, or the allowance runs out."""
        deadline = self.clock() + self.ready_seconds
        while True:
            if self.card.state().ours_serving:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(self.ready_poll_seconds)

    def _acquire(self) -> Outcome | None:
        """Get the card, or say why not. None means "go ahead and drain"."""
        state = self.card.state()

        if state.held_by_others:
            # **The refusal that matters.** `model_servers.start()` would take the card
            # from these; a drain must not. Logged every time, because an indefinite
            # wait that says nothing is indistinguishable from a hang.
            log.info(
                "%s hold(s) the GPU; deferring the drain rather than evicting a model "
                "somebody may be using. Retrying in %.0fs",
                ", ".join(state.held_by_others),
                self._backoff.delay,
            )
            return Outcome.DEFERRED

        if not state.controllable:
            # Not in the cluster, or no RBAC. Degrade rather than crash: whatever is
            # already servable is still drainable, and on a dev box with a port-forward
            # that is the normal case.
            if not self._warned_uncontrollable:
                self._warned_uncontrollable = True
                log.warning(
                    "cannot start or stop model servers here (not in the cluster, or no "
                    "permission to scale). Draining against whatever is already serving"
                )
            return None

        if state.ours_up:
            # Already up, and not by us. Used as found and left as found — see
            # `ClusterCard.release`.
            log.info(
                "the model server is already up; draining against it and leaving it as "
                "found, since something else very likely started it"
            )
            return None

        log.info("pending photographs and a free card: asking for the vision model server")
        if not self.card.ask_for_it():
            log.warning("the cluster refused the scale-up; backing off")
            return Outcome.NOT_READY
        if not self._wait_ready():
            log.warning(
                "the model server did not start serving within %.0fs; releasing the card "
                "rather than holding one that will not answer",
                self.ready_seconds,
            )
            return Outcome.NOT_READY
        return None

    # -- one pass -----------------------------------------------------------

    def tick(self) -> Outcome:
        """One poll. Touches nothing at all when the queue is empty.

        The order is load-bearing: **queue depth first**, so an idle watcher makes no
        cluster call and no probe. Reading the card first would turn "idle" into a
        Kubernetes API call every fifteen seconds forever.
        """
        pending = self.status.pending_count()
        if pending is None:
            return Outcome.UNREACHABLE
        if pending == 0:
            self._backoff.reset()
            return Outcome.IDLE

        log.info("%d photograph(s) waiting to be read", pending)
        # Raised before the card is asked for, not after: the reaper's next run may land
        # between the scale-up and the first claim, and that window is exactly the one
        # this flag exists to cover.
        self.flag.begin()
        try:
            deferred = self._acquire()
            if deferred is not None:
                return deferred
            proposed = dispatch_captures.run(
                HeartbeatClient(self.client, self.flag),
                self.provider,
                worker_id=self.worker_id,
                limit=self.limit,
                # Drain to empty and return: one model load for the whole queue, which
                # is ADR 0021's own reason for the drain posture.
                poll_seconds=0.0,
                max_batches=None,
            )
            log.info("drain finished: %d photograph(s) produced a proposal", proposed)
            return Outcome.DRAINED
        finally:
            # **Release first, then anything else** — the ordering this repository
            # insists on and `bench/almagest_bench/cluster.py` implements. A failure
            # anywhere above must not be able to leave the card held, so this runs on
            # the success path, the exception path and the signal path alike.
            self.release_now()
            self.flag.end()

    def release_now(self) -> None:
        """Give the card back, never raising. Safe to call twice and on the way out."""
        try:
            self.card.release()
        except Exception as error:  # a cluster client's failures are not enumerable
            log.error("could not release the card: %s", error)

    # -- forever ------------------------------------------------------------

    def run(self, *, max_ticks: int | None = None) -> int:
        """Poll until asked to stop. Returns how many passes ran.

        `max_ticks` is what the tests bound the loop with; the deployment passes None.
        """
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            ticks += 1
            try:
                outcome = self.tick()
            except Exception as error:  # one bad pass must not end the watcher
                # `tick`'s own `finally` has already released the card by the time this
                # runs, which is why this can afford to simply carry on.
                log.exception("dispatch watcher pass failed: %s", error)
                self.sleep(self._backoff.widen())
                continue

            if outcome in (Outcome.DEFERRED, Outcome.NOT_READY, Outcome.UNREACHABLE):
                self.sleep(self._backoff.widen())
                continue
            self._backoff.reset()
            self.sleep(self.poll_seconds)
        return ticks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.dispatch_watcher",
        description=(
            "Drain the capture-dispatch queue when it has work, bringing the vision "
            "model up on demand and releasing the GPU afterwards (ADR 0021). Waits for "
            "the card; never evicts another model server."
        ),
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="API root.")
    parser.add_argument(
        "--worker-id", default=None, help="Recorded on the lease. Defaults to the hostname."
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="Replay recorded model responses instead of calling one. Needs no GPU.",
    )
    parser.add_argument("--model-url", default="http://almagest-llm:11434")
    parser.add_argument("--model", default="qwen3-vl:8b")
    parser.add_argument("--wire", default=None, choices=("ollama_native", "openai_content_parts"))
    parser.add_argument("--timeout", type=float, default=dispatch_captures.DEFAULT_TIMEOUT)
    parser.add_argument("--limit", type=int, default=dispatch_captures.DEFAULT_LIMIT)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--ready-seconds", type=float, default=DEFAULT_READY_SECONDS)
    parser.add_argument("--stall-seconds", type=float, default=DEFAULT_STALL_SECONDS)
    parser.add_argument("--flag-port", type=int, default=DEFAULT_FLAG_PORT)
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Stop after this many polls. For a smoke run; the Deployment passes nothing.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    server = server_for_model_url(args.model_url)
    if server is None:
        # A model URL that maps to no known Deployment would silently become a watcher
        # that can never start anything, which is worse than not starting.
        log.error(
            "%s is not one of the known model servers (%s); nothing here could start it",
            args.model_url,
            ", ".join(s.base_url for s in model_servers.SERVERS),
        )
        return 2

    try:
        provider = dispatch_captures.build_provider(args)
    except Exception as error:
        # Same posture as the worker: a missing model is a deployment error, and finding
        # it out before the first claim is what keeps it from spending GPU handovers.
        log.error("no vision provider: %s", error)
        return 2

    flag = DrainFlag(stall_seconds=args.stall_seconds)
    serve_flag(flag, port=args.flag_port)

    watcher = Watcher(
        status=HttpQueueStatus(args.base_url),
        card=ClusterCard(server),
        client=dispatch_captures.HttpApiClient(args.base_url, timeout=args.timeout),
        provider=provider,
        worker_id=args.worker_id or socket.gethostname()[:64],
        flag=flag,
        limit=args.limit,
        poll_seconds=args.poll_seconds,
        ready_seconds=args.ready_seconds,
    )

    # Belt and braces over `tick`'s `finally`, and in `cluster.py`'s order: the card
    # goes back before anything else, and the handler is installed so a `SIGTERM` from
    # a rolling update does not leave a GPU held by a pod that is gone.
    atexit.register(watcher.release_now)

    def on_signal(signum: int, frame: FrameType | None) -> None:
        del frame
        log.info("signal %d: releasing the card and exiting", signum)
        watcher.release_now()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)

    watcher.run(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
