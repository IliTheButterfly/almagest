"""The chat loop: what it sends, and what it does when the model is not there.

## What this file is really guarding

**Losing what somebody typed because the GPU was busy.** The user's turn is
committed *before* the model is called, so a timeout, an unloaded model or a
server scaled to zero costs the reply and never the typing. That ordering is easy
to "simplify" into one transaction and the damage only shows up on a bad day.

**A model answering under instructions that have since changed.** Stored `system`
turns are dropped and the current prompt is prepended, so an old transcript is
replayed under today's rules rather than yesterday's.

**A scratchpad on screen.** Qwen3 emits `<think>` blocks; the prompt asks for
them off and the loop strips them anyway, because a model ignoring an instruction
should not put its reasoning in a chat bubble.

Offline throughout — `FakeChatModel` stands in for the endpoint.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import ChatKind, ChatRole
from app.services import chat, chat_agent
from app.services.chat_agent import FakeChatModel, ModelUnavailable


def _thread(db: Session) -> object:
    return chat.create_thread(db, kind=ChatKind.SEARCH)


def test_the_current_prompt_replaces_any_stored_system_turn(db: Session) -> None:
    thread = _thread(db)
    chat.append_message(db, thread=thread, role=ChatRole.SYSTEM, content="an old prompt")
    chat.append_message(db, thread=thread, role=ChatRole.USER, content="hello")

    sent = chat_agent.to_messages(chat.messages(db, thread_id=thread.id))

    assert sent[0]["role"] == "system"
    assert "Almagest" in sent[0]["content"]
    assert all("an old prompt" not in m["content"] for m in sent)


def test_a_think_block_never_reaches_the_transcript(db: Session) -> None:
    thread = _thread(db)
    chat.append_message(db, thread=thread, role=ChatRole.USER, content="hi")
    model = FakeChatModel(reply="<think>weighing options</think>100 nF, in drawer 01.")

    reply = chat_agent.respond(model, chat.messages(db, thread_id=thread.id))

    assert reply.text == "100 nF, in drawer 01."


def test_an_empty_completion_says_so_rather_than_storing_a_blank(db: Session) -> None:
    """A model that emitted only a think block, or ran out of tokens. An empty
    bubble is uninterpretable; saying which happened is not."""
    thread = _thread(db)
    chat.append_message(db, thread=thread, role=ChatRole.USER, content="hi")

    reply = chat_agent.respond(
        FakeChatModel(reply="<think>...</think>"), chat.messages(db, thread_id=thread.id)
    )

    assert "returned nothing" in reply.text


def test_history_is_bounded_so_a_long_thread_keeps_working(db: Session) -> None:
    """Unbounded would be correct and would eventually exceed the context window,
    failing *every* message rather than dropping the oldest."""
    thread = _thread(db)
    for index in range(chat_agent.MAX_HISTORY_TURNS + 10):
        chat.append_message(db, thread=thread, role=ChatRole.USER, content=f"turn {index}")

    sent = chat_agent.to_messages(chat.messages(db, thread_id=thread.id))

    assert len(sent) == chat_agent.MAX_HISTORY_TURNS + 1  # +1 for the system prompt
    assert sent[-1]["content"] == f"turn {chat_agent.MAX_HISTORY_TURNS + 9}"


def test_no_model_configured_is_not_an_error(client: TestClient, db: Session) -> None:
    """The expensive half may be absent — never installed, out of GPU, scaled to
    zero for the co-tenant. The product degrades to something honest."""
    thread = _thread(db)
    db.commit()

    response = client.post(f"/api/chat/threads/{thread.id}/send", json={"content": "hello"})

    assert response.status_code == 200
    body = response.json()
    assert body["assistant"] is None
    assert "ALMAGEST_LLM_BASE_URL" in body["error"]
    # The turn survived, which is the point.
    assert body["user"]["content"] == "hello"


def test_the_users_turn_survives_an_unreachable_model(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """**The assertion that matters.** A model that cannot be reached must cost the
    reply and never the typing — that is the worst possible moment to lose what
    somebody just wrote."""
    thread = _thread(db)
    db.commit()

    class Dead:
        model = "dead"

        def complete(self, messages: object, tools: object = None) -> dict[str, object]:
            raise ModelUnavailable("connection refused")

    def dead(*_args: object, **_kwargs: object) -> Dead:
        return Dead()

    monkeypatch.setattr(chat_agent, "build_model", dead)

    response = client.post(f"/api/chat/threads/{thread.id}/send", json={"content": "keep this"})

    assert response.status_code == 200
    assert response.json()["assistant"] is None
    assert "connection refused" in response.json()["error"]

    stored = client.get(f"/api/chat/threads/{thread.id}").json()["messages"]
    assert [m["content"] for m in stored] == ["keep this"]


def test_a_reply_is_stored_with_the_model_that_produced_it(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """Recorded per message, because the model behind a thread changes — a local
    pass today, a frontier escalation tomorrow."""
    thread = _thread(db)
    db.commit()

    def fake(*_args: object, **_kwargs: object) -> FakeChatModel:
        return FakeChatModel(model="qwen3:8b", reply="Two.")

    monkeypatch.setattr(chat_agent, "build_model", fake)

    response = client.post(f"/api/chat/threads/{thread.id}/send", json={"content": "how many?"})

    assistant = response.json()["assistant"]
    assert assistant["content"] == "Two."
    assert assistant["model"] == "qwen3:8b"
    assert assistant["role"] == "assistant"


# ---------------------------------------------------------------------------
# What a failure says
# ---------------------------------------------------------------------------


def test_a_refused_connection_names_the_fix_not_the_stack() -> None:
    """`URLError calling http://.../v1/chat/completions` is accurate and useless —
    it names a URL the reader did not choose and a library they did not call.

    The overwhelmingly common cause is that no model server is running, because
    both Deployments default to zero and the reaper scales them down when chat has
    been idle. So the message says that, and names the command."""
    import urllib.error

    message = chat_agent.explain(
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
        "http://almagest-llm:11434",
        "qwen3:8b",
    )

    assert "make k8s-model" in message
    # And the raw text survives: a guess that turns out wrong must not hide the
    # evidence that would have shown it.
    assert "refused" in message.lower()


def test_a_timeout_explains_the_cold_start() -> None:

    message = chat_agent.explain(TimeoutError("timed out"), "http://x:1", "qwen3:8b")

    assert "VRAM" in message
    assert "try again" in message.lower()


def test_a_missing_model_names_the_model() -> None:
    message = chat_agent.explain(RuntimeError("HTTP 404: not found"), "http://x:1", "qwen3:70b")

    assert "qwen3:70b" in message


# ---------------------------------------------------------------------------
# Retrying
# ---------------------------------------------------------------------------


def test_a_retry_does_not_ask_the_question_twice(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """**A retry re-runs the pipeline; it is not a second question.**

    The user's turn is stored before the model is called, so when the model fails
    the question is already in the thread. An earlier version re-sent the text,
    which appended a duplicate and left the transcript saying somebody asked twice
    — visible to the reader, and fed back to the model as context on the next turn.
    """
    thread = _thread(db)
    db.commit()

    class Dead:
        model = "dead"

        def complete(self, messages: object, tools: object = None) -> dict[str, object]:
            raise ModelUnavailable("connection refused")

    def dead(*_args: object, **_kwargs: object) -> Dead:
        return Dead()

    monkeypatch.setattr(chat_agent, "build_model", dead)
    first = client.post(f"/api/chat/threads/{thread.id}/send", json={"content": "only once"})
    assert first.json()["assistant"] is None

    # The retry: no content at all.
    def alive(*_args: object, **_kwargs: object) -> FakeChatModel:
        return FakeChatModel(model="qwen3:8b", reply="Answered on the second try.")

    monkeypatch.setattr(chat_agent, "build_model", alive)
    second = client.post(f"/api/chat/threads/{thread.id}/send", json={})

    assert second.json()["assistant"]["content"] == "Answered on the second try."

    stored = client.get(f"/api/chat/threads/{thread.id}").json()["messages"]
    questions = [m["content"] for m in stored if m["role"] == "user"]
    assert questions == ["only once"], stored


def test_a_retry_with_nothing_to_retry_is_refused(client: TestClient, db: Session) -> None:
    """ "Try again" has no meaning without an unanswered question, and silently
    regenerating over an existing answer would rewrite history."""
    thread = _thread(db)
    db.commit()

    response = client.post(f"/api/chat/threads/{thread.id}/send", json={})

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "nothing_to_retry"
