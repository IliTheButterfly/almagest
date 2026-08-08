"""Talking *about* a capture, beside the read rather than to it (ADR 0018, ADR 0021).

The activity screen answers *what happened*. It cannot answer *why* — "why did you
read the FCC ID as a part number", "is `CFI4JT100K` even a real Stackpole code",
"what else could that marking be" — and those are the questions a reviewer actually
has in front of a wrong reading. So there is a conversation, and it hangs off the
entry.

## The dispatch run stays single-shot, and this is why the chat is separate

The vision call is a **schema-constrained decode**: `vision.schema_for` gives the
model no `url` property, requires a non-empty `source_text`, and caps the array at
`max_candidates`. That constraint is not a preference, it is the mechanism — it is
what makes an invented datasheet URL *unrepresentable* rather than discouraged, and
what keeps a reading from arriving without the characters it was read from.

**A conversation cannot be constrained that way.** Free prose has no schema, so a
chat that also produced identities would reopen precisely the failure ADR 0021
exists to prevent: a plausible part number, asserted confidently, with nothing to
check it against. Making the dispatch call conversational was therefore rejected
outright. The read stays one shot with a schema, and talking happens next to it.

## The model does not see the photograph, and that is a limitation not a choice

`chat_agent.ChatModel` is text-only. `to_messages` emits `{"role", "content"}` with
`content` a plain string, and `OpenAICompatChatModel` posts that to
`/v1/chat/completions` — there is no content-parts list, no `images` array, and
nothing anywhere in the chat path that could carry base64. Only
`vision_openai_compat` speaks multimodal, and only the *worker* does, because ADR
0005 keeps pixels out of the API process.

Forcing an image in would mean giving the API process a multimodal transport, which
is the one thing that fence exists to prevent. So the thread is seeded with the
**context** instead: the browser's barcode and OCR lines, the candidates with their
quoted `source_text`, and the raw completion. That is enough for the question that
matters — *why did you think it was the FCC ID* is answered by the prompt and the
quote, not by the pixels — and it is honestly less than seeing the picture. The
briefing says so in as many words, so the model does not answer as though it had
looked.

## Nothing here can write an identity, structurally

The three columns ADR 0021 forbids anything automated from touching are
`pending_intakes.mpn`, `resolved_part_id` and `status`, plus the
`intake_identity_candidates` rows. This module reads all of them and writes a
`chat_threads` row and `chat_messages` rows. That is the whole surface:

* the only tools a chat model has are `chat_tools.TOOLS`, which are two **reads**;
* there is no route from a thread to `dispatch.record_result` or to
  `intake.resolve` — accepting a candidate is still
  `POST /api/intake/pending/{id}/resolve`, pressed by a person, and there is
  deliberately no second door;
* `tests/integration/test_capture_chat.py` asserts it by running a turn and
  comparing the entry and its candidates before and after.

A model in this thread can say "I think it is the other one". Acting on that is
still somebody's decision.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat import ChatThread
from app.models.enums import ChatKind, ChatRole
from app.models.scanning import PendingIntake
from app.services import activity, chat

#: Characters of the raw completion carried into the briefing.
#:
#: Far below `MAX_TRANSCRIPT_CHARS`, and the gap is the point: the run may hold 12 318
#: characters of reasoning (ADR 0021 measured exactly that), and pasting all of it into
#: every turn's history would spend the context window on deliberation nobody asked
#: about. The *answer* is short. What is cut is said to be cut, so the model does not
#: reason over a truncated JSON object believing it is whole.
MAX_RESPONSE_CHARS = 4_000

#: OCR lines carried in. A capture of a dense distributor label routinely has thirty;
#: beyond this the briefing stops being a briefing. Bounded rather than sampled — the
#: first lines are the top of the label, which is where a part number is printed.
MAX_OCR_LINES = 40

#: How the seeded turn opens, so a reader can tell it from something a person typed.
BRIEFING_HEADER = "Context for this scan (seeded automatically, not typed by anybody):"


def briefing_for(session: Session, *, entry: PendingIntake) -> str:
    """Everything a text model can be told about one parked scan.

    Assembled from `activity.story_for`, so this and the activity screen are reading
    the same seven tables through the same function — a second query set would drift,
    and the failure would be a model answering about a state the screen never showed.

    **Says plainly that there is no image.** A model told about a photograph it cannot
    see will otherwise answer as though it looked, and a confident description of
    pixels nobody showed it is the exact genre of wrongness this pipeline is built
    around.
    """
    story = activity.story_for(session, entry=entry)
    lines: list[str] = [BRIEFING_HEADER, ""]

    lines.append(
        "You are being asked about one scan in an electronic-component inventory. "
        "**You cannot see the photograph** — no image is attached to this "
        "conversation and none can be. Everything below is text that was read off it "
        "by other means, or said about it by a vision model that could see it. Answer "
        "from this text, and say so when a question can only be settled by looking at "
        "the picture."
    )
    lines.append("")
    lines.append(f"Scan #{entry.id}. Raw scanned payload: {entry.raw_payload!r}")
    if entry.symbology is not None:
        lines.append(f"Decoded as {entry.symbology} ({entry.decoded_kind or 'unknown kind'}).")
    lines.append(f"Queue status: {entry.status}. Reading state: {entry.dispatch_state}.")

    if story.capture is None:
        lines.append("")
        lines.append("There is no photograph at all — this was a bare barcode scan.")
    else:
        barcodes = [r.text for r in story.capture.regions if r.kind == "barcode"]
        ocr = [r.text for r in story.capture.regions if r.kind != "barcode"]
        lines.append("")
        lines.append(
            "The browser read the photograph twice before any model saw it. "
            "A decoded barcode is checksummed and is the strongest evidence in this "
            "chain; an OCR line is the weakest and is often wrong about single "
            "characters (1/I, 0/O, 5/S)."
        )
        lines.append(
            f"Barcodes decoded ({len(barcodes)}): "
            + (", ".join(repr(text) for text in barcodes) if barcodes else "none")
        )
        shown = ocr[:MAX_OCR_LINES]
        lines.append(f"OCR lines ({len(ocr)}):")
        lines.extend(f"  {text}" for text in shown)
        if len(ocr) > len(shown):
            lines.append(f"  … and {len(ocr) - len(shown)} more, not shown here.")

    lines.append("")
    if not story.identity_candidates:
        lines.append(
            "No identity has been proposed. Either no model has read it yet, or one "
            "read it and could name nothing — which is a normal answer here, not a "
            "failure."
        )
    else:
        lines.append(
            "A vision model proposed these identities, best first. The confidence "
            "shown is the **stored** one, clamped below the threshold at which "
            "anything is promoted automatically; the model's own self-report survives "
            "only in the raw completion below. Nothing here has been accepted — a "
            "person accepts a reading, never a model."
        )
        for rank, candidate in enumerate(story.identity_candidates, start=1):
            lines.append(
                f"  {rank}. {candidate.mpn}"
                + (f" ({candidate.manufacturer})" if candidate.manufacturer else "")
                + (f", package {candidate.package}" if candidate.package else "")
                + f" — stored confidence {candidate.confidence:.2f}"
            )
            lines.append(f"     quoted from the label: {candidate.source_text!r}")
            if candidate.note:
                lines.append(f"     the model's note: {candidate.note}")

    for run in story.model_runs:
        lines.append("")
        lines.append(f"Model call: {run.model} via {run.provider}.")
        if run.error is not None:
            lines.append(f"  It broke: {run.error}")
        if run.finish_reason is not None:
            lines.append(f"  finish_reason: {run.finish_reason}")
        if run.response_text is not None:
            body = run.response_text[:MAX_RESPONSE_CHARS]
            lines.append("  Its raw completion, before anything parsed it:")
            lines.append(f"  {body}")
            if len(run.response_text) > len(body):
                lines.append(
                    f"  … cut here; {len(run.response_text) - len(body)} more characters "
                    "were returned and are not shown."
                )

    lines.append("")
    lines.append(
        "The question follows. You cannot change what this scan is recorded as — you "
        "have no tool that writes an identity, and accepting a reading is a person's "
        "decision made elsewhere. Explain, disagree, and rank; do not assert."
    )
    return "\n".join(lines)


def thread_for(session: Session, *, entry: PendingIntake) -> tuple[ChatThread, bool]:
    """The conversation about this scan, creating and seeding it once. `(thread, created)`.

    **Get-or-create, keyed on `chat_threads.intake_id`.** Pressing the button twice
    must reopen the conversation rather than start a second one — a person who asked
    three questions yesterday and comes back today is looking for those three
    questions, and a fresh empty thread would silently orphan them. An archived thread
    is skipped, so archiving is a real way to start over.

    The briefing is seeded as a **`user`** turn, which is a deliberate and slightly
    odd choice. `chat_agent.to_messages` drops stored `system` turns and prepends the
    current `SYSTEM_PROMPT` instead — correct, because replaying an older system turn
    would answer under instructions that have since changed — so a briefing stored as
    `system` would be *silently dropped* and the model would answer about a scan it
    had been told nothing about. Stored as a user turn it reaches the model on every
    turn of the conversation, and `BRIEFING_HEADER` is what tells a reader that
    nobody typed it.
    """
    existing = session.execute(
        select(ChatThread)
        .where(
            ChatThread.intake_id == entry.id,
            ChatThread.kind == ChatKind.CAPTURE,
            ChatThread.archived_at.is_(None),
        )
        .order_by(ChatThread.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    thread = chat.create_thread(
        session,
        kind=ChatKind.CAPTURE,
        intake_id=entry.id,
        title=f"Scan #{entry.id}: {entry.mpn or entry.raw_payload[:40]}",
    )
    chat.append_message(
        session,
        thread=thread,
        role=ChatRole.USER,
        content=briefing_for(session, entry=entry),
    )
    return thread, True
