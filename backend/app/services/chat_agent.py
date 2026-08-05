"""The chat agent loop: a thread's turns in, one assistant turn out.

## Why this is in the API process, when ADR 0018 said it would not be

ADR 0018 puts the agent loop in a separate service, and its reason is specific:
*an agent whose tools call back into a single-replica SQLite writer, from inside
that writer's own process, is a self-deadlock.* The hazard is **tool re-entry**,
not the loop.

Its tools (`app.services.chat_tools`) run **against the session the request
already holds** — ordinary reads, no second request, no second connection from the
same bounded pool. The re-entry is therefore still absent and the deadlock still
unreachable.

An earlier draft of this docstring said "the moment a tool is added, this must
move". That was wrong, and the distinction it missed is the one above: **HTTP
re-entry deadlocks; an in-process read does not.** What *would* force the move is
a tool that has to travel over HTTP — reusing `mcpserver`'s curated surface, or
anything reaching another service. See `chat_tools` for why that duplication is
accepted meanwhile, and which side wins when the two disagree.

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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.chat import ChatMessage
from app.models.enums import ChatRole
from app.services import chat_tools

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
    "You have tools that read the real inventory. **Use them** rather than "
    "answering from memory whenever a question is about what exists, how many there "
    "are, or where something is — you cannot know those without looking.\n"
    "\n"
    "Report only what a tool returned. Never invent a stock level, a location, a "
    "part number or a quantity, and never round a tool's number to a nicer one. If "
    "a search comes back empty, say nothing is *recorded* as matching — which is "
    "not the same as nothing being in the room, because a part whose details were "
    "never filled in cannot match.\n"
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
    #: What the model looked up, stored so the UI can **show** it. A tool call the
    #: user cannot see is a fact they cannot check.
    tool_calls_json: str | None = None


class ChatModel(Protocol):
    """What the loop needs from a model. The seam ADR 0018's move depends on."""

    model: str

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]: ...

    def stream(self, messages: Sequence[dict[str, Any]]) -> Iterator[str]:
        """Yield the answer's text deltas. No tools: see `chat_stream`."""
        ...


@dataclass
class OpenAICompatChatModel:
    """Any `/v1/chat/completions` endpoint — the cluster's Ollama, or vLLM."""

    base_url: str
    model: str
    timeout: float = DEFAULT_TIMEOUT

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            # Some sampling, unlike extraction: this is conversation, and a fully
            # greedy chat model repeats itself into a loop on follow-ups.
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
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
            message: dict[str, Any] = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ModelUnavailable(f"no completion in the response from {url}") from error
        return message

    def stream(self, messages: Sequence[dict[str, Any]]) -> Iterator[str]:
        """Text deltas from an SSE `chat.completion.chunk` stream.

        Read line by line off the socket rather than buffered, which is the whole
        point — a response held until complete would arrive as one lump and the
        user would be back to watching a spinner.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0.3,
            "stream": True,
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
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        return
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        # A partial frame is not fatal — the next line usually
                        # completes the thought, and dying here would throw away a
                        # reply that is most of the way there.
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                    text = delta.get("content")
                    if text:
                        yield str(text)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ModelUnavailable(f"{type(error).__name__} streaming from {url}") from error


@dataclass
class FakeChatModel:
    """A scripted model, so the loop is testable with no network and no GPU.

    `scripted` is a queue of raw assistant messages, which is what makes the
    *tool* path testable: the first can carry `tool_calls` and the second the
    prose that follows, exactly as a real model's two round trips do.
    """

    model: str = "fake"
    reply: str = "I cannot see the inventory from here yet."
    scripted: list[dict[str, Any]] | None = None
    seen: list[list[dict[str, Any]]] | None = None

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 - Protocol shape
    ) -> dict[str, Any]:
        if self.seen is None:
            self.seen = []
        self.seen.append([dict(m) for m in messages])
        if self.scripted:
            return self.scripted.pop(0)
        return {"role": "assistant", "content": self.reply}

    def stream(self, messages: Sequence[dict[str, Any]]) -> Iterator[str]:
        """The scripted reply, one word at a time — enough to prove the frames
        arrive separately rather than as one lump."""
        del messages
        for index, word in enumerate(self.reply.split(" ")):
            yield word if index == 0 else f" {word}"


def _strip_thinking(text: str) -> str:
    """Remove a `<think>` block. See the module docstring."""
    return _THINK.sub("", text).strip()


def to_messages(turns: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """The transcript as the wire wants it, newest `MAX_HISTORY_TURNS` kept.

    Stored `system` turns are dropped and `SYSTEM_PROMPT` is prepended instead:
    the prompt is the current one, and replaying an older stored system turn would
    make the model answer under instructions that have since changed.
    """
    recent = [turn for turn in turns if turn.role != ChatRole.SYSTEM][-MAX_HISTORY_TURNS:]
    out: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in recent:
        # A `tool` turn has no meaning to a model that was given no tools; sending
        # it would describe capabilities this loop does not have.
        role = "assistant" if turn.role in (ChatRole.ASSISTANT, ChatRole.TOOL) else "user"
        out.append({"role": role, "content": turn.content})
    return out


#: Tool rounds allowed in one turn. A model that keeps calling tools without
#: answering is looping; three is enough for "search, then check a location, then
#: answer" and short enough that a loop costs seconds rather than the whole lease.
MAX_TOOL_ROUNDS = 3


def respond(model: ChatModel, turns: Sequence[ChatMessage], *, session: Any = None) -> Reply:
    """One assistant turn, running any tools the model asks for on the way.

    `session` is the caller's open `Session`. With none, the model is offered no
    tools at all — which keeps this usable where there is no database, and keeps
    the no-tools path testable.
    """
    messages = to_messages(turns)
    tools = chat_tools.TOOLS if session is not None else None
    calls_made: list[dict[str, Any]] = []
    message: dict[str, Any] = {}

    for _ in range(MAX_TOOL_ROUNDS):
        message = model.complete(messages, tools)
        requested = message.get("tool_calls") or []
        if not requested:
            break
        # The assistant's own tool-call turn goes back verbatim, or the model
        # cannot match its call ids to the results that follow.
        messages.append(message)
        for spec in requested:
            function = spec.get("function", {})
            name = str(function.get("name", ""))
            arguments = function.get("arguments", "{}")
            calls_made.append({"tool": name, "arguments": arguments})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": spec.get("id", ""),
                    "name": name,
                    "content": chat_tools.call(session, name, arguments),
                }
            )
    else:
        # Still asking for tools after the cap. Saying so beats an empty bubble,
        # and the cap is what stops a loop costing the whole request.
        message = {"content": "I kept looking things up without reaching an answer."}

    text = _strip_thinking(str(message.get("content") or ""))
    if not text:
        # An empty completion is a real outcome — a model that emitted only a
        # think block, or hit its token ceiling mid-thought. Saying so beats
        # storing an empty bubble nobody can interpret.
        text = "(the model returned nothing — it may have run out of tokens)"
    return Reply(
        text=text,
        model=model.model,
        tool_calls_json=json.dumps(calls_made) if calls_made else None,
    )


def build_model(base_url: str, model_name: str) -> ChatModel | None:
    """The configured model, or None when none is configured.

    None is not an error and callers must not treat it as one — see the module
    docstring on the model being optional.
    """
    if not base_url:
        return None
    return OpenAICompatChatModel(base_url=base_url, model=model_name)
