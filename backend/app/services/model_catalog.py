"""Which models this install can talk to, and what each is for.

Three sizes, because the jobs genuinely differ:

* **Datasheet extraction** is a reading task with a right answer, decoded against a
  JSON schema. The grammar does the work, so a small model is not a compromise —
  `extract.schema_for` makes an invented field name unrepresentable, and the
  MPN-decoder cross-check catches the wrong-variant-row error a bigger model would
  otherwise be bought to avoid.
* **Part-picking conversation** is the opposite: open-ended, and the failure is
  answering from memory instead of calling a tool. That is where size actually
  buys something — a 27B decides *when to look* far better than an 8B does.
* **Everything in between** wants a middle rung, or people pick the big one for
  everything and wait a minute for "what is a 0603".

## Two servers, and what `requires_swap` actually means

Ollama holds several *models* and swaps between them on demand, so the small and
medium rungs share one deployment: switching between them costs a weight reload,
not a rollout. The 27B needs its own server — vLLM, for AWQ-Marlin and an fp8 KV
cache, which are what make a 27B fit in 24 GB at all.

`requires_swap` therefore says **"this one needs a GPU that no other Almagest
model server is holding"**, not "this model is unusual". Observed directly
(2026-08-05): with `almagest-llm` up, scaling `almagest-llm-27b` to 1 leaves it
`Pending` with `Insufficient nvidia.com/gpu`; scaling Ollama to 0 starts it
immediately.

**What is *not* established is a node-wide fact.** An earlier note here claimed
`nvidia.com/gpu` is "capacity 1, integral and exclusive". That overstated a
measurement which could only show availability *at that instant* — this namespace
cannot read nodes (RBAC), other namespaces on this machine run GPU work of their
own, and the probe may simply have landed while one of them held a device. So a
`Pending` here means "no GPU free for us right now", and the cause may be another
namespace rather than our own Ollama. `make k8s-model` frees ours first because
that is the half we control.

## Reachability is probed, because a picker that lies is worse than no picker

An earlier version listed all three unconditionally and let the send fail. That is
how somebody picks the 27B, waits, and is told "the model could not be reached" —
a true sentence that explains nothing, because **choosing a model in the UI does
not start it.** Both deployments default to zero and the reaper scales them down
on idle, so at any moment most of this list is usually not running.

So `probe()` opens a socket to each base URL with a short timeout and reports what
answered. It is a connect, not a request: a model mid-download has a pod but no
listener, which is exactly the state worth distinguishing from "running".

The cost is a few hundred milliseconds on the models list. Worth it — the
alternative is a control that offers choices which silently do not work.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlparse

#: The Ollama deployment. Holds several models and swaps between them; a switch
#: within this base URL costs a weight reload, not a rollout.
OLLAMA = "http://almagest-llm:11434"
#: The vLLM deployment, off by default. Taking the card is an explicit act.
VLLM_27B = "http://almagest-llm-27b:8000"


@dataclass(frozen=True)
class ModelChoice:
    """One model somebody can pick, and enough about it to pick well."""

    #: Stable id used on the wire and stored on the message that answered.
    id: str
    label: str
    #: Rough parameter count, for ordering and for the UI to say "bigger is slower".
    size_b: int
    base_url: str
    #: What to send as `model` to that endpoint.
    served_name: str
    #: One line a person can choose on, written in terms of the job rather than
    #: the architecture.
    good_for: str
    #: True when selecting this needs the GPU handed over from another server —
    #: minutes, not seconds. Surfaced so the choice is informed.
    requires_swap: bool = False


#: Ordered small to large. The order is the UI's order, deliberately: the cheap
#: one should be the easy pick, and reaching for 27B should be a decision.
CATALOG: tuple[ModelChoice, ...] = (
    ModelChoice(
        id="qwen3-4b",
        label="Qwen3 4B — fast",
        size_b=4,
        base_url=OLLAMA,
        served_name="qwen3:4b",
        good_for=(
            "Datasheet extraction and quick lookups. Schema-constrained decoding "
            "does the work here, so small is not a compromise."
        ),
    ),
    ModelChoice(
        id="qwen3-8b",
        label="Qwen3 8B — balanced",
        size_b=8,
        base_url=OLLAMA,
        served_name="qwen3:8b",
        good_for="General questions about the inventory. The default.",
    ),
    ModelChoice(
        id="almagest-27b",
        label="Qwen3.6 27B — best reasoning",
        size_b=27,
        base_url=VLLM_27B,
        served_name="almagest-27b",
        good_for=(
            "Part picking, substitutions and design discussion — anywhere deciding "
            "*when to look something up* matters more than raw speed."
        ),
        # Its own server, so it needs a GPU no other Almagest model server holds.
        # Not a property of the model — see the module docstring.
        requires_swap=True,
    ),
)

#: What chat uses when nobody chose. The middle rung: reaching for 27B should be
#: a decision, and 4B is meant for extraction rather than conversation.
DEFAULT_ID = "qwen3-8b"


def by_id(model_id: str | None) -> ModelChoice:
    """Resolve a pick, falling back to the default rather than raising.

    A stale id from a bookmarked UI, or a model removed from the catalogue, should
    answer with the default and not 500 — the person asked a question, and which
    model answers it is not the part they care about most.
    """
    for choice in CATALOG:
        if choice.id == model_id:
            return choice
    for choice in CATALOG:
        if choice.id == DEFAULT_ID:
            return choice
    return CATALOG[0]


#: How long to wait for a model server to accept a connection. Short: this runs
#: per model on a UI list, and a server that cannot complete a TCP handshake in
#: half a second on the same cluster network is not one that will answer a chat.
PROBE_TIMEOUT = 0.5


def probe(choice: ModelChoice, timeout: float = PROBE_TIMEOUT) -> bool:
    """Is something listening for this model right now?

    A bare TCP connect rather than an HTTP request, deliberately: vLLM spends
    minutes downloading weights before it binds a port, and a pod that exists but
    is not listening is precisely the state a picker must not present as ready.

    Any failure is `False`. This decides whether a control is offered, so being
    wrong in the optimistic direction is the expensive one.
    """
    parsed = urlparse(choice.base_url)
    host, port = parsed.hostname, parsed.port
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_hint(choice: ModelChoice) -> str:
    """The command that makes this model available, named for *this* model.

    Generic advice ("start a model server") is what turns a specific failure into
    a shrug. The person picked the 27B; the answer they need mentions the 27B.
    """
    suffix = {"qwen3-4b": "8b", "qwen3-8b": "8b", "almagest-27b": "27b"}.get(choice.id, "8b")
    return f"make k8s-model M={suffix}"
