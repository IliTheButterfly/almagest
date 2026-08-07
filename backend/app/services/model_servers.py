"""Which model servers exist, what each is doing, and turning one on or off.

## Why this is a layer of its own

Three modules divide the job, and the seam matters:

* `model_catalog` — **which models a person may pick**, and asking a server what it
  currently serves. No cluster, no side effects.
* `model_scaler` — **the cluster call**: read or set one Deployment's replicas.
  Knows nothing about models.
* here — **the join**, and the only place that knows starting one model server
  means stopping another.

## A server is the unit you can start, not a model

The picker offers three models; there are two servers. Ollama holds the 4B and the
8B and swaps between them on demand, so "start the 8B" and "start the 4B" are the
same cluster action, and stopping that server takes both away. Anything that let
somebody stop "the 4B" would either be a lie or would silently take the 8B with
it, so **the control is per server and says which models it holds.**

## Starting one releases the others, because the card is single

Both servers request `nvidia.com/gpu: 1`. Scaling the 27B up while Ollama is still
up leaves it `Pending` with `Insufficient nvidia.com/gpu` — observed directly, see
`model_catalog`'s docstring — which in a UI reads as "I pressed start and nothing
happened, forever". So `start()` scales every *other* known server to zero first,
exactly as `make k8s-model` does, and reports what it released so the person is
told rather than surprised.

It does **not** wait for the released pod to exit, because a request cannot: the
old pod's shutdown and the new one's weight load are minutes of work. The status
view is the second half — it distinguishes `starting` from `running`, so a page
that polls shows the handover happening instead of guessing.

## Stopping is a real button, not just a timeout

The reaper already releases the GPU on idle, so an explicit Stop might look
redundant. It is not: idle is measured in tens of minutes, and the reason to stop
a model is usually that **somebody else needs the card now** — a co-tenant build,
or the other model. Waiting out a timer is not an answer to that.

## Everything degrades to a read-only view

On a dev box there is no ServiceAccount token, so nothing here can scale anything.
That is reported once, as `controllable = false`, and start/stop refuse with the
`make k8s-model` command that does work there. The status list still renders — what
is running is answered by probing the servers, which needs no cluster rights at
all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

from app.services import model_catalog, model_scaler
from app.services.model_catalog import OLLAMA, VLLM_27B, ModelChoice


class ServerState(StrEnum):
    """What a model server is doing, in the four states worth telling apart."""

    #: Answering, and it named at least one model it can serve.
    RUNNING = "running"
    #: Asked for, but not serving yet — a pod pulling an image, or vLLM loading
    #: weights and compiling CUDA graphs. Minutes, not seconds.
    STARTING = "starting"
    #: Deliberately at zero replicas. The normal resting state.
    STOPPED = "stopped"
    #: Nothing is answering and this install cannot read the cluster, so "off" and
    #: "not deployed here" cannot be distinguished. Said plainly rather than
    #: guessed at.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelServer:
    """One Deployment, and the models it can serve."""

    id: str
    label: str
    base_url: str
    #: The hostname `model_scaler` maps to a Deployment name.
    host: str
    models: tuple[ModelChoice, ...]

    @property
    def deployment(self) -> str | None:
        """The Deployment behind it, or None if it is not one this may touch."""
        return model_scaler.DEPLOYMENT_FOR_HOST.get(self.host)


#: Label and id per server base URL. Only the naming lives here; *which models*
#: each holds is grouped out of `model_catalog.CATALOG` below, so adding a model
#: to the catalogue cannot leave this list stale.
_NAMES: dict[str, tuple[str, str]] = {
    OLLAMA: ("ollama", "Ollama — small and medium models"),
    VLLM_27B: ("vllm-27b", "vLLM — the 27B"),
}


def _build() -> tuple[ModelServer, ...]:
    """Group the catalogue by base URL, in catalogue order (smallest first)."""
    grouped: dict[str, list[ModelChoice]] = {}
    for choice in model_catalog.CATALOG:
        grouped.setdefault(choice.base_url, []).append(choice)

    servers: list[ModelServer] = []
    for base_url, choices in grouped.items():
        host = urlparse(base_url).hostname or base_url
        # An unnamed base URL still gets an entry rather than vanishing: a model
        # nobody can see is worse than one with a plain label.
        server_id, label = _NAMES.get(base_url, (host, host))
        servers.append(
            ModelServer(
                id=server_id,
                label=label,
                base_url=base_url,
                host=host,
                models=tuple(choices),
            )
        )
    return tuple(servers)


SERVERS: tuple[ModelServer, ...] = _build()


def by_id(server_id: str) -> ModelServer | None:
    """The named server, or None. Callers turn None into a 404."""
    for server in SERVERS:
        if server.id == server_id:
            return server
    return None


def server_for(choice: ModelChoice) -> ModelServer | None:
    """Which server would answer for this model."""
    for server in SERVERS:
        if server.base_url == choice.base_url:
            return server
    return None


@dataclass(frozen=True)
class ModelStatus:
    """One pickable model, and whether its server is serving it right now."""

    choice: ModelChoice
    #: The server named this model when asked. False while it is still loading,
    #: and false for a model that was never pulled into Ollama.
    loaded: bool


@dataclass(frozen=True)
class ServerStatus:
    """One server as a person needs to see it: state, counts, and what it holds."""

    server: ModelServer
    state: ServerState
    #: Replicas asked for and ready, or None when the cluster cannot be read.
    #: None is not zero — see `ServerState.UNKNOWN`.
    desired_replicas: int | None
    ready_replicas: int | None
    models: tuple[ModelStatus, ...]

    @property
    def holds_gpu(self) -> bool:
        """Is this server claiming the card — running, or on its way up?

        `desired >= 1` rather than "is it answering": a pod loading weights is
        already holding the device, and that is precisely when somebody wants to
        know why the other model will not start.
        """
        if self.desired_replicas is not None:
            return self.desired_replicas >= 1
        return self.state in (ServerState.RUNNING, ServerState.STARTING)


def status_of(server: ModelServer) -> ServerStatus:
    """Probe one server and read its replica count.

    One probe per server rather than one per model: the two are the same HTTP call,
    and the 4B and the 8B live behind the same one.
    """
    replicas = model_scaler.read_replicas(server.host)
    # Ask the server itself even when no model is configured, but only once the
    # cluster has said a pod is up. That evidence is what makes the connect safe to
    # spend: off a cluster `read_replicas` is None, so nothing is probed and the
    # hostnames — which hang rather than fail on a dev box — are never resolved.
    running_somewhere = replicas is not None and replicas.desired >= 1
    probe = model_catalog.probe_server(server.base_url, force=running_somewhere)

    if probe.served:
        state = ServerState.RUNNING
    elif replicas is None:
        # No cluster to ask. A listener that is not answering is loading; silence
        # could be either off or never deployed, and saying which would be a guess.
        state = ServerState.STARTING if probe.listening else ServerState.UNKNOWN
    elif replicas.desired == 0:
        state = ServerState.STOPPED
    else:
        state = ServerState.STARTING

    return ServerStatus(
        server=server,
        state=state,
        desired_replicas=None if replicas is None else replicas.desired,
        ready_replicas=None if replicas is None else replicas.ready,
        models=tuple(
            ModelStatus(choice=choice, loaded=choice.served_name in probe.served)
            for choice in server.models
        ),
    )


def statuses() -> tuple[ServerStatus, ...]:
    """Every server, in catalogue order."""
    return tuple(status_of(server) for server in SERVERS)


def controllable() -> bool:
    """Can this install start and stop models, or only look at them?"""
    return model_scaler.available()


@dataclass(frozen=True)
class SwitchResult:
    """The outcome of a start or stop, in the terms a person needs to be told."""

    ok: bool
    #: Servers this scaled to zero to free the card. Empty for a stop, and empty
    #: for a start that had the card to itself.
    released: tuple[str, ...]
    #: One sentence to show. Says what is happening and how long it takes, or why
    #: nothing happened.
    detail: str


def _hint(server: ModelServer) -> str:
    """The `make` command that does this from a laptop, named for this server."""
    suffix = "27b" if server.base_url == VLLM_27B else "8b"
    return f"make k8s-model M={suffix}"


def start(server: ModelServer) -> SwitchResult:
    """Bring a server up, releasing every other one first.

    Returns immediately. `ok` means the cluster accepted the requests, not that the
    model is ready — the caller shows `detail` and lets the status view report the
    rest.
    """
    if not controllable():
        return SwitchResult(
            ok=False,
            released=(),
            detail=(
                f"This install cannot start models — it is not running in the "
                f"cluster, or has no permission to scale. Run `{_hint(server)}`."
            ),
        )

    # Release first, and unconditionally rather than only when a read says the
    # other is up: the read may be the thing that is unavailable, and scaling
    # something already at zero is a no-op. Starting into an occupied card is the
    # failure worth spending two extra calls to avoid.
    released: list[str] = []
    for other in SERVERS:
        if other.id == server.id:
            continue
        if model_scaler.set_replicas(other.host, 0):
            released.append(other.id)

    if not model_scaler.set_replicas(server.host, 1):
        return SwitchResult(
            ok=False,
            released=tuple(released),
            detail=(
                f"{server.label} could not be started — the cluster refused the "
                f"request. Run `{_hint(server)}` to see why."
            ),
        )

    detail = (
        f"{server.label} is starting. Loading weights into VRAM takes a few "
        f"minutes for a large model."
    )
    if released:
        # Said out loud, because the pod being replaced does not exit instantly and
        # the new one sits Pending until it does.
        detail += (
            " The other model server is being stopped to free the GPU, so the new"
            " one may wait a moment for the card."
        )
    return SwitchResult(ok=True, released=tuple(released), detail=detail)


def stop(server: ModelServer) -> SwitchResult:
    """Scale a server to zero, freeing the GPU now rather than on the idle timer."""
    if not controllable():
        return SwitchResult(
            ok=False,
            released=(),
            detail=(
                "This install cannot stop models — it is not running in the "
                "cluster, or has no permission to scale. Run `make k8s-model M=off`."
            ),
        )
    if not model_scaler.set_replicas(server.host, 0):
        return SwitchResult(
            ok=False,
            released=(),
            detail=f"{server.label} could not be stopped — the cluster refused the request.",
        )
    return SwitchResult(
        ok=True,
        released=(server.id,),
        detail=f"{server.label} is stopping. The GPU is released once its pod exits.",
    )
