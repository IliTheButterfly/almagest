"""The chat agent loop: a thread's turns in, one assistant turn out.

## Why this is in the API process, when ADR 0018 said it would not be

ADR 0018 puts the agent loop in a separate service, and its reason is specific:
*an agent whose tools call back into a single-replica SQLite writer, from inside
that writer's own process, is a self-deadlock.* The hazard is **tool re-entry**,
not the loop.

This first iteration has **no tools**. It reads a transcript, calls a model, and
appends the reply — one outbound HTTP call, nothing re-entering the API. The
deadlock ADR 0018 describes is unreachable here, so shipping it in-process is
honest rather than a shortcut, and it is what makes chat testable against a real
local model today.

**The moment a tool is added, this must move.** That is not a vague intention: the
first tool call is the exact event that makes the ADR's argument apply, and the
seam is already right — `ChatModel` is a Protocol, and `respond()` takes the
transcript and returns text, so relocating it behind HTTP changes callers and
nothing else.

## The model is optional, and its absence is not an error

`llm_base_url` empty means no model is configured. Chat then answers with a plain
sentence saying so, rather than raising. That is the same posture ADR 0005 takes
for extraction: the expensive half may be absent — never installed, out of GPU,
scaled to zero for the co-tenant — and the product degrades to something honest
instead of breaking.

## `/no_think`, and why the reply is cleaned

Qwen3 emits a `<think>...</think>` block before its answer. It is genuinely
useful when debugging an extraction, and it is noise in a chat bubble — so the
prompt asks for it off and `_strip_thinking` removes it if it arrives anyway.
Belt and braces, because a model that ignores the instruction should not put its
scratchpad on the screen.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.chat import ChatMessage
from app.models.enums import ChatRole

log = logging.getLogger("almagest.chat")

#: Seconds one completion may take. Generous: the first message of a session pays
#: the model's load into VRAM (ADR 0016's `keep_alive` unloads it when idle), which
#: is tens of seconds for an 8B on a busy card.
DEFAULT_TIMEOUT = 300.0

#: How many past turns to send. The whole thread would be correct and unbounded;
#: this keeps a months-old project thread from eventually exceeding the context
#: window and failing every message rather than the oldest.
MAX_HISTORY_TURNS = 30

SYSTEM_PROMPT = (
    "You are the assistant inside Almagest, a self-hosted electronic-component "
    "inventory. You help with what parts exist, where they are, and what would "
    "substitute for what.\n"
    "\n"
    "You do not yet have tools, so you cannot look anything up. Say so plainly "
    "when a question needs the actual inventory — do not invent stock levels, "
    "locations, part numbers or quantities. A confident wrong answer about what is "
    "in a drawer is worse than 'I cannot see the inventory from here'.\n"
    "\n"
    "Be brief. /no_think"
)

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class ModelUnavailable(RuntimeError):
    """The endpoint could not be reached. Distinct from having no model configured."""


@dataclass(frozen=True)
class Reply:
    text: str
    model: str


class ChatModel(Protocol):
    """What the loop needs from a model. The seam ADR 0018's move depends on."""

    model: str

    def complete(self, messages: Sequence[dict[str, str]]) -> str: ...


@dataclass
class OpenAICompatChatModel:
    """Any `/v1/chat/completions` endpoint — the cluster's Ollama, or vLLM."""

    base_url: str
    model: str
    timeout: float = DEFAULT_TIMEOUT

    def complete(self, messages: Sequence[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            # Some sampling, unlike extraction: this is conversation, and a fully
            # greedy chat model repeats itself into a loop on follow-ups.
            "temperature": 0.3,
        }
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body: Any = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode("utf-8", "replace")
            raise ModelUnavailable(f"HTTP {error.code} from {url}: {detail}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ModelUnavailable(f"{type(error).__name__} calling {url}: {error}") from error

        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise ModelUnavailable(f"no completion in the response from {url}") from error


@dataclass
class FakeChatModel:
    """A scripted model, so the loop is testable with no network and no GPU."""

    model: str = "fake"
    reply: str = "I cannot see the inventory from here yet."
    seen: list[list[dict[str, str]]] | None = None

    def complete(self, messages: Sequence[dict[str, str]]) -> str:
        if self.seen is None:
            self.seen = []
        self.seen.append([dict(m) for m in messages])
        return self.reply


def _strip_thinking(text: str) -> str:
    """Remove a `<think>` block. See the module docstring."""
    return _THINK.sub("", text).strip()


def to_messages(turns: Sequence[ChatMessage]) -> list[dict[str, str]]:
    """The transcript as the wire wants it, newest `MAX_HISTORY_TURNS` kept.

    Stored `system` turns are dropped and `SYSTEM_PROMPT` is prepended instead:
    the prompt is the current one, and replaying an older stored system turn would
    make the model answer under instructions that have since changed.
    """
    recent = [turn for turn in turns if turn.role != ChatRole.SYSTEM][-MAX_HISTORY_TURNS:]
    out: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in recent:
        # A `tool` turn has no meaning to a model that was given no tools; sending
        # it would describe capabilities this loop does not have.
        role = "assistant" if turn.role in (ChatRole.ASSISTANT, ChatRole.TOOL) else "user"
        out.append({"role": role, "content": turn.content})
    return out


def respond(model: ChatModel, turns: Sequence[ChatMessage]) -> Reply:
    """One assistant turn for this transcript."""
    text = _strip_thinking(model.complete(to_messages(turns)))
    if not text:
        # An empty completion is a real outcome — a model that emitted only a
        # think block, or hit its token ceiling mid-thought. Saying so beats
        # storing an empty bubble nobody can interpret.
        text = "(the model returned nothing — it may have run out of tokens)"
    return Reply(text=text, model=model.model)


def build_model(base_url: str, model_name: str) -> ChatModel | None:
    """The configured model, or None when none is configured.

    None is not an error and callers must not treat it as one — see the module
    docstring on the model being optional.
    """
    if not base_url:
        return None
    return OpenAICompatChatModel(base_url=base_url, model=model_name)
