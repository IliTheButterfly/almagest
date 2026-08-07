"""Seeing which models are running, and turning one on or off.

Everything here fakes the two things that are not local: what a model server
answers when asked which models it serves, and what the cluster says about a
Deployment's replicas. Both are one function each (`model_catalog.probe_server`,
`model_scaler.read_replicas` / `set_replicas`), which is the point of that seam.

The cases worth pinning are the ones where an honest answer differs from the
convenient one: a pod that exists but is still loading is *not* running, a cluster
this install cannot read is *not* "stopped", and starting one server stops the
other because there is one GPU.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services import model_catalog, model_scaler, model_servers


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """A fake cluster: replica counts by host, readable and writable."""
    replicas = {server.host: 0 for server in model_servers.SERVERS}

    def read(host: str) -> model_scaler.ReplicaCount | None:
        if host not in replicas:
            return None
        # Ready follows desired: these tests drive readiness through `serving`
        # below, and a fake that reported partial readiness would be asserting
        # about Kubernetes rather than about this code.
        return model_scaler.ReplicaCount(desired=replicas[host], ready=replicas[host])

    def write(host: str, count: int) -> bool:
        if host not in replicas:
            return False
        replicas[host] = count
        return True

    monkeypatch.setattr(model_scaler, "read_replicas", read)
    monkeypatch.setattr(model_scaler, "set_replicas", write)
    monkeypatch.setattr(model_servers, "controllable", lambda: True)
    return replicas


def serving(monkeypatch: pytest.MonkeyPatch, **by_url: object) -> None:
    """Make each base URL answer with the given served names, or nothing.

    A value of `None` means "listening but not answering yet" — the loading state.
    """

    def probe(base_url: str, timeout: float = model_catalog.PROBE_TIMEOUT, **_: object) -> Any:
        answer = by_url.get(base_url, ())
        if answer is None:
            return model_catalog.ServerProbe(listening=True, served=frozenset())
        names = frozenset(answer)  # type: ignore[arg-type]
        return model_catalog.ServerProbe(listening=bool(names), served=names)

    monkeypatch.setattr(model_catalog, "probe_server", probe)


def find(body: dict[str, Any], server_id: str) -> dict[str, Any]:
    return next(row for row in body["servers"] if row["id"] == server_id)


# ---------------------------------------------------------------------------
# What the list says
# ---------------------------------------------------------------------------


def test_the_list_holds_every_catalogue_model_exactly_once(client: TestClient) -> None:
    """The picker's three models are grouped under servers, and none is dropped."""
    body = client.get("/api/system/models").json()
    listed = [held["id"] for server in body["servers"] for held in server["models"]]
    assert sorted(listed) == sorted(choice.id for choice in model_catalog.CATALOG)


def test_the_two_ollama_models_share_one_server(client: TestClient) -> None:
    """The 4B and 8B are one Deployment, so they must be one start/stop control.

    A UI offering to stop "the 4B" separately would either lie or take the 8B with
    it — see `model_servers`' docstring.
    """
    body = client.get("/api/system/models").json()
    ollama = find(body, "ollama")
    assert sorted(held["id"] for held in ollama["models"]) == ["qwen3-4b", "qwen3-8b"]
    assert ollama["deployment"] == "almagest-llm"


