"""Streaming a reply token by token, and persisting it once it is whole.

## Why the tools run first and only the answer streams

A turn can be two model calls: one that asks for a tool, and one that answers.
Streaming the first would show the user a tool-call payload appearing character by
character, which is noise. So the tool rounds run to completion the way they
always have, and **only the final answer streams**. What the user sees is a pause
(the model deciding and looking things up), then prose arriving live — which is
what every chat interface they have used does.

## The session problem, and why this opens its own

FastAPI closes a request-scoped dependency when the *response* is returned, and a
`StreamingResponse` returns before its body has been generated. So a generator
that used `Depends(get_db)`'s session would be writing through a session that is
already closing — intermittently, under load, which is the worst kind.

So the tool rounds use the request's session (they finish before streaming
starts), and the **persist at the end opens its own short-lived session**. That is
not a workaround; it is the honest lifetime: the write happens after the response
began, so it cannot belong to the request's transaction.

## The user's turn is already stored before any of this

Same rule as the non-streaming path, for the same reason: a dropped connection
mid-stream must cost the reply and never the typing. A stream that dies halfway
leaves the question in the thread and no answer, which is recoverable by asking
again — and is exactly what the transcript should say happened.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.chat import ChatMessage, ChatThread
from app.models.enums import ChatRole
from app.services import chat, chat_agent, chat_tools
from app.services.chat_agent import ChatModel, ModelUnavailable

log = logging.getLogger("almagest.chat.stream")


def _event(kind: str, payload: dict[str, Any]) -> str:
    """One SSE frame.

    Named events rather than a bare `data:` line, so the client can tell a token
    from a tool call from a failure without sniffing the shape of the JSON.
    """
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


def stream_reply(
    model: ChatModel,
    session: Session,
    thread: ChatThread,
    turns: Sequence[ChatMessage],
) -> Iterator[str]:
    """Yield SSE frames for one assistant turn, then persist it.

    Frames: `tool` (zero or more), then `token` (many), then exactly one of `done`
    or `error`. A client that sees `done` has the whole reply; one that sees the
    connection drop without it should assume the turn did not land, because it
    did not.
    """
    messages = chat_agent.to_messages(turns)
    tools = chat_tools.TOOLS
    calls_made: list[dict[str, Any]] = []

    try:
        # --- tool rounds, not streamed. See the module docstring.
        for _ in range(chat_agent.MAX_TOOL_ROUNDS):
            message = model.complete(messages, tools)
            requested = message.get("tool_calls") or []
            if not requested:
                break
            messages.append(message)
            for spec in requested:
                function = spec.get("function", {})
                name = str(function.get("name", ""))
                arguments = function.get("arguments", "{}")
                calls_made.append({"tool": name, "arguments": arguments})
                # Surfaced as it happens: "looking that up" is the explanation for
                # the pause the user is currently staring at.
                yield _event("tool", {"tool": name, "arguments": str(arguments)[:400]})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": spec.get("id", ""),
                        "name": name,
                        "content": chat_tools.call(session, name, arguments),
                    }
                )
        else:
            yield _event("error", {"message": "kept looking things up without answering"})
            return

        # --- the answer, streamed.
        collected: list[str] = []
        for delta in model.stream(messages):
            collected.append(delta)
            yield _event("token", {"text": delta})

        text = chat_agent._strip_thinking("".join(collected))
        if not text:
            text = "(the model returned nothing — it may have run out of tokens)"

    except ModelUnavailable as error:
        # The user's turn is already stored; this costs the reply only.
        yield _event("error", {"message": str(error)})
        return

    # --- persist, in a session of this generator's own. See the module docstring.
    with get_session_factory()() as writer:
        stored = writer.get(ChatThread, thread.id)
        if stored is not None:
            message_row = chat.append_message(
                writer,
                thread=stored,
                role=ChatRole.ASSISTANT,
                content=text,
                model=model.model,
                tool_calls_json=json.dumps(calls_made) if calls_made else None,
            )
            writer.commit()
            yield _event("done", {"message_id": message_row.id, "model": model.model})
            return
    yield _event("done", {"message_id": None, "model": model.model})
