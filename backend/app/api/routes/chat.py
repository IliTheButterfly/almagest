"""`/api/chat` — threads, turns, writeups and export (ADR 0018).

Storage and retrieval. **No model runs here and no agent loop lives here**, for
the reason ADR 0018 gives: an agent whose tools call back into a single-replica
SQLite writer, from inside that writer's own process, is a self-deadlock. What the
API owns is the transcript; what decides an assistant turn is somebody else's job,
and it reaches these routes over HTTP like any other client.

## Two history lists, one table

`GET /api/chat/threads?kind=search` and `?kind=project&project_id=N` are the two
lists. They differ in the UI and in retention, not in shape — see
`app.models.chat` for why that is a column rather than a second table.

## Every route here is `Excluded` from the MCP tool surface

An agent driving its own transcript store is a loop: the thing writing the
messages would be reading them back as context and writing again. `coverage.py`
records that decision explicitly rather than leaving it to be inferred.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.db.session import get_db
from app.models.chat import MAX_MESSAGE_CHARS, MAX_WRITEUP_CHARS, ChatThread, ChatWriteup
from app.models.enums import ChatKind, ChatRole
from app.services import chat
from app.services.chat import ChatError

router = APIRouter(prefix="/api/chat", tags=["chat"])

Title = Annotated[str, StringConstraints(min_length=1, max_length=200)]
MessageText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS)]
WriteupBody = Annotated[str, StringConstraints(min_length=1, max_length=MAX_WRITEUP_CHARS)]

_REASON_STATUS = {
    "message_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "writeup_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class ThreadRead(BaseModel):
    id: int
    kind: ChatKind
    project_id: int | None
    title: str
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: Cheap enough to include on the list, and it is what makes an empty thread
    #: distinguishable from one whose messages failed to load.
    message_count: int = 0


class MessageRead(BaseModel):
    id: int
    seq: int
    role: ChatRole
    content: str
    #: Shown in the UI, never hidden. A tool call the user cannot see is a fact
    #: they cannot check, and this whole pipeline treats a model's claim as
    #: evidence rather than as an answer.
    tool_calls_json: str | None
    model: str | None
    created_at: datetime


class ThreadDetail(BaseModel):
    thread: ThreadRead
    messages: list[MessageRead]


class ThreadCreate(BaseModel):
    kind: ChatKind
    project_id: RowId | None = None
    title: Title | None = None


class MessageCreate(BaseModel):
    role: ChatRole = ChatRole.USER
    content: MessageText
    model: str | None = Field(default=None, max_length=128)
    tool_calls_json: str | None = None


class ThreadArchive(BaseModel):
    archived: bool = True


class WriteupRead(BaseModel):
    id: int
    title: str
    body_md: str
    origin_thread_id: int | None
    project_id: int | None
    created_at: datetime


class WriteupCreate(BaseModel):
    """Create a writeup, and optionally post it in the same call.

    Posting is a separate *act* in the service (`create_writeup` then
    `post_writeup`) but one *request* here, because "make a writeup and send it to
    the Nixie clock project" is one intention and splitting it across two
    round-trips invites the second one failing and leaving an orphan.
    """

    title: Title
    body_md: WriteupBody
    origin_thread_id: RowId | None = None
    project_id: RowId | None = None
    #: Post into this existing thread once created.
    post_to_thread_id: RowId | None = None
    #: Or start a new project thread and post there. Creating a *project* is
    #: deliberately not offered — see ADR 0018: letting a chat mint project rows on
    #: inference is how a projects list fills with `Untitled 4`.
    post_to_new_project_id: RowId | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_thread(db: Session, thread_id: int) -> ChatThread:
    thread = db.get(ChatThread, thread_id)
    if thread is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "no_such_thread", "message": f"no chat thread with id {thread_id}"},
        )
    return thread


def _thread_read(db: Session, thread: ChatThread) -> ThreadRead:
    return ThreadRead(
        id=thread.id,
        kind=ChatKind(thread.kind),
        project_id=thread.project_id,
        title=thread.title,
        archived_at=thread.archived_at,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        message_count=len(chat.messages(db, thread_id=thread.id)),
    )


def _message_read(row: object) -> MessageRead:
    return MessageRead.model_validate(row, from_attributes=True)


def _refuse(error: ChatError) -> HTTPException:
    return HTTPException(
        _REASON_STATUS.get(error.reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
        detail={"reason": error.reason, "message": str(error)},
    )


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@router.get("/threads", response_model=list[ThreadRead])
def list_chat_threads(
    kind: ChatKind,
    project_id: RowId | None = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    db: Session = Depends(get_db),
) -> list[ThreadRead]:
    """One history list. `kind` is required rather than defaulted, deliberately:
    the whole point of two surfaces is that a caller says which one it means, and a
    default would quietly mix a project's threads into the search list."""
    threads = chat.list_threads(
        db, kind=kind, project_id=project_id, include_archived=include_archived, limit=limit
    )
    return [_thread_read(db, thread) for thread in threads]


