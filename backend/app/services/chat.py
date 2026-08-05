"""Threads, turns and writeups — the storage half of chat (ADR 0018).

**No model runs here.** This module appends rows and reads them back; the agent
loop that decides what an assistant turn says lives outside the API, for the reason
ADR 0018 gives: an agent whose tools call back into a single-replica SQLite writer,
from inside that writer's own process, is a self-deadlock waiting for a busy
connection pool.

What that leaves here is small and worth stating precisely, because the temptation
is to grow it.

## `seq` is assigned here, not by the caller

A thread's turn order is `(thread_id, seq)` with a unique constraint, and `seq` is
`max + 1` computed inside the same transaction as the insert. Two consequences,
both deliberate:

* A retried send either lands the same row or collides — it cannot append a
  duplicate turn. That matters because the send path is a streaming request that a
  phone will drop halfway through more often than anything else in this system.
* Order does not depend on `chat_messages.id`, which is a global autoincrement
  interleaving every thread. "Message 3 of this thread" is then a thing an export,
  a URL and a person can all name the same way.

## Editing is appending

There is no `update_message`, and its absence is the enforcement. A transcript
whose history no longer matches what the model was shown cannot explain the answer
it gave, which is the one job a transcript has. See `app.models.chat` for why this
is convention rather than a trigger.

## A writeup is created here; posting it is a second, separate act

`create_writeup` writes the artefact. `post_writeup` puts it into a thread and
records the link. Keeping them apart is what lets one writeup reach two projects
without being copied — and what lets a writeup outlive the thread it came from.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.chat import (
    MAX_MESSAGE_CHARS,
    MAX_WRITEUP_CHARS,
    ChatMessage,
    ChatThread,
    ChatWriteup,
    ChatWriteupPost,
)
from app.models.enums import ChatKind, ChatRole
from app.models.types import utcnow

#: How much of the first user turn becomes the thread's title when none is given.
#: Short: the history list is scanned, not read, and a title that wraps is a title
#: that stops being a landmark.
TITLE_CHARS = 60


class ChatError(RuntimeError):
    """A refusal the routes map to an HTTP status."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def title_from(text: str) -> str:
    """A thread title from its opening turn.

    Generated once at creation and stored, never derived on read. A title that
    changes as the conversation grows makes the history list unstable to scan —
    the thread you were looking for is not where you last saw it.
    """
    flat = " ".join(text.split())
    if len(flat) <= TITLE_CHARS:
        return flat or "New chat"
    return flat[: TITLE_CHARS - 1].rstrip() + "…"


def create_thread(
    session: Session,
    *,
    kind: ChatKind,
    project_id: int | None = None,
    title: str | None = None,
) -> ChatThread:
    """Start a conversation.

    A `project` thread must name a project and a `search` thread must not. Refused
    rather than coerced: a project thread with no project would be invisible in
    both history lists, and a search thread carrying one would appear in a
    project's list without belonging to it.
    """
    if kind is ChatKind.PROJECT and project_id is None:
        raise ChatError("project_required", "a project thread must name a project")
    if kind is ChatKind.SEARCH and project_id is not None:
        raise ChatError("project_not_allowed", "a search thread cannot belong to a project")

    thread = ChatThread(kind=kind, project_id=project_id, title=title or "New chat")
    session.add(thread)
    session.flush()
    return thread


def list_threads(
    session: Session,
    *,
    kind: ChatKind,
    project_id: int | None = None,
    include_archived: bool = False,
    limit: int = 50,
) -> list[ChatThread]:
    """One history list. `kind` is what separates them — see `app.models.chat`."""
    query = select(ChatThread).where(ChatThread.kind == kind)
    if project_id is not None:
        query = query.where(ChatThread.project_id == project_id)
    if not include_archived:
        query = query.where(ChatThread.archived_at.is_(None))
    return list(
        session.execute(query.order_by(ChatThread.updated_at.desc()).limit(limit)).scalars().all()
    )


def messages(session: Session, *, thread_id: int) -> list[ChatMessage]:
    return list(
        session.execute(
            select(ChatMessage).where(ChatMessage.thread_id == thread_id).order_by(ChatMessage.seq)
        )
        .scalars()
        .all()
    )