def test_a_server_that_answers_is_running_and_names_the_loaded_model(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster["almagest-llm"] = 1
    serving(monkeypatch, **{model_catalog.OLLAMA: ["qwen3:8b"]})

    ollama = find(client.get("/api/system/models").json(), "ollama")
    assert ollama["state"] == "running"
    assert ollama["holds_gpu"] is True
    loaded = {held["id"]: held["loaded"] for held in ollama["models"]}
    # Only the model the server actually named. The 4B shares the deployment but
    # was never pulled, and offering it would 404 at generation time.
    assert loaded == {"qwen3-8b": True, "qwen3-4b": False}


def test_a_pod_that_is_still_loading_is_starting_not_running(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """vLLM binds its port minutes before it can answer. That gap is the state."""
    cluster["almagest-llm-27b"] = 1
    serving(monkeypatch, **{model_catalog.VLLM_27B: None})

    vllm = find(client.get("/api/system/models").json(), "vllm-27b")
    assert vllm["state"] == "starting"
    # Already holding the card even though it cannot answer — which is exactly why
    # the other server will not start.
    assert vllm["holds_gpu"] is True


def test_zero_replicas_is_stopped(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch)
    body = client.get("/api/system/models").json()
    assert [row["state"] for row in body["servers"]] == ["stopped", "stopped"]
    assert all(row["holds_gpu"] is False for row in body["servers"])


def test_an_unreadable_cluster_is_unknown_rather_than_stopped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Nobody here can tell" must not render as "the model is off".

    This is the dev-box case, and the difference is the whole reason
    `desired_replicas` is nullable.
    """
    monkeypatch.setattr(model_scaler, "read_replicas", lambda _host: None)
    serving(monkeypatch)

    body = client.get("/api/system/models").json()
    assert [row["state"] for row in body["servers"]] == ["unknown", "unknown"]
    assert all(row["desired_replicas"] is None for row in body["servers"])
    assert all(row["ready_replicas"] is None for row in body["servers"])


def test_a_dev_box_is_not_controllable_and_says_what_to_run(client: TestClient) -> None:
    body = client.get("/api/system/models").json()
    assert body["controllable"] is False
    assert body["hint"] == "make k8s-model M=8b|27b|off"


def test_a_pod_that_is_up_is_asked_even_with_no_model_configured(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's `ALMAGEST_LLM_BASE_URL` gate is skipped once a pod is known up.

    Without this, a server running while that variable is unset — the state
    `make k8s-model M=off` leaves behind — reads as perpetually `starting`, because
    the one call that could contradict the replica count was never made.
    """
    cluster["almagest-llm"] = 1
    asked: dict[str, bool] = {}

    def probe(base_url: str, timeout: float = model_catalog.PROBE_TIMEOUT, **kwargs: object) -> Any:
        asked[base_url] = bool(kwargs.get("force"))
        return model_catalog.ServerProbe(listening=False, served=frozenset())

    monkeypatch.setattr(model_catalog, "probe_server", probe)
    client.get("/api/system/models")

    assert asked[model_catalog.OLLAMA] is True
    # And the one that is down is not forced: off a cluster those hostnames hang
    # rather than fail, so a probe with no evidence behind it is not free.
    assert asked[model_catalog.VLLM_27B] is False


# ---------------------------------------------------------------------------
# Starting and stopping
# ---------------------------------------------------------------------------


def test_starting_one_server_releases_the_other(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One GPU. Scaling up without scaling down leaves the new pod Pending forever."""
    cluster["almagest-llm"] = 1
    serving(monkeypatch, **{model_catalog.OLLAMA: ["qwen3:8b"]})

    body = client.post("/api/system/models/vllm-27b/start").json()
    assert body["ok"] is True
    assert body["released"] == ["ollama"]
    assert cluster == {"almagest-llm": 0, "almagest-llm-27b": 1}
    assert "GPU" in body["detail"]


def test_a_start_reports_the_state_after_itself(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response carries the fresh list, so one round trip acts and refreshes."""
    serving(monkeypatch)
    body = client.post("/api/system/models/ollama/start").json()
    assert find(body, "ollama")["state"] == "starting"
    assert find(body, "ollama")["desired_replicas"] == 1


def test_starting_something_already_up_is_a_no_op(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person mashing Start must not fight the cluster."""
    cluster["almagest-llm"] = 1
    serving(monkeypatch, **{model_catalog.OLLAMA: ["qwen3:8b"]})

    assert client.post("/api/system/models/ollama/start").json()["ok"] is True
    assert cluster["almagest-llm"] == 1


def test_stopping_scales_to_zero(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster["almagest-llm"] = 1
    serving(monkeypatch, **{model_catalog.OLLAMA: ["qwen3:8b"]})

    body = client.post("/api/system/models/ollama/stop").json()
    assert body["ok"] is True
    assert body["released"] == ["ollama"]
    assert cluster["almagest-llm"] == 0


def test_a_stop_leaves_the_other_server_alone(
    client: TestClient, cluster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster["almagest-llm"] = 1
    cluster["almagest-llm-27b"] = 1
    serving(monkeypatch)

    client.post("/api/system/models/ollama/stop")
    assert cluster["almagest-llm-27b"] == 1


def test_an_uncontrollable_install_refuses_with_the_command_that_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 with `ok: false`, not a 500: nothing broke, this install just cannot.

    The message has to name a command, or the answer is a shrug.
    """
    serving(monkeypatch)
    response = client.post("/api/system/models/vllm-27b/start")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "make k8s-model M=27b" in body["detail"]


def test_a_cluster_that_refuses_the_scale_is_reported_as_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch)
    monkeypatch.setattr(model_servers, "controllable", lambda: True)
    monkeypatch.setattr(model_scaler, "set_replicas", lambda _host, _count: False)
    monkeypatch.setattr(model_scaler, "read_replicas", lambda _host: None)

    body = client.post("/api/system/models/ollama/start").json()
    assert body["ok"] is False
    assert "could not be started" in body["detail"]


def test_an_unknown_server_is_a_404(client: TestClient) -> None:
    assert client.post("/api/system/models/gpt-9/start").status_code == 404
    assert client.post("/api/system/models/gpt-9/stop").status_code == 404


# ---------------------------------------------------------------------------
# The scaler's own reading
# ---------------------------------------------------------------------------


def test_read_replicas_is_none_outside_a_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ServiceAccount token means no answer — not a fabricated zero."""
    monkeypatch.setattr(model_scaler, "TOKEN_PATH", model_scaler.SA_DIR / "does-not-exist")
    assert model_scaler.read_replicas("almagest-llm") is None
    assert model_scaler.set_replicas("almagest-llm", 1) is False


def test_the_scaler_refuses_a_host_it_does_not_own() -> None:
    """The credential can scale things, so the set it may touch is an allowlist."""
    assert model_scaler.read_replicas("octans-gpu-builder") is None
    assert model_scaler.set_replicas("octans-gpu-builder", 0) is False


def test_a_missing_replica_field_reads_as_zero_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes omits `status.replicas` entirely when nothing is up."""
    monkeypatch.setattr(model_scaler, "_call", lambda *_a, **_k: {"spec": {}, "status": {}})
    count = model_scaler.read_replicas("almagest-llm")
    assert count == model_scaler.ReplicaCount(desired=0, ready=0)
