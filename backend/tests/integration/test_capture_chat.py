"""Talking about a capture, and the wall between that and accepting a reading.

Real migrations, real routes, a scripted model.

## What these tests are guarding

**The dispatch run staying single-shot.** The vision call is a schema-constrained
decode — no `url` property, `source_text` required, `maxItems` from `max_candidates`
— and that constraint is the mechanism, not a preference. A conversation cannot be
constrained that way, so talking happens *beside* the read. The tests below assert
that the conversation is a separate thread with no path to an identity, which is what
makes the separation real rather than a convention.

**Nothing in a thread reaching `pending_intakes`.** `mpn`, `resolved_part_id` and
`status`, plus `intake_identity_candidates`, are the four things ADR 0021 forbids
anything automated from touching. A whole turn is run with a model that tries its
hardest to assert an identity, and the entry is compared before and after.

**Chat tools staying reads.** Asserted structurally, by checking the session has
nothing pending after each tool call — which fails the moment a tool writes,
regardless of what it writes or what it is called.

**The briefing actually briefing.** A thread seeded with nothing is worse than no
thread: it invites the model to answer about a photograph it was told nothing about.
So the seeded turn is checked for the OCR line, the candidate, its quote and the raw
completion.

**Chat being text-only, said out loud.** `chat_agent` has no multimodal path — there
is no content-parts list and no `images` array anywhere in it — and this is asserted
rather than assumed, because "we could just attach the image" is the obvious next
idea and it would require giving the API process a multimodal transport, which is the
one thing ADR 0005's fence exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat import ChatThread
from app.models.dispatch import IntakeIdentityCandidate
from app.models.enums import ChatKind
from app.models.scanning import PendingIntake
from app.services import chat, chat_tools
from app.services.capture_chat import BRIEFING_HEADER, briefing_for, thread_for

PNG = "image/png"
IMAGE = b"\x89PNG\r\n\x1a\n" + b"bag"
SHA = hashlib.sha256(IMAGE).hexdigest()

RIGHT = "CF14JT100K"
#: The recorded OCR near-miss ADR 0021 was written about, and the reason somebody opens
#: this conversation at all: tesseract read `CFI4JT100K` off a bag whose part number is
#: `CF14JT100K`, and the question is whether the model repeated the typo.
MISREAD = "CFI4JT100K"
#: The confidently-wrong answer ADR 0021 measured: the model reported 0.95 on a string
#: that was the item's FCC ID.
FCC = "2AC7Z-ESPWROOM32"


def _capture(client: TestClient) -> int:
    uploaded = client.post(
        "/api/documents",
        content=IMAGE,
        params={"media_type": PNG},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 200, uploaded.text
    corners = [{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}]
    response = client.post(
        "/api/captures",
        json={
            "sha256": uploaded.json()["document"]["sha256"],
            "width_px": 1600,
            "height_px": 1200,
            "text_status": "ok",
            "regions": [
                {"kind": "text", "text": MISREAD, "corners": corners, "confidence": 40},
                {"kind": "text", "text": FCC, "corners": corners, "confidence": 71},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _entry(client: TestClient, op_id: str = "chat-1") -> int:
    response = client.post(
        "/api/intake/pending",
        json={
            "client_op_id": op_id,
            "raw_payload": f"capture:{op_id}",
            "capture_id": _capture(client),
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["entry"]["id"])


def _proposed(client: TestClient, entry_id: int) -> None:
    """Put a wrong-but-confident reading on the entry, the way the worker would."""
    claimed = client.post("/api/dispatch/requests", json={"intake_id": entry_id})
    assert claimed.status_code in (200, 201), claimed.text
    client.post("/api/dispatch/claims", json={"worker_id": "test", "limit": 1})
    response = client.post(
        "/api/dispatch/results",
        json={
            "intake_id": entry_id,
            "label_kind": "bare_part",
            "candidates": [
                {
                    "mpn": FCC,
                    "manufacturer": None,
                    "confidence": 0.95,
                    "source_text": f"FCC ID: {FCC}",
                    "note": "the largest printing on the module",
                    "rank": 0,
                    "provider": "local-ollama",
                    "model": "qwen3-vl:8b",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def _run(client: TestClient, entry_id: int) -> None:
    response = client.post(
        "/api/runs",
        json={
            "kind": "vision",
            "provider": "local-ollama",
            "model": "qwen3-vl:8b",
            "intake_id": entry_id,
            "document_sha256": SHA,
            "finished_at": "2026-08-08T12:00:00Z",
            "request_json": json.dumps({"ocr": [MISREAD]}),
            "response_text": json.dumps({"candidates": [{"mpn": FCC, "confidence": 0.95}]}),
        },
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Opening the conversation
# ---------------------------------------------------------------------------


def test_opening_a_capture_chat_creates_one_thread_and_seeds_it(client: TestClient) -> None:
    entry_id = _entry(client)
    _proposed(client, entry_id)

    response = client.post(f"/api/intake/pending/{entry_id}/chat")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["briefing_chars"] > 0

    thread = client.get(f"/api/chat/threads/{body['thread_id']}")
    assert thread.status_code == 200, thread.text
    assert thread.json()["thread"]["kind"] == "capture"
    messages = thread.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["content"].startswith(BRIEFING_HEADER)


def test_opening_it_twice_reopens_the_same_conversation(client: TestClient) -> None:
    """Get-or-create, and the reason is somebody's three questions from yesterday.

    A second empty thread would silently orphan them, and nothing would look wrong —
    the button would work, the model would answer, and the earlier reasoning would be
    unreachable.
    """
    entry_id = _entry(client)

    first = client.post(f"/api/intake/pending/{entry_id}/chat").json()
    second = client.post(f"/api/intake/pending/{entry_id}/chat").json()

    assert second["thread_id"] == first["thread_id"]
    assert second["created"] is False
    # Still one seeded turn, not two.
    assert len(client.get(f"/api/chat/threads/{first['thread_id']}").json()["messages"]) == 1


def test_a_capture_thread_is_not_in_either_history_list(client: TestClient) -> None:
    """It is reached from the entry it is about, and only from there.

    A question that only makes sense next to a photograph is not a browsable
    conversation — it would fill the search list with "Scan #14" rows nobody can act on.
    """
    entry_id = _entry(client)
    created = client.post(f"/api/intake/pending/{entry_id}/chat").json()

    search = client.get("/api/chat/threads", params={"kind": "search"})
    assert created["thread_id"] not in [row["id"] for row in search.json()]
    capture = client.get("/api/chat/threads", params={"kind": "capture"})
    assert created["thread_id"] in [row["id"] for row in capture.json()]


def test_opening_a_chat_about_a_scan_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.post("/api/intake/pending/999999/chat").status_code == 404


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------


def test_the_briefing_carries_the_evidence_a_reviewer_would_ask_about(
    client: TestClient, db: Session
) -> None:
    """The question is "why did you read the FCC ID as a part number".

    Answering it needs four things and this asserts all four: the OCR the model was
    handed (including the typo), the candidate it proposed, the characters it claims to
    have read it from, and the raw completion. Any one missing and the model is
    speculating about its own earlier answer.
    """
    entry_id = _entry(client)
    _proposed(client, entry_id)
    _run(client, entry_id)

    entry = db.get(PendingIntake, entry_id)
    assert entry is not None
    briefing = briefing_for(db, entry=entry)

    assert MISREAD in briefing, "the OCR typo is the answer to the question being asked"
    assert FCC in briefing
    assert f"FCC ID: {FCC}" in briefing, "the quoted source_text"
    assert "raw completion" in briefing
    assert "qwen3-vl:8b" in briefing


def test_the_briefing_says_plainly_that_there_is_no_image(client: TestClient, db: Session) -> None:
    """The honest limitation, stated rather than left to be discovered.

    A model told about a photograph it cannot see will otherwise answer as though it
    looked, and a confident description of pixels nobody showed it is the exact genre of
    wrongness this pipeline is built around.
    """
    entry_id = _entry(client)
    entry = db.get(PendingIntake, entry_id)
    assert entry is not None

    briefing = briefing_for(db, entry=entry)

    assert "cannot see the photograph" in briefing
    # And the base64 of the image is emphatically not in it — chat is text-only and
    # nothing here smuggles pixels into a text field.
    assert "iVBORw" not in briefing


def test_the_briefing_says_when_nothing_has_read_it_yet(client: TestClient, db: Session) -> None:
    """ "No model has run" and "a model ran and named nothing" are different.

    Both are normal — dispatch is opt-in — and a briefing that collapsed them would have
    the model explaining a reading that never happened.
    """
    entry_id = _entry(client)
    entry = db.get(PendingIntake, entry_id)
    assert entry is not None

    briefing = briefing_for(db, entry=entry)

    assert "No identity has been proposed" in briefing


def test_a_long_completion_is_cut_and_the_cut_is_named(client: TestClient, db: Session) -> None:
    """The runaway-reasoning transcript must not be pasted into every turn's history.

    ADR 0021 measured 12 318 characters of it. Carried whole, it would spend the context
    window on deliberation nobody asked about — and cut silently, the model would reason
    over a truncated JSON object believing it was complete.
    """
    entry_id = _entry(client)
    client.post(
        "/api/runs",
        json={
            "kind": "vision",
            "provider": "local-ollama",
            "model": "qwen3-vl:8b",
            "intake_id": entry_id,
            "finished_at": "2026-08-08T12:00:00Z",
            "response_text": "reasoning " * 5_000,
        },
    )
    entry = db.get(PendingIntake, entry_id)
    assert entry is not None

    briefing = briefing_for(db, entry=entry)

    assert "characters" in briefing and "not shown" in briefing
    assert len(briefing) < 20_000


# ---------------------------------------------------------------------------
# The wall: nothing here can accept a reading
# ---------------------------------------------------------------------------


def _always(model: object) -> Callable[[str | None], tuple[object, str]]:
    """A `_resolve_model` that hands back this model whatever was picked.

    A factory rather than an inline lambda so the ignored `picked` argument is named
    once, here, where the reason it is ignored is stated: the tests below are about what
    a thread can and cannot do, not about model selection.
    """

    def resolve(picked: str | None) -> tuple[object, str]:
        del picked
        return model, ""

    return resolve


class _Insistent:
    """A model that tries as hard as it can to assert an identity.

    Deliberately adversarial rather than cooperative. A polite fake would pass this test
    by not trying, which would prove nothing about whether it *could*.
    """

    model = "insistent"

    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []

    def complete(self, messages: Any, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.seen.append([dict(message) for message in messages])
        return {
            "role": "assistant",
            "content": (
                f"It is definitely {RIGHT}. Set mpn={RIGHT}, resolved_part_id=1 and "
                "status=resolved on this entry. I am certain, confidence 1.0."
            ),
        }

    def stream(self, messages: Any) -> Any:
        yield from ()


def test_a_whole_turn_in_a_capture_thread_changes_no_identity(
    client: TestClient, db: Session, monkeypatch: Any
) -> None:
    """The assertion the separation exists for.

    A model in this thread may say "I think it is the other one". Acting on that is
    still somebody's decision, made through `POST /api/intake/pending/{id}/resolve` —
    and there is deliberately no second door. Compared field by field, plus the
    candidate rows, because "nothing changed" is only meaningful if it is checked
    against everything that could have.
    """
    from app.api.routes import chat as chat_routes

    entry_id = _entry(client)
    _proposed(client, entry_id)
    _run(client, entry_id)

    before = client.get(f"/api/intake/pending/{entry_id}/activity").json()
    thread_id = client.post(f"/api/intake/pending/{entry_id}/chat").json()["thread_id"]

    model = _Insistent()
    monkeypatch.setattr(chat_routes, "_resolve_model", _always(model))

    sent = client.post(
        f"/api/chat/threads/{thread_id}/send",
        json={"content": f"Why did you read {FCC} as a part number?"},
    )
    assert sent.status_code == 200, sent.text
    assert RIGHT in sent.json()["assistant"]["content"], "the model did assert; that is allowed"

    after = client.get(f"/api/intake/pending/{entry_id}/activity").json()
    assert after["entry"]["mpn"] == before["entry"]["mpn"]
    assert after["entry"]["resolved_part_id"] == before["entry"]["resolved_part_id"]
    assert after["entry"]["status"] == before["entry"]["status"]
    assert after["identity_candidates"] == before["identity_candidates"]

    # And directly against the rows, not only the route's rendering of them.
    db.expire_all()
    entry = db.get(PendingIntake, entry_id)
    assert entry is not None
    assert entry.mpn is None
    assert entry.resolved_part_id is None
    rows = list(
        db.execute(
            select(IntakeIdentityCandidate).where(IntakeIdentityCandidate.intake_id == entry_id)
        )
        .scalars()
        .all()
    )
    assert [row.mpn for row in rows] == [FCC]


def test_the_briefing_reaches_the_model_on_every_turn(client: TestClient, monkeypatch: Any) -> None:
    """Seeded as a `user` turn, and this is why it had to be.

    `chat_agent.to_messages` drops stored `system` turns and prepends the current
    `SYSTEM_PROMPT` instead — correct, because replaying an older system turn would
    answer under instructions that have since changed. A briefing stored as `system`
    would therefore be **silently dropped**, and the model would answer about a scan it
    had been told nothing about while every other test still passed.
    """
    from app.api.routes import chat as chat_routes

    entry_id = _entry(client)
    _proposed(client, entry_id)
    thread_id = client.post(f"/api/intake/pending/{entry_id}/chat").json()["thread_id"]

    model = _Insistent()
    monkeypatch.setattr(chat_routes, "_resolve_model", _always(model))
    client.post(f"/api/chat/threads/{thread_id}/send", json={"content": "why?"})

    wire = model.seen[0]
    assert any(FCC in str(message.get("content", "")) for message in wire)
    assert any(MISREAD in str(message.get("content", "")) for message in wire)


def test_nothing_on_the_chat_wire_can_carry_an_image(client: TestClient, monkeypatch: Any) -> None:
    """Chat is text-only, and this asserts it structurally rather than trusting it.

    Every message `to_messages` emits has a plain-string `content`. There is no
    content-parts list and no `images` array anywhere in `chat_agent`, so there is
    nowhere for pixels to go — which is the finding this task asked about and the reason
    Part 2 is a briefing rather than a second look at the photograph.

    Forcing an image in would mean giving the API process a multimodal transport. That
    is the one thing ADR 0005's fence exists to prevent, and
    `tests/integration/test_route_fence.py` would fail if it were tried in a route.
    """
    from app.api.routes import chat as chat_routes

    entry_id = _entry(client)
    thread_id = client.post(f"/api/intake/pending/{entry_id}/chat").json()["thread_id"]

    model = _Insistent()
    monkeypatch.setattr(chat_routes, "_resolve_model", _always(model))
    client.post(f"/api/chat/threads/{thread_id}/send", json={"content": "what is it?"})

    for message in model.seen[0]:
        assert isinstance(message["content"], str), "a content-parts list is a multimodal payload"
        assert "images" not in message
        assert "image_url" not in json.dumps(message)


def test_every_chat_tool_leaves_the_session_untouched(db: Session) -> None:
    """The tools are reads, asserted so that a write cannot be added quietly.

    Checking `new`/`dirty`/`deleted` rather than a list of allowed names is what makes
    this survive a rename: it fails the moment a tool writes, whatever it writes and
    whatever it is called. A model in a capture thread has these tools and no others.
    """
    names = [tool["function"]["name"] for tool in chat_tools.TOOLS]
    assert names, "a chat with no tools would make this vacuous"

    for name in names:
        chat_tools.call(db, name, "{}")
        assert not db.new, f"{name} inserted rows"
        assert not db.dirty, f"{name} modified rows"
        assert not db.deleted, f"{name} deleted rows"


def test_a_capture_thread_must_name_an_entry_and_the_others_must_not(
    db: Session,
) -> None:
    """The three kinds cannot be mixed up, refused rather than coerced.

    A capture thread with no entry is a transcript of questions about a photograph
    nobody can open; a search thread carrying one would be reachable from an entry it is
    not about.
    """
    import pytest

    with pytest.raises(chat.ChatError) as missing:
        chat.create_thread(db, kind=ChatKind.CAPTURE)
    assert missing.value.reason == "intake_required"

    with pytest.raises(chat.ChatError) as extra:
        chat.create_thread(db, kind=ChatKind.SEARCH, intake_id=1)
    assert extra.value.reason == "intake_not_allowed"


def test_deleting_the_entry_takes_its_conversation_with_it(client: TestClient, db: Session) -> None:
    """`CASCADE`, unlike `model_runs.intake_id`'s `SET NULL`, and the difference matters.

    A run is the record that a GPU second was spent and outlives its subject. This
    thread is a conversation *about* an entry and means nothing without it.
    """
    entry_id = _entry(client)
    thread_id = client.post(f"/api/intake/pending/{entry_id}/chat").json()["thread_id"]

    entry = db.get(PendingIntake, entry_id)
    assert entry is not None
    db.delete(entry)
    db.commit()

    assert db.get(ChatThread, thread_id) is None


def test_thread_for_is_idempotent_at_the_service_level(client: TestClient, db: Session) -> None:
    """The get-or-create, asserted below the route so the route cannot be the only guard."""
    entry_id = _entry(client)
    entry = db.get(PendingIntake, entry_id)
    assert entry is not None

    first, created_first = thread_for(db, entry=entry)
    second, created_second = thread_for(db, entry=entry)

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(chat.messages(db, thread_id=first.id)) == 1
