"""What a model was told, what it said, and what the call cost.

The transcript table. Everything else in this pipeline stores a model's
*conclusions* — `intake_identity_candidates` holds the identities a vision run
proposed, `parameter_value_candidate` holds the fields an extraction run
proposed — and none of it stores the conversation those conclusions came out of.
That gap is the reason this table exists, and it is not a logging convenience.

**The never-auto-accept rule is only reviewable if the prompt is reviewable.**
`CLAUDE.md` forbids accepting an OCR'd or model-read part number without a
person, and ADR 0021 makes `source_text` `NOT NULL` so the person has something
to check against the photograph. But a quote answers only *what the model said*.
When a reading is wrong the next question is always *what was it told* — did the
browser's OCR hand it `CFI4JT100K` and did it repeat the typo, was the barcode
anchor present, did the reasoning budget run out before the answer began. Without
the request and the raw completion, that question has no answer and the reviewer
is left arguing with a bare assertion. With them it is arithmetic.

ADR 0021 measured the case that settles it: a run that spent **12 318 characters
of reasoning and never answered**. Nothing about that is visible in a candidate
row, because there is no candidate row — and it is exactly the evidence somebody
needs.

## Deliberately not a ledger

No `RAISE(ABORT)` triggers, following `app.models.captures`' own paragraph on
this. A run is a notebook entry: evidence somebody keeps, which they must be able
to delete when it is noise, and which no balance is computed from. `stock_ledger`
is the thing money and counts hang off; deleting a stale transcript must stay a
one-click mistake to fix rather than a compensating row.

Consequences worth saying out loud rather than implying:

* **Retention is unbounded and nothing prunes.** A drain of forty photographs
  writes forty rows, each holding a prompt and a completion. That is fine at the
  scale this system runs at and it is not fine forever; the fix is a pruning pass
  and it is not written. The bound below caps one row, not the table.
* **A row can outlive its subject.** `intake_id` is `SET NULL` and
  `document_sha256` is not a foreign key at all, so the historical fact of the
  run survives the entry being deleted or the blob being pruned.

## No image bytes, ever

`request_json` is the payload as sent, with the image replaced by
`{"image_sha256": "..."}`. Storing the base64 would put megabytes per run into a
table nothing prunes, duplicate a blob that already exists in the document store,
and put image data in the one process (`app.api`) that ADR 0005 keeps pixels out
of. The hash is the handle: the picture is at
`GET /api/documents/{sha256}` and always was.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ModelRunKind
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: Characters kept of `request_json` and of `response_text`, each.
#:
#: **Deliberately loose.** ADR 0021 measured a single run emitting 12 318
#: characters of reasoning and no answer, and that transcript is the evidence
#: somebody needs — a tight bound would truncate precisely the runs worth
#: reading. 200 000 leaves an order of magnitude of headroom over the worst case
#: measured while still refusing an unbounded column, which is what a text field
#: written straight from a request body has to be.
#:
#: When it does bite, `ModelRun.truncated` is set. Silently cutting would leave a
#: reader comparing a prompt against a response that referred to something no
#: longer in it, with nothing to say why.
MAX_TRANSCRIPT_CHARS = 200_000

#: A provider's self-declared name (`local-ollama`, `local-vllm`) and the model it
#: served (`qwen3-vl:8b`). Matching `intake_identity_candidates`' widths, because
#: the same two strings are written to both from one result and two widths would
#: mean a value that fits one row and is truncated in the other.
MAX_PROVIDER_LENGTH = 64
MAX_MODEL_LENGTH = 128

#: A finish reason, in whichever vocabulary the server used. Free text: Ollama says
#: `done_reason` and the OpenAI shape says `finish_reason`, and
#: `app.services.enrichment.calls.CallStats` passes an unrecognised one through
#: rather than coercing it into a vocabulary it does not belong to. An enum here
#: would have to coerce.
MAX_FINISH_REASON_LENGTH = 64


class ModelRun(Base):
    """One call to a model: the prompt, the raw answer, and the bill."""

    __tablename__ = "model_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    kind: Mapped[str] = mapped_column(StrEnumType(ModelRunKind), nullable=False)

    #: Which deployment answered, and which weights it was serving. Free strings
    #: for the reason `IntakeIdentityCandidate.provider` gives: the whole point of
    #: `VisionProvider` is that swapping the model is configuration, and a new one
    #: must not need a migration to be able to say it was there.
    provider: Mapped[str] = mapped_column(String(MAX_PROVIDER_LENGTH), nullable=False)
    model: Mapped[str] = mapped_column(String(MAX_MODEL_LENGTH), nullable=False)

    #: The parked scan this run was about. `SET NULL` rather than `CASCADE`,
    #: unlike `intake_identity_candidates.intake_id`: a candidate is a *proposal
    #: about* an entry and is meaningless without it, while a run is a record that
    #: a model was asked a question and cost something. Deleting the entry does not
    #: un-spend the GPU second.
    #:
    #: **Nullable because an extraction run has no intake.** It is about a
    #: document, and the column that identifies it is `document_sha256` below.
    intake_id: Mapped[int | None] = mapped_column(
        ForeignKey("pending_intakes.id", ondelete="SET NULL")
    )

    #: What the model was shown — a photograph for a vision run, a PDF for an
    #: extraction one. Deliberately **not** a foreign key to `documents.sha256`,
    #: exactly as `research_candidates.document_sha256` is not: a blob may be
    #: pruned or replaced without invalidating the historical fact that this run
    #: read it, and a dangling reference here is a diagnostic curiosity rather
    #: than a correctness problem.
    document_sha256: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    #: When the call came back, successfully or not. Recorded separately from
    #: `latency_ms` because that number is the *provider's* measurement of its own
    #: call and this pair is the worker's wall clock; a large gap between them is
    #: itself a finding.
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # -- `CallStats`, persisted -------------------------------------------------
    #
    # Every one of these is nullable, and **not one of them defaults to 0**. That
    # is `app.services.enrichment.calls.CallStats`' own rule and it is repeated
    # here because a column is easier to give a default to than a dataclass field:
    # `usage` is optional in the OpenAI response shape and several local servers
    # omit it, so **a missing count must stay distinguishable from a count of
    # zero**. A zero default would make "the server did not say" read as "the
    # prompt was empty", and any average taken over these rows would be pulled
    # toward whichever servers were quiet.
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    #: `stop`, `length`, or whatever this server calls it. `length` is the one that
    #: matters: it says the answer ran out of room rather than finishing, which is
    #: the difference between a broken model and a budget set too low.
    finish_reason: Mapped[str | None] = mapped_column(String(MAX_FINISH_REASON_LENGTH))

    # -- the transcript --------------------------------------------------------

    #: The payload as sent, **with the image replaced by `{"image_sha256": ...}`**.
    #: See the module docstring for why the bytes are never here.
    #:
    #: Nullable, and the NULL is meaningful: it says the transport raised before it
    #: could report what it had built. Storing `""` instead would be
    #: indistinguishable from a payload that was genuinely empty, which is the same
    #: missing-versus-zero mistake the token counts above avoid.
    request_json: Mapped[str | None] = mapped_column(Text)

    #: The completion string exactly as returned, **before parsing**. This is the
    #: point of the table: `parse_response` refuses a malformed answer and raises,
    #: so without the raw string the most interesting runs — the ones that produced
    #: no candidate row at all — leave nothing behind to read.
    #:
    #: NULL when the call never got as far as a completion.
    response_text: Mapped[str | None] = mapped_column(Text)

    #: What broke, when something did. NULL on a run that completed — including a
    #: run that completed by naming nothing, which is a normal answer and not an
    #: error (`DispatchState.UNIDENTIFIED`, and `ResearchState.EXHAUSTED` before
    #: it). A health check reading this column must not treat "the model could not
    #: tell" as breakage.
    error: Mapped[str | None] = mapped_column(Text)

    #: Set when `request_json` or `response_text` hit `MAX_TRANSCRIPT_CHARS`.
    #: Stored rather than derived from the stored length, because after truncation
    #: the length is the bound and says nothing about whether it was reached.
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    __table_args__ = (
        # The timeline's query, and the only one that runs per request: one
        # intake entry's runs in the order they happened. Composite rather than a
        # bare index on `intake_id`, whose leading column this already serves.
        Index("ix_model_runs_intake", "intake_id", "started_at"),
    )
