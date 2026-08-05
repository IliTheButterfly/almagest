"""The tools chat can reach, and the loop that runs them.

## What this file is really guarding

**An answer built from memory rather than from rows.** The whole reason the tools
exist is that a model asked "what capacitors do I have" will otherwise either
refuse or invent. The first test below checks the model's answer is preceded by a
tool call that genuinely hit the database and came back naming the seeded part.

**A tool call the user cannot see.** `tool_calls_json` is stored on the assistant
turn so the transcript shows what the answer was built from. This system treats a
model's claim as evidence rather than as an answer everywhere else; chat must not
be the one place that stops.

**A loop costing the whole request.** A model that keeps calling tools without
answering is a real failure mode, and the round cap turns it into a sentence
rather than a hung connection.

**Milli-units leaking to the model.** Quantities are stored as thousandths. A model
handed `620000` will report "620,000 in stock", sincerely and uncorrectably.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import ChatKind, ChatRole
from app.services import chat, chat_agent, chat_tools
from app.services.chat_agent import FakeChatModel
from tests.factories import make_location, make_part


def _thread(db: Session) -> object:
    return chat.create_thread(db, kind=ChatKind.SEARCH)


def _search_call(query: str = "ceramic") -> dict[str, object]:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "c1",
                "function": {"name": "search_parts", "arguments": json.dumps({"query": query})},
            }
        ],
    }


def test_the_model_answers_from_rows_and_the_call_is_shown(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """The point of the whole exercise: asked what is in stock, the model looks."""
    make_part(db, "22uF 16V ceramic, 0805", mpn="DEMO-CAP-22U")
    thread = _thread(db)
    db.commit()

    model = FakeChatModel(
        model="qwen3:8b",
        scripted=[_search_call(), {"role": "assistant", "content": "One: a 22uF 0805 ceramic."}],
    )

    def build(*_args: object, **_kwargs: object) -> FakeChatModel:
        return model

    monkeypatch.setattr(chat_agent, "build_model", build)

    response = client.post(
        f"/api/chat/threads/{thread.id}/send", json={"content": "what ceramics do I have?"}
    )

    assistant = response.json()["assistant"]
    assert assistant["content"] == "One: a 22uF 0805 ceramic."
    # Shown, not hidden.
    assert "search_parts" in (assistant["tool_calls_json"] or "")

    # And the tool genuinely read the database rather than being stubbed: the
    # content handed back to the model names the seeded part.
    tool_turn = next(turn for turn in (model.seen or [])[-1] if turn.get("role") == "tool")
    assert "DEMO-CAP-22U" in str(tool_turn["content"])


def test_quantities_reach_the_model_in_whole_units(db: Session) -> None:
    """Stored as thousandths, handed over as units. A model shown `620000` reports
    620,000 in stock, and no prompt talks it out of that."""
    assert chat_tools._units(620_000) == 620.0
    assert chat_tools._units(None) == 0


def test_a_truncated_result_says_so(db: Session, monkeypatch) -> None:
    """A silently cut list reads as "that is all of them", and the model reports it
    as such — so the cut is stated and the model can narrow instead."""
    for index in range(chat_tools.MAX_ROWS + 3):
        make_part(db, f"resistor {index}", mpn=f"R-{index}")
    db.flush()

    out = json.loads(chat_tools.call(db, "search_parts", '{"query": "resistor"}'))

    assert out["count"] == chat_tools.MAX_ROWS
    assert "narrow" in out["truncated"]


def test_an_empty_query_lists_stock_rather_than_refusing(db: Session) -> None:
    """Found on the deployed model, not in a unit test.

    Asked "what parts do we have in stock", the 8B correctly called `search_parts`
    with `query: ""` — there is no search term in that question. The tool required
    one, refused, and the model then produced nothing at all. **The model was right
    and the schema was wrong**, so nothing is required now and an empty query means
    "list what there is".
    """
    make_part(db, "22uF 16V ceramic, 0805", mpn="DEMO-CAP-22U")
    db.flush()

    out = json.loads(chat_tools.call(db, "search_parts", '{"query": "", "in_stock_only": false}'))

    assert "error" not in out
    assert any(row["mpn"] == "DEMO-CAP-22U" for row in out["results"])


def test_an_empty_search_is_not_reported_as_an_empty_room(db: Session) -> None:
    """The tool description carries the distinction the MCP server also makes:
    nothing *recorded* as matching is not nothing in the room."""
    out = json.loads(chat_tools.call(db, "search_parts", '{"query": "nothing-like-this"}'))
    assert out["results"] == []

    description = chat_tools.TOOLS[0]["function"]["description"]
    assert "not the same as nothing being in the room" in description


def test_locations_can_be_listed(db: Session) -> None:
    make_location(db, "SMD drawer A")
    db.flush()

    out = json.loads(chat_tools.call(db, "list_locations", '{"query": "SMD"}'))

    assert any("SMD" in row["path"] for row in out["results"])


def test_an_unknown_tool_is_reported_to_the_model_not_raised(db: Session) -> None:
    """The model asked for something that does not exist. Telling it lets it recover
    on the next turn; raising would end the conversation over a correctable slip."""
    assert "no such tool" in chat_tools.call(db, "delete_everything", "{}")


def test_malformed_arguments_come_back_as_an_error_the_model_can_read(db: Session) -> None:
    assert "not valid JSON" in chat_tools.call(db, "search_parts", "{not json")


def test_a_tool_loop_gives_up_rather_than_hanging_the_request(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """A model that keeps calling tools without answering is looping."""
    thread = _thread(db)
    db.commit()

    def build(*_args: object, **_kwargs: object) -> FakeChatModel:
        return FakeChatModel(scripted=[dict(_search_call()) for _ in range(10)])

    monkeypatch.setattr(chat_agent, "build_model", build)

    response = client.post(f"/api/chat/threads/{thread.id}/send", json={"content": "loop"})

    assert "without reaching an answer" in response.json()["assistant"]["content"]


def test_no_session_means_no_tools_are_offered(db: Session) -> None:
    """Keeps `respond` usable where there is no database, and keeps the no-tools
    path testable rather than dead."""
    thread = _thread(db)
    chat.append_message(db, thread=thread, role=ChatRole.USER, content="hi")
    model = FakeChatModel(reply="no tools here")

    reply = chat_agent.respond(model, chat.messages(db, thread_id=thread.id), session=None)

    assert reply.tool_calls_json is None
    assert reply.text == "no tools here"