@router.post("/threads", response_model=ThreadDetail, status_code=status.HTTP_201_CREATED)
def create_chat_thread(request: ThreadCreate, db: Session = Depends(get_db)) -> ThreadDetail:
    try:
        thread = chat.create_thread(
            db, kind=request.kind, project_id=request.project_id, title=request.title
        )
    except ChatError as error:
        raise _refuse(error) from error
    detail = ThreadDetail(thread=_thread_read(db, thread), messages=[])
    db.commit()
    return detail


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
def read_chat_thread(thread_id: RowId, db: Session = Depends(get_db)) -> ThreadDetail:
    thread = _require_thread(db, thread_id)
    return ThreadDetail(
        thread=_thread_read(db, thread),
        messages=[_message_read(row) for row in chat.messages(db, thread_id=thread.id)],
    )


@router.post("/threads/{thread_id}/messages", response_model=MessageRead)
def append_chat_message(
    thread_id: RowId, request: MessageCreate, db: Session = Depends(get_db)
) -> MessageRead:
    """Add one turn.

    There is no route that *updates* a message, and that absence is the enforcement
    of ADR 0018's append-only rule: a transcript whose history no longer matches
    what the model was shown cannot explain the answer it gave.
    """
    thread = _require_thread(db, thread_id)
    try:
        message = chat.append_message(
            db,
            thread=thread,
            role=request.role,
            content=request.content,
            model=request.model,
            tool_calls_json=request.tool_calls_json,
        )
    except ChatError as error:
        raise _refuse(error) from error
    read = _message_read(message)
    db.commit()
    return read


@router.post("/threads/{thread_id}/archive", response_model=ThreadRead)
def archive_chat_thread(
    thread_id: RowId, request: ThreadArchive, db: Session = Depends(get_db)
) -> ThreadRead:
    thread = _require_thread(db, thread_id)
    chat.archive_thread(db, thread=thread, archived=request.archived)
    read = _thread_read(db, thread)
    db.commit()
    return read


@router.get("/threads/{thread_id}/export")
def export_chat_thread(
    thread_id: RowId,
    fmt: Annotated[str, Query(alias="format", pattern="^(md|json)$")] = "md",
    db: Session = Depends(get_db),
) -> Response:
    """The transcript, for pasting into another model.

    That is the point of export and it shapes the format: a local 8B is not the
    right tool for every question, and the way out has to be one copy rather than a
    re-explanation. `md` carries a front-matter header; `json` round-trips roles and
    tool calls intact.
    """
    thread = _require_thread(db, thread_id)
    turns = chat.messages(db, thread_id=thread.id)
    if fmt == "json":
        payload = ThreadDetail(
            thread=_thread_read(db, thread),
            messages=[_message_read(row) for row in turns],
        )
        return Response(
            payload.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="chat-{thread.id}.json"'},
        )
    return Response(
        chat.to_markdown(thread, turns),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="chat-{thread.id}.md"'},
    )


# ---------------------------------------------------------------------------
# Writeups
# ---------------------------------------------------------------------------


@router.post("/writeups", response_model=WriteupRead, status_code=status.HTTP_201_CREATED)
def create_chat_writeup(request: WriteupCreate, db: Session = Depends(get_db)) -> WriteupRead:
    """Create a writeup, and post it where asked.

    The two destinations are mutually exclusive and both optional: a writeup that
    is created and posted nowhere is a legitimate draft.
    """
    if request.post_to_thread_id is not None and request.post_to_new_project_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "ambiguous_destination",
                "message": "post to an existing thread or a new project thread, not both",
            },
        )
    try:
        writeup = chat.create_writeup(
            db,
            title=request.title,
            body_md=request.body_md,
            origin_thread_id=request.origin_thread_id,
            project_id=request.project_id,
        )
        if request.post_to_thread_id is not None:
            chat.post_writeup(
                db, writeup=writeup, thread=_require_thread(db, request.post_to_thread_id)
            )
        elif request.post_to_new_project_id is not None:
            destination = chat.create_thread(
                db,
                kind=ChatKind.PROJECT,
                project_id=request.post_to_new_project_id,
                title=writeup.title,
            )
            chat.post_writeup(db, writeup=writeup, thread=destination)
    except ChatError as error:
        raise _refuse(error) from error

    read = WriteupRead.model_validate(writeup, from_attributes=True)
    db.commit()
    return read


@router.get("/writeups/{writeup_id}/export")
def export_chat_writeup(
    writeup_id: RowId,
    db: Session = Depends(get_db),
) -> Response:
    writeup = db.get(ChatWriteup, writeup_id)
    if writeup is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "no_such_writeup", "message": f"no writeup with id {writeup_id}"},
        )
    body = f"# {writeup.title}\n\n{writeup.body_md}\n"
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="writeup-{writeup.id}.md"'},
    )
