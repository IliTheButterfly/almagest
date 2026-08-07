"""Read and set a model server's replica count, from inside the API pod.

The cluster half of model control: this module knows how to ask Kubernetes what a
model Deployment is doing and to change it. Which models exist, which server holds
which, and what starting one means for the others is `app.services.model_servers`.

## Why this exists

The GPU is released when chat is idle — it must be, since holding it blocks every
other namespace on the machine. But that left a hole: the model is *usually* down,
and sending a message just failed. The person then had to leave the app, run
`make k8s-model`, and come back, which is not a workflow anybody keeps.

So a send that finds its model down **starts it**. Releasing on idle stops being a
trap once coming back is automatic, and the two together are what let the GPU be
genuinely shared rather than either hogged or unusable.

## It scales and returns; it does not wait

A 27B takes minutes to load weights and compile CUDA graphs. Holding an HTTP
request open for that would tie up a worker in a single-replica API, and time out
in every proxy between here and the browser. So this asks for the scale-up and
returns immediately, and the caller tells the person it is starting. The retry
button is then the natural second half — the message is already in the thread.

## No Kubernetes client library

The API image must not grow one for two REST calls. This uses the pod's own
mounted ServiceAccount token against the in-cluster API, with `urllib` — the same
choice, for the same reason, as every other client in this repository.

**Everything here fails soft.** No token, no RBAC, not in a cluster at all: it
returns False and the caller reports that the model is down, which is exactly what
it would have said anyway. A dev box running `make run` has none of this and must
not break because of it.
"""

from __future__ import annotations

import json
import logging
import pathlib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("almagest.model_scaler")

#: Where the kubelet mounts the pod's identity. Absent outside a cluster, which is
#: the signal that scaling is simply not available here.
SA_DIR = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")
TOKEN_PATH = SA_DIR / "token"
CA_PATH = SA_DIR / "ca.crt"
NAMESPACE_PATH = SA_DIR / "namespace"

#: Seconds for a scale call. Short: this happens inside a chat request, and a
#: cluster API that has not answered in five seconds is not going to help.
TIMEOUT = 5.0

#: Deployment names this may touch, by base URL host. An allowlist rather than a
#: derived name: this runs with a credential that can scale things, and the set of
#: things it may scale should be readable in one line rather than computed.
DEPLOYMENT_FOR_HOST = {
    "almagest-llm": "almagest-llm",
    "almagest-llm-27b": "almagest-llm-27b",
}


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def available() -> bool:
    """Is in-cluster scaling possible at all? False on a dev box, and that is fine."""
    return TOKEN_PATH.is_file()


@dataclass(frozen=True)
class ReplicaCount:
    """What the cluster says about one model Deployment, right now.

    Both numbers matter and they are not the same question. `desired` is what
    somebody (or this code) asked for; `ready` is how many pods are actually
    serving. A model downloading 16 GB of weights sits at `desired=1, ready=0` for
    minutes, and reporting that as "running" is the single most misleading thing
    this could say — hence two fields rather than a boolean.
    """

    desired: int
    ready: int


def _call(deployment: str, *, subresource: str, method: str, body: bytes | None) -> Any | None:
    """One authenticated call against the pod's own in-cluster API, or None.

    Everything here fails soft and returns None. There is no cluster on a dev box,
    no token in a test, and possibly no RBAC in a namespace somebody re-created —
    all three are states the caller must already handle, so none of them is worth
    an exception that a route would only convert back into "not available".
    """
    if deployment not in DEPLOYMENT_FOR_HOST.values():
        # Refused rather than derived: this runs with a credential that can scale
        # things, and the set of things it may scale stays readable in one place.
        return None

    token = _read(TOKEN_PATH)
    namespace = _read(NAMESPACE_PATH)
    if token is None or namespace is None:
        return None

    url = (
        f"https://kubernetes.default.svc/apis/apps/v1/namespaces/{namespace}"
        f"/deployments/{deployment}{subresource}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        # Merge-patch, so this sets `replicas` and touches nothing else.
        headers["Content-Type"] = "application/merge-patch+json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    context = ssl.create_default_context(cafile=str(CA_PATH)) if CA_PATH.is_file() else None
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            return json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        log.info("cluster call %s %s%s failed: %s", method, deployment, subresource, error)
        return None


def read_replicas(host: str) -> ReplicaCount | None:
    """How many replicas the Deployment behind `host` wants and has, or None.

    None means "this install cannot see the cluster" — a dev box, a test, or
    missing RBAC — and is deliberately distinct from `desired=0`, which means the
    model is genuinely switched off. A UI that conflates the two tells somebody a
    model is stopped when the truth is that nobody here can tell.

    Reads the `scale` subresource rather than the whole Deployment: it carries both
    numbers this needs, and it is the narrower of the two things RBAC already
    grants.
    """
    deployment = DEPLOYMENT_FOR_HOST.get(host)
    if deployment is None:
        return None
    payload = _call(deployment, subresource="/scale", method="GET", body=None)
    if not isinstance(payload, dict):
        return None
    spec = payload.get("spec") or {}
    status = payload.get("status") or {}
    return ReplicaCount(
        desired=int(spec.get("replicas") or 0),
        # `status.replicas` on a scale subresource counts *ready* pods for the
        # selector — absent entirely when none are up, which reads as 0.
        ready=int(status.get("replicas") or 0),
    )


def set_replicas(host: str, replicas: int) -> bool:
    """Scale the Deployment behind `host`. True if the call landed.

    True means "the cluster accepted the request", **not** "the model is ready" —
    see the module docstring. Idempotent in both directions, so a person mashing a
    Start button does not fight the cluster.
    """
    deployment = DEPLOYMENT_FOR_HOST.get(host)
    if deployment is None:
        return False
    body = json.dumps({"spec": {"replicas": replicas}}).encode()
    payload = _call(deployment, subresource="/scale", method="PATCH", body=body)
    if payload is None:
        # Fails soft: the caller says the model is down, which is true.
        return False
    log.info("asked %s for %s replicas", deployment, replicas)
    return True


def ensure_running(host: str) -> bool:
    """Scale the Deployment behind `host` to one replica. True if the call landed."""
    return set_replicas(host, 1)
