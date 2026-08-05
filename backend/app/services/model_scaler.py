"""Start a model server on demand, from inside the API pod.

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


def ensure_running(host: str) -> bool:
    """Scale the Deployment behind `host` to one replica. True if the call landed.

    True means "the scale-up was requested", **not** "the model is ready" — see the
    module docstring. Idempotent: scaling something already at 1 is a no-op, so a
    person mashing send does not fight the cluster.
    """
    deployment = DEPLOYMENT_FOR_HOST.get(host)
    if deployment is None:
        # Not one of ours. Refused rather than derived, so this credential can only
        # ever reach the two servers named above.
        return False

    token = _read(TOKEN_PATH)
    namespace = _read(NAMESPACE_PATH)
    if token is None or namespace is None:
        return False

    url = (
        f"https://kubernetes.default.svc/apis/apps/v1/namespaces/{namespace}"
        f"/deployments/{deployment}/scale"
    )
    body = json.dumps({"spec": {"replicas": 1}}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            # Merge-patch, so this sets `replicas` and touches nothing else.
            "Content-Type": "application/merge-patch+json",
        },
    )
    context = ssl.create_default_context(cafile=str(CA_PATH)) if CA_PATH.is_file() else None
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            payload: Any = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        # Fails soft: the caller says the model is down, which is true.
        log.info("could not scale %s up: %s", deployment, error)
        return False

    log.info("asked %s for 1 replica (was %s)", deployment, payload.get("spec", {}).get("replicas"))
    return True