def append_message(
    session: Session,
    *,
    thread: ChatThread,
    role: ChatRole,
    content: str,
    model: str | None = None,
    tool_calls_json: str | None = None,
) -> ChatMessage:
    """Add one turn, assigning `seq` inside this transaction.

    Also titles the thread from its first user turn, and touches `updated_at` so
    the history list orders by "last spoken in" rather than "created" — which is
    what makes a long-running project thread stay findable.
    """
    if not content.strip():
        raise ChatError("empty_message", "a message needs content")
    if len(content) > MAX_MESSAGE_CHARS:
        raise ChatError(
            "message_too_large", f"a message may be at most {MAX_MESSAGE_CHARS} characters"
        )

    highest = session.execute(
        select(func.coalesce(func.max(ChatMessage.seq), 0)).where(
            ChatMessage.thread_id == thread.id
        )
    ).scalar_one()

    message = ChatMessage(
        thread_id=thread.id,
        seq=highest + 1,
        role=role,
        content=content,
        model=model,
        tool_calls_json=tool_calls_json,
    )
    session.add(message)

    if role is ChatRole.USER and thread.title == "New chat":
        thread.title = title_from(content)
    thread.updated_at = utcnow()
    session.flush()
    return message


def archive_thread(session: Session, *, thread: ChatThread, archived: bool = True) -> ChatThread:
    """Hide a thread from its list without deleting it.

    Search threads are expected to accumulate and be archived freely; project
    threads are never archived automatically. That policy lives in the caller —
    nothing here knows the difference, which is what keeps a future third kind from
    having to be taught about it.
    """
    thread.archived_at = utcnow() if archived else None
    session.flush()
    return thread


# ---------------------------------------------------------------------------
# Writeups
# ---------------------------------------------------------------------------


def create_writeup(
    session: Session,
    *,
    title: str,
    body_md: str,
    origin_thread_id: int | None = None,
    project_id: int | None = None,
) -> ChatWriteup:
    if not body_md.strip():
        raise ChatError("empty_writeup", "a writeup needs a body")
    if len(body_md) > MAX_WRITEUP_CHARS:
        raise ChatError(
            "writeup_too_large", f"a writeup may be at most {MAX_WRITEUP_CHARS} characters"
        )
    writeup = ChatWriteup(
        title=title.strip() or "Untitled writeup",
        body_md=body_md,
        origin_thread_id=origin_thread_id,
        project_id=project_id,
    )
    session.add(writeup)
    session.flush()
    return writeup


def post_writeup(session: Session, *, writeup: ChatWriteup, thread: ChatThread) -> ChatWriteupPost:
    """Put a writeup into a thread as a message, and record the link.

    Idempotent by the unique constraint on `(writeup_id, thread_id)`: posting the
    same writeup into the same thread twice is a double-click, not an intention, so
    the second call returns the first posting rather than appending a second copy.
    """
    existing = session.execute(
        select(ChatWriteupPost).where(
            ChatWriteupPost.writeup_id == writeup.id,
            ChatWriteupPost.thread_id == thread.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    message = append_message(
        session,
        thread=thread,
        role=ChatRole.ASSISTANT,
        content=f"## {writeup.title}\n\n{writeup.body_md}",
    )
    post = ChatWriteupPost(writeup_id=writeup.id, thread_id=thread.id, message_id=message.id)
    session.add(post)
    session.flush()
    return post


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def to_markdown(thread: ChatThread, turns: Sequence[ChatMessage]) -> str:
    """A transcript as markdown with a front-matter header.

    Sized for pasting into another model's context window, which is the entire
    reason export exists: a local 8B is not the right tool for every question, and
    the way out has to be one copy rather than a re-explanation.

    System turns are included. They are what the model was actually shown, and an
    export that silently drops them hands the next model a conversation whose
    answers do not follow from its visible inputs.
    """
    head = [
        "---",
        f'title: "{thread.title}"',
        f"kind: {thread.kind}",
        f"exported_at: {utcnow().isoformat()}",
        "---",
        "",
        f"# {thread.title}",
        "",
    ]
    body: list[str] = []
    for turn in turns:
        body.append(f"## {turn.role}")
        if turn.model:
            body.append(f"*{turn.model}*")
        body.append("")
        body.append(turn.content)
        body.append("")
    return "\n".join(head + body)
