"""Chat threads, their messages, and the writeups that move between them (ADR 0018).

Two conversational surfaces with **separate history lists** — a disposable one for
inventory questions and a durable one bound to a project — plus a way to take work
from the first to the second.

## One table, discriminated by `kind`

Two history lists is a `WHERE kind = ?`, not two tables. They differ in the UI and
in retention, not in shape, and a second table would duplicate every message
relationship to express a difference that is one column wide.

`kind` is `sa.String` + `StrEnumType`, never `sa.Enum` — a test greps
`sqlite_master` for `CHECK` and SQLite cannot alter one. That is load-bearing here
rather than ceremonial: a third kind (a thread hanging off a part, off a build) is
the obvious next request, and it must stay a one-line change.

## Messages are append-only, but not trigger-enforced

`stock_ledger` has DB triggers because a wrong balance is a correctness failure
that compounds. A rewritten transcript is not that — it is a *product* failure, and
a different one: a thread whose history no longer matches what the model was
actually shown cannot explain the answer it gave.

So editing a message creates a new message, by convention and by the absence of any
route that updates one. The convention is enforced where it is cheap (there is no
`PATCH`) and not defended against a determined `UPDATE`, because the cost of the
trigger — every future migration touching this table having to work around it —
buys protection against a threat that is a person deciding to rewrite history in
SQL, which no trigger stops anyway.

## A writeup is a row, not a message

`chat_writeups` exists **independently of both threads**. That is the whole point
of ADR 0018's "make a writeup and send it to the project": the artefact survives
either thread being archived, and posting the same writeup into two projects links
it twice rather than copying it. A writeup stored as "the text of message 47" would
lose its identity the moment anybody wanted it in a second place.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin
from app.models.enums import ChatKind, ChatRole
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: Longest a single message may be. Generous — a pasted BOM or a model's long
#: answer both live here — but bounded, because the body is JSON held in memory
#: and an agent looping is how a megabyte arrives.
MAX_MESSAGE_CHARS = 200_000

#: Longest a writeup body may be. Larger than a message: a writeup is the thing
#: somebody exports and hands to another model, so it is expected to be long.
MAX_WRITEUP_CHARS = 500_000


class ChatThread(Base, TimestampMixin):
    """One conversation. `kind` decides which history list it appears in."""

    __tablename__ = "chat_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(StrEnumType(ChatKind), nullable=False)
    #: Set for `project` threads, NULL for `search` ones. Deliberately **not** a
    #: separate table per kind — see the module docstring — and deliberately
    #: nullable rather than pointing search threads at a sentinel project, which
    #: would make "this project's threads" a query that has to know about the
    #: sentinel.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    #: A short human label. Generated from the first message when the thread is
    #: created and editable afterwards; never derived on read, because a title that
    #: changes as the conversation grows makes the history list unstable to scan.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Archived threads stay readable and stop appearing in the list. Search
    #: threads are expected to be noisy and disposable; project threads are never
    #: auto-archived, which is a policy the *caller* enforces — nothing here knows
    #: about it.
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        # The two history lists, in one index: "search threads, newest first" and
        # "this project's threads, newest first" are both prefixes of it.
        Index("ix_chat_threads_list", "kind", "project_id", "updated_at"),
    )


class ChatMessage(Base):
    """One turn. Append-only by convention — see the module docstring."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    #: Position within the thread, 1-based. Stored rather than derived from `id`
    #: so a transcript's order does not depend on a global autoincrement that
    #: interleaves with every other thread — and so "message 3 of this thread" is a
    #: thing a person and an export can both name.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(StrEnumType(ChatRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Tool calls made while producing this message, as JSON. Kept so the UI can
    #: **show** them: a tool call the user cannot see is a fact they cannot check,
    #: and this pipeline's whole posture is that a model's claim is evidence rather
    #: than an answer.
    tool_calls_json: Mapped[str | None] = mapped_column(Text)
    #: Which model produced an assistant turn. NULL for a user turn. Recorded per
    #: message rather than per thread because the model behind a thread changes —
    #: a local pass today, a frontier escalation tomorrow — and a transcript that
    #: cannot say which one said what is not a transcript.
    model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # One message per position. This is what makes an interrupted send safe to
        # retry: the retry either lands the same row or collides, rather than
        # appending a duplicate turn nobody asked for.
        UniqueConstraint("thread_id", "seq", name="uq_chat_messages_thread_seq"),
        Index("ix_chat_messages_thread", "thread_id", "seq"),
    )


class ChatWriteup(Base, TimestampMixin):
    """A durable artefact produced in one thread, postable into others."""

    __tablename__ = "chat_writeups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Markdown. Stored as text rather than as a `documents` blob because it is
    #: written, edited and searched as text — the blob store is content-addressed
    #: and immutable, which is right for a fetched PDF and wrong for a draft.
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    #: Where it was written. Kept even after that thread is archived, because "this
    #: came out of a conversation, and here is which one" is most of a writeup's
    #: provenance.
    origin_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="SET NULL")
    )
    #: The project it is *about*, if any. Distinct from the threads it has been
    #: posted into: a writeup can be posted to three threads and still belong to
    #: one project.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))


class ChatWriteupPost(Base):
    """One writeup, posted into one thread, as one message."""

    __tablename__ = "chat_writeup_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    writeup_id: Mapped[int] = mapped_column(
        ForeignKey("chat_writeups.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    #: The message the post became. `SET NULL` rather than `CASCADE`: the fact that
    #: a writeup was posted somewhere outlives the message row, and losing the link
    #: should not lose the record of the posting.
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # Posting the same writeup into the same thread twice is a double-click,
        # not an intention.
        UniqueConstraint("writeup_id", "thread_id", name="uq_chat_writeup_posts"),
    )
