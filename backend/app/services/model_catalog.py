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

## Two servers, and the reason is the card, not taste

`nvidia.com/gpu` on this node is capacity 1, integral and exclusive (measured, ADR
0016). One **server** may hold it at a time — but Ollama can hold several
*models* and swap between them on demand, so the small and medium rungs share one
deployment and switching between them costs a reload, not a rollout.

Only the 27B needs its own server (vLLM, for AWQ-Marlin and an fp8 KV cache — the
things that make a 27B fit in 24 GB at all). So switching *to* it is a genuine
scale-down/scale-up, which is why `requires_swap` is on the row and why the UI
says so before you pick it.

## `available` is not knowable from here

Whether a model is actually loadable depends on which deployment currently holds
the card, and asking would mean a cluster round trip on every page load. So the
catalogue reports what is *configured*, `reachable` is resolved per base URL when
somebody asks, and a pick that turns out to be wrong fails in the one place that
can say so usefully — the send, which already reports an unreachable model without
losing the message.
"""

from __future__ import annotations

from dataclasses import dataclass

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
        # Its own server, and the card is exclusive.
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
